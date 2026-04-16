"""
Módulo para gestión de tokens JWT en el servidor WebSocket.
Optimizado: rotación condicional + pipeline HSET (sin SET JSON), minimizando escrituras.
"""

import logging
import uuid
import time
import tornado.websocket
import hashlib
import json
from typing import Dict, Any, List

from session import (
    get_client, add_message_to_queue, init_redis_client, add_jti_to_blacklist
)
from config import CLIENT_KEY_PREFIX, SESSION_KEY_PREFIX
from utils import crear_token_jwt

PARTICIPANTS_HASH_FIELD = 'participants_hash'
LAST_JWT_ROTATION_FIELD = 'last_jwt_rotation'
JWT_MIN_ROTATION_SECONDS = 300  # Rotación mínima (5 min) salvo cambios de participantes / force

async def generate_and_distribute_jwts(session_data, client_data_of_joiner=None, active_websockets=None, force_regenerate=False):
    """Genera/regenera JWTs para todos los clientes de la sesión cuando es necesario.

    Regeneración disparada por:
      - Nuevo cliente (hash de participantes cambia)
      - force_regenerate=True
      - Rotación temporal (>= JWT_MIN_ROTATION_SECONDS desde última rotación)

    Pasos:
      1. Calcular hash de participantes y decidir regeneración.
      2. Si no se requiere, salir temprano (ahorra lecturas / escrituras).
      3. Generar tokens en memoria y actualizar sólo current_jti modificados (pipeline).
      4. Actualizar metadata de sesión (hash y timestamp rotación) una sola vez.
    """
    if not session_data:
        logging.error("[JWT-DIST] session_data es None.")
        return

    room_id_for_jwt = session_data.get('room_id')
    if not room_id_for_jwt:
        logging.error("[JWT-DIST] room_id ausente en session_data.")
        return

    participant_ids: List[str] = session_data.get('clients', []) or []
    participant_ids_sorted = sorted(participant_ids)
    current_hash = hashlib.sha256((','.join(participant_ids_sorted)).encode('utf-8')).hexdigest()
    stored_hash = session_data.get(PARTICIPANTS_HASH_FIELD)
    last_rotation = session_data.get(LAST_JWT_ROTATION_FIELD, 0)
    now = time.time()

    need_regen = force_regenerate or (current_hash != stored_hash) or (now - last_rotation >= JWT_MIN_ROTATION_SECONDS)

    if not need_regen:
        logging.debug(f"[JWT-DIST] No se requiere regeneración JWT (hash estable, dentro ventana). Sala {room_id_for_jwt}")
        return

    if not participant_ids:
        logging.warning(f"[JWT-DIST] Sesión {room_id_for_jwt} sin participantes.")
        return

    rc = await init_redis_client()
    if not rc:
        logging.error("[JWT-DIST] Redis no disponible para generar tokens.")
        return

    # Recolectar datos de clientes (una sola pasada)
    clients_data: Dict[str, Dict[str, Any]] = {}
    for pid in participant_ids:
        if client_data_of_joiner and pid == client_data_of_joiner.get('client_id'):
            cdata = client_data_of_joiner
        else:
            cdata = await get_client(pid)
        if not cdata:
            # Registro mínimo
            is_mobile = not pid.startswith('web-')
            cdata = {
                'client_id': pid,
                'room_id': room_id_for_jwt,
                'status': 'pending',  # legacy
                'is_web': pid.startswith('web-'),
                'is_mobile': is_mobile,
                'last_seen': now
            }
        clients_data[pid] = cdata

    pipeline = rc.pipeline()
    messages_to_send = []
    previous_jtis=[]
    for pid, cdata in clients_data.items():
        old_jti = cdata.get('current_jti')
        new_jti = uuid.uuid4().hex
        token = crear_token_jwt(pid, room_id_for_jwt, jti=new_jti)
        if not token:
            logging.error(f"[JWT-DIST] Falló creación token para {pid} en sala {room_id_for_jwt}.")
            continue
        if old_jti:
            previous_jtis.append(old_jti)
        cdata['current_jti'] = new_jti
        _hset_client_dict(pipeline, pid, cdata)
        messages_to_send.append({
            'event': 'jwt_updated', 'jwt_token': token, 'client_id': pid,
            'room_id': room_id_for_jwt, 'status': 'success', 'timestamp': now,
            'is_mobile': cdata.get('is_mobile', not pid.startswith('web-'))
        })

    session_data[PARTICIPANTS_HASH_FIELD] = current_hash
    session_data[LAST_JWT_ROTATION_FIELD] = now
    _hset_session_dict(pipeline, room_id_for_jwt, session_data)

    # Ejecutar pipeline
    try:
        await pipeline.execute()
    except Exception as e:
        logging.error(f"[JWT-DIST] Error ejecutando pipeline: {e}")

    # Blacklist de JTIs previos (best effort)
    for jti in previous_jtis:
        try:
            # TTL = tiempo restante estimado; usamos expiración estándar JWT_MIN_ROTATION_SECONDS como fallback
            from config import JWT_EXPIRATION_DELTA_SECONDS as _JWT_EXP
            await add_jti_to_blacklist(jti, _JWT_EXP)
        except Exception:
            pass

    # Distribuir tokens
    distributed = 0
    queued = 0
    for msg in messages_to_send:
        pid = msg['client_id']
        if active_websockets and pid in active_websockets:
            try:
                await active_websockets[pid].write_message(msg)
                distributed += 1
            except tornado.websocket.WebSocketClosedError:
                await add_message_to_queue(pid, msg)
                queued += 1
            except Exception as e:
                logging.error(f"[JWT-DIST] Error enviando token a {pid}: {e}")
                await add_message_to_queue(pid, msg)
                queued += 1
        else:
            await add_message_to_queue(pid, msg)
            queued += 1

    logging.info(f"[JWT-DIST] Rotación JWT sala {room_id_for_jwt}: participantes={len(participant_ids)} distribuidos={distributed} encolados={queued} hash={current_hash[:8]}.. force={force_regenerate}")


# Helpers hash -------------------------------------------------
COMPLEX_TYPES = (dict, list)

def _hset_client_dict(pipeline, client_id: str, data: Dict[str,Any]):
    key = f"{CLIENT_KEY_PREFIX}{client_id}"
    for f,v in data.items():
        if isinstance(v, COMPLEX_TYPES):
            pipeline.hset(key, f, json.dumps(v))
        else:
            pipeline.hset(key, f, v)
    # TTL (usa SESSION_TIMEOUT genérico si disponible)
    try:
        from config import SESSION_TIMEOUT as _SESSION_TTL
        pipeline.expire(key, _SESSION_TTL)
    except Exception:
        pass

def _hset_session_dict(pipeline, room_id: str, data: Dict[str,Any]):
    key = f"{SESSION_KEY_PREFIX}{room_id}"
    for f,v in data.items():
        if isinstance(v, (dict, list)):
            pipeline.hset(key, f, json.dumps(v))
        else:
            pipeline.hset(key, f, v)
    try:
        from config import SESSION_TIMEOUT as _SESSION_TTL
        pipeline.expire(key, _SESSION_TTL)
    except Exception:
        pass

__all__ = ['generate_and_distribute_jwts']
