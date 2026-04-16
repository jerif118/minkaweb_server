import asyncio # Importar asyncio
import redis.asyncio as aioredis # Usar redis-py asyncio
import redis.exceptions as redis # Para excepciones
from datetime import datetime, timedelta, timezone
import uuid
import json
import logging
import time
from typing import Any, Dict, List, Optional

# Importar funciones JWT desde utils.py
from utils import crear_token_jwt, verificar_token_jwt_async

# Importar configuraciones de config.py
from config import (
    REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASSWORD,
    SESSION_KEY_PREFIX as CFG_SESSION_KEY_PREFIX,
    CLIENT_KEY_PREFIX as CFG_CLIENT_KEY_PREFIX,
    JWT_BLACKLIST_KEY_PREFIX as CFG_JWT_JTI_BLACKLIST_PREFIX,
    MESSAGE_QUEUE_KEY_PREFIX as CFG_MESSAGE_QUEUE_KEY_PREFIX,
    SESSION_TIMEOUT as CFG_SESSION_TIMEOUT,
    RECONNECT_GRACE_PERIOD as CFG_RECONNECT_GRACE_PERIOD,
    DOZE_TIMEOUT as CFG_DOZE_TIMEOUT,
    WEB_RECONNECT_TIMEOUT as CFG_WEB_RECONNECT_TIMEOUT,
    MESSAGE_TTL_SECONDS as CFG_MESSAGE_TTL_SECONDS,
    JWT_SECRET_KEY as CFG_JWT_SECRET_KEY,
    JWT_ALGORITHM as CFG_JWT_ALGORITHM,
    JWT_EXPIRATION_DELTA_SECONDS as CFG_JWT_EXPIRATION_DELTA_SECONDS,
    MAX_QUEUE_LENGTH, # Añadido para usarlo en add_message_to_queue
    REDIS_SSL
)

# Variable global para el cliente Redis asíncrono
redis_client = None

async def init_redis_client():
    """Inicializa la conexión global del cliente Redis asíncrono."""
    global redis_client
    if redis_client is None:
        scheme = 'rediss' if REDIS_SSL else 'redis'
        redis_url = (
            f"{scheme}://default:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
            "?ssl_cert_reqs=none"
        )
        logging.info(f"[REDIS_INIT] Conectando a Redis en {REDIS_HOST}:{REDIS_PORT}, DB: {REDIS_DB}")
        try:
            redis_client = await aioredis.from_url(redis_url, decode_responses=True)
            await redis_client.ping()
            logging.info("[REDIS_INIT] Conexión a Redis establecida y verificada (ping exitoso).")
        except (redis.RedisError, ConnectionRefusedError) as e: # Capturar ConnectionRefusedError también
            logging.error(f"[REDIS_INIT] No se pudo conectar a Redis: {e}")
            redis_client = None # Asegurar que redis_client es None si falla la conexión
            raise # Relanzar para que el llamador maneje la falla de conexión crítica
    return redis_client

# Prefijos para keys de redis (usando los de config.py)
SESSION_KEY_PREFIX = CFG_SESSION_KEY_PREFIX
CLIENT_KEY_PREFIX = CFG_CLIENT_KEY_PREFIX
JWT_JTI_BLACKLIST_PREFIX = CFG_JWT_JTI_BLACKLIST_PREFIX
MESSAGE_QUEUE_KEY_PREFIX = CFG_MESSAGE_QUEUE_KEY_PREFIX

# Tiempos y TTLs (usando los de config.py)
SESSION_TIMEOUT = CFG_SESSION_TIMEOUT
RECONNECT_GRACE_PERIOD = CFG_RECONNECT_GRACE_PERIOD
DOZE_TIMEOUT = CFG_DOZE_TIMEOUT
WEB_RECONNECT_TIMEOUT = CFG_WEB_RECONNECT_TIMEOUT
MESSAGE_TTL_SECONDS = CFG_MESSAGE_TTL_SECONDS

# JWT Config (usando los de config.py)
JWT_SECRET_KEY = CFG_JWT_SECRET_KEY
JWT_ALGORITHM = CFG_JWT_ALGORITHM
JWT_EXPIRATION_DELTA_SECONDS = CFG_JWT_EXPIRATION_DELTA_SECONDS

# active_websockets se maneja en appv2.py, no aquí.

# Métricas Redis simples
REDIS_METRICS: Dict[str, Dict[str, float]] = {}

def _record_metric(op: str, elapsed: float):
    stat = REDIS_METRICS.setdefault(op, {'count': 0.0, 'total_time': 0.0, 'max_time': 0.0})
    stat['count'] += 1
    stat['total_time'] += elapsed
    if elapsed > stat['max_time']:
        stat['max_time'] = elapsed

def get_redis_metrics_summary():
    summary = {}
    for op, data in REDIS_METRICS.items():
        avg = (data['total_time']/data['count'])*1000 if data['count'] else 0
        summary[op] = {
            'ops': int(data['count']),
            'avg_ms': round(avg, 3),
            'max_ms': round(data['max_time']*1000, 3)
        }
    return summary

# Utilidades HSET ---------------------------------
PRIMITIVE_FIELDS_CLIENT = {
    'client_id', 'room_id', 'status', 'is_web', 'is_mobile', 'current_jti',
    'pending_since', 'doze_start', 'doze_id'
}
NUMERIC_FIELDS_CLIENT = {
    'last_seen', 'message_queue_len'
}
PRIMITIVE_FIELDS_SESSION = {
    'room_id', 'password', 'initiator_id', 'status', 'has_dozing_client',
    'doze_client_id', 'participants_hash', 'last_jwt_rotation'
}
NUMERIC_FIELDS_SESSION = {
    'created_at', 'last_activity', 'doze_start_time'
}
LIST_FIELDS_SESSION = {'clients'}

async def _migrate_string_to_hash(rc, key):
    try:
        ktype = await rc.type(key)
        if ktype == 'string':
            raw = await rc.get(key)
            if raw:
                try:
                    data = json.loads(raw)
                except Exception:
                    return
                pipe = rc.pipeline()
                # Eliminar primero el string para evitar WRONGTYPE en hset
                pipe.delete(key)
                for f, v in data.items():
                    if isinstance(v, (dict, list)):
                        pipe.hset(key, f, json.dumps(v))
                    else:
                        pipe.hset(key, f, v)
                await pipe.execute()
    except Exception as e:
        logging.debug(f"[MIGRATE] Fallo migrando {key}: {e}")

def _encode_value(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)

def _decode_client_hash(h: Dict[str,str]) -> Dict[str,Any]:
    if not h: return {}
    out: Dict[str,Any] = {}
    for k,v in h.items():
        if k in NUMERIC_FIELDS_CLIENT:
            try: out[k] = float(v) if '.' in v else int(v)
            except: out[k]=v
        else:
            if v and (v.startswith('{') or v.startswith('[')):
                try: out[k]=json.loads(v)
                except: out[k]=v
            else:
                out[k]=v
    return out

def _decode_session_hash(h: Dict[str,str]) -> Dict[str,Any]:
    if not h: return {}
    out: Dict[str,Any] = {}
    for k,v in h.items():
        if k in NUMERIC_FIELDS_SESSION:
            try: out[k] = float(v) if '.' in v else int(v)
            except: out[k]=v
        elif k in LIST_FIELDS_SESSION:
            try: out[k]=json.loads(v)
            except: out[k]=[]
        else:
            if v and (v.startswith('{') or v.startswith('[')):
                try: out[k]=json.loads(v)
                except: out[k]=v
            else:
                out[k]=v
    return out

# Reemplazar implementaciones get/set con soporte hash
async def get_session(room_id):
    start=time.perf_counter()
    rc = await init_redis_client()
    if not rc: return None
    if not room_id:
        return None
    key=f"{SESSION_KEY_PREFIX}{room_id}"
    await _migrate_string_to_hash(rc, key)
    data = await rc.hgetall(key)
    _record_metric('get_session', time.perf_counter()-start)
    if not data:
        return None
    return _decode_session_hash(data)

async def set_session(room_id, data):
    start=time.perf_counter()
    rc = await init_redis_client()
    if not rc: return False
    if not room_id:
        return False
    key=f"{SESSION_KEY_PREFIX}{room_id}"
    pipe = rc.pipeline()
    # Actualización NO destructiva: sobrescribe campos presentes.
    for f,v in data.items():
        if f in LIST_FIELDS_SESSION and isinstance(v, list):
            pipe.hset(key, f, json.dumps(v))
        elif isinstance(v,(dict,list)):
            pipe.hset(key,f,json.dumps(v))
        else:
            pipe.hset(key,f,v)
    pipe.expire(key, SESSION_TIMEOUT)
    try:
        await pipe.execute(); ok=True
    except Exception as e:
        logging.error(f"Error de Redis en set_session para {room_id}: {e}"); ok=False
    _record_metric('set_session', time.perf_counter()-start)
    return ok

async def delete_session(room_id):
    start=time.perf_counter()
    rc = await init_redis_client()
    if not rc: return False
    if not room_id:
        return False
    try:
        res = await rc.delete(f"{SESSION_KEY_PREFIX}{room_id}")>0
    except redis.RedisError as e:
        logging.error(f"Error de Redis en delete_session para {room_id}: {e}")
        res=False
    _record_metric('delete_session', time.perf_counter()-start)
    return res

async def get_all_session_keys():
    start=time.perf_counter()
    rc = await init_redis_client()
    if not rc: return []
    try:
        keys = await rc.keys(f"{SESSION_KEY_PREFIX}*")
        result=[key.replace(SESSION_KEY_PREFIX, "", 1) for key in keys]
    except redis.RedisError as e:
        logging.error(f"Error de Redis en get_all_session_keys: {e}")
        result=[]
    _record_metric('get_all_session_keys', time.perf_counter()-start)
    return result

async def get_client(client_id):
    start=time.perf_counter()
    rc = await init_redis_client()
    if not rc: return None
    if not client_id:
        return None
    key=f"{CLIENT_KEY_PREFIX}{client_id}"
    await _migrate_string_to_hash(rc, key)
    data = await rc.hgetall(key)
    _record_metric('get_client', time.perf_counter()-start)
    if not data:
        return None
    return _decode_client_hash(data)

async def set_client(client_id, data):
    start=time.perf_counter()
    rc = await init_redis_client()
    if not rc: return False
    if not client_id:
        return False
    key=f"{CLIENT_KEY_PREFIX}{client_id}"
    pipe = rc.pipeline()
    for f,v in data.items():
        pipe.hset(key, f, _encode_value(v))
    # Reutilizamos SESSION_TIMEOUT como TTL genérico para clientes
    pipe.expire(key, SESSION_TIMEOUT)
    try:
        await pipe.execute(); ok=True
    except Exception as e:
        logging.error(f"Error de Redis en set_client para {client_id}: {e}"); ok=False
    _record_metric('set_client', time.perf_counter()-start)
    return ok

async def delete_client(client_id, keep_message_queue=False):
    start=time.perf_counter()
    rc = await init_redis_client()
    if not rc: return False
    if not client_id:
        return False
    try:
        result = await rc.delete(f"{CLIENT_KEY_PREFIX}{client_id}") > 0
        if not keep_message_queue:
            await rc.delete(f"{MESSAGE_QUEUE_KEY_PREFIX}{client_id}")
    except redis.RedisError as e:
        logging.error(f"Error de Redis en delete_client para {client_id}: {e}")
        result=False
    _record_metric('delete_client', time.perf_counter()-start)
    return result

async def get_all_client_keys():
    start=time.perf_counter()
    rc = await init_redis_client()
    if not rc: return []
    try:
        keys = await rc.keys(f"{CLIENT_KEY_PREFIX}*")
        result=[key.replace(CLIENT_KEY_PREFIX, "", 1) for key in keys]
    except redis.RedisError as e:
        logging.error(f"Error de Redis en get_all_client_keys: {e}")
        result=[]
    _record_metric('get_all_client_keys', time.perf_counter()-start)
    return result

# Gestión de JTIs para JWT
async def add_jti_to_blacklist(jti, ttl_seconds):
    rc = await init_redis_client()
    if not rc: return False
    if not jti:
        return False
    try:
        return await rc.setex(f"{JWT_JTI_BLACKLIST_PREFIX}{jti}", ttl_seconds, "1")
    except redis.RedisError as e:
        logging.error(f"Error de Redis en add_jti_to_blacklist para JTI {jti}: {e}")
        return False

async def is_jti_blacklisted(jti):
    rc = await init_redis_client()
    if not rc: return True # Si Redis no está, considerar inválido por seguridad
    if not jti:
        return True 
    try:
        return await rc.exists(f"{JWT_JTI_BLACKLIST_PREFIX}{jti}") == 1
    except redis.RedisError as e:
        logging.error(f"Error de Redis en is_jti_blacklisted para JTI {jti}: {e}")
        return True # Fallar seguro

async def is_valid_jti(jti):
    rc = await init_redis_client()
    if not rc: return False # Si Redis no está, considerar inválido
    return jti and not await is_jti_blacklisted(jti)

# Funciones JWT - Ahora usando las de utils.py
def generate_jwt(client_id, room_id):
    """
    Crea un token JWT para un cliente y sala específicos.
    Esta función es un wrapper de crear_token_jwt de utils.py para mantener compatibilidad.
    """
    return crear_token_jwt(client_id, room_id)

async def verify_jwt(token):
    """
    Verifica un token JWT y su JTI.
    Esta función es un wrapper de verificar_token_jwt_async de utils.py para mantener compatibilidad.
    """
    if not token:
        return None
    
    async def check_jti_blacklisted(jti):
        return await is_jti_blacklisted(jti)
    
    return await verificar_token_jwt_async(token, check_jti_blacklisted)

# Funciones para manejar la cola de mensajes
# Helpers comunes cola mensajes ---------------------------------

def _prepare_queue_message(client_id: str, message: Dict[str,Any], *, is_dozing: bool, is_mobile: bool, now: Optional[float]=None) -> Dict[str,Any]:
    now = now or time.time()
    sender_id = message.get('sender_id', 'unknown') if isinstance(message, dict) else 'unknown'
    return {
        'id': f"{now}-{uuid.uuid4().hex[:8]}",
        'data': message,
        'timestamp': now,
        'attempts': 0,
        'sender_id': sender_id,
        'is_doze_message': is_dozing,
        'for_mobile': is_mobile
    }

def _serialize_queue_message(msg: Dict[str,Any]) -> Optional[str]:
    try:
        return json.dumps(msg)
    except Exception as e:
        logging.error(f"[QUEUE] Error serializando mensaje {msg.get('id')}: {e}")
        return None

async def add_message_to_queue(client_id, message):
    start=time.perf_counter()
    rc = await init_redis_client()
    if not rc: return False
    if not client_id or not message:
        return False

    client_data = await get_client(client_id)
    if not client_data:
        is_mobile = not client_id.startswith('web-')
        logging.info(f"[QUEUE] Creando registro temporal para cliente {client_id} (móvil: {is_mobile}) para encolar mensaje")
        client_data = {
            'client_id': client_id,
            'status': 'pending',
            'created_at': time.time(),
            'is_web': not is_mobile,
            'is_mobile': is_mobile
        }
        await set_client(client_id, client_data)

    is_dozing = client_data.get('status') == 'dozing'
    is_mobile = client_data.get('is_mobile', not client_id.startswith('web-'))

    wrapped = _prepare_queue_message(client_id, message, is_dozing=is_dozing, is_mobile=is_mobile)
    message_json = _serialize_queue_message(wrapped)
    if message_json is None:
        return False

    queue_key = f"{MESSAGE_QUEUE_KEY_PREFIX}{client_id}"
    try:
        current_length = await rc.llen(queue_key)
        if current_length >= MAX_QUEUE_LENGTH:
            logging.warning(f"Cola para {client_id} llena ({MAX_QUEUE_LENGTH}). No se añadió {wrapped['id']}.")
            return False
        result = await rc.rpush(queue_key, message_json)
        current_ttl = await rc.ttl(queue_key)
        if current_ttl == -1:
            await rc.expire(queue_key, MESSAGE_TTL_SECONDS)
        logging.info(f"[QUEUE] Mensaje encolado para {client_id} (móvil: {is_mobile}, dozing: {is_dozing}). Cola: {await rc.llen(queue_key)}")
        _record_metric('add_message_to_queue', time.perf_counter()-start)
        return result > 0
    except redis.RedisError as e:
        logging.error(f"Error de Redis en add_message_to_queue para {client_id}: {e}")
        return False

async def batch_add_messages(messages_by_client: Dict[str, List[Dict[str,Any]]]):
    """Encola lotes de mensajes por cliente usando un pipeline y helpers comunes."""
    start=time.perf_counter()
    rc = await init_redis_client()
    if not rc: return False
    pipe = rc.pipeline()
    now=time.time()
    for client_id, msgs in messages_by_client.items():
        queue_key=f"{MESSAGE_QUEUE_KEY_PREFIX}{client_id}"
        try:
            current_len = await rc.llen(queue_key)
        except Exception:
            current_len = 0
        available = max(0, MAX_QUEUE_LENGTH - current_len)
        if available <= 0:
            logging.warning(f"[BATCH-QUEUE] Cola llena para {client_id}, se descarta lote ({len(msgs)} msgs)")
            continue
        accepted = msgs[:available]
        dropped = len(msgs) - len(accepted)
        if dropped>0:
            logging.warning(f"[BATCH-QUEUE] {dropped} mensajes descartados por límite para {client_id}")
        for msg in accepted:
            is_mobile = not client_id.startswith('web-')
            wrapped = _prepare_queue_message(client_id, msg, is_dozing=False, is_mobile=is_mobile, now=now)
            serialized = _serialize_queue_message(wrapped)
            if serialized:
                pipe.rpush(queue_key, serialized)
        pipe.expire(queue_key, MESSAGE_TTL_SECONDS)
    try:
        await pipe.execute(); ok=True
    except Exception as e:
        logging.error(f"[BATCH-QUEUE] Error ejecutando pipeline: {e}"); ok=False
    _record_metric('batch_add_messages', time.perf_counter()-start)
    return ok

async def get_pending_messages(client_id, delete_queue=True):
    rc = await init_redis_client()
    if not rc: return []
    if not client_id:
        return []

    queue_key = f"{MESSAGE_QUEUE_KEY_PREFIX}{client_id}"
    try:
        if not await rc.exists(queue_key):
            return []
            
        queue_length = await rc.llen(queue_key)
        if queue_length == 0:
            return []
        logging.info(f"Recuperando {queue_length} mensajes pendientes para {client_id}")
        
        messages_json = await rc.lrange(queue_key, 0, -1)
        
        messages = []
        deserialize_errors = 0
        
        for msg_json in messages_json:
            try:
                msg = json.loads(msg_json)
                if 'data' in msg:
                    messages.append(msg['data'])
                else:
                    messages.append(msg)
            except json.JSONDecodeError:
                deserialize_errors += 1
                logging.error(f"Error al deserializar mensaje de la cola para {client_id}")
        
        if delete_queue and deserialize_errors == 0:
            await rc.delete(queue_key)
            logging.info(f"Cola de mensajes eliminada para {client_id} después de recuperar {len(messages)} mensajes")
        elif deserialize_errors > 0:
            logging.warning(f"No se eliminó la cola para {client_id} debido a {deserialize_errors} errores de deserialización")
        elif not delete_queue:
            logging.info(f"Conservando cola de mensajes para {client_id} con {len(messages)} mensajes")
            await rc.expire(queue_key, MESSAGE_TTL_SECONDS)
        
        return messages
    except redis.RedisError as e:
        logging.error(f"Error de Redis en get_pending_messages para {client_id}: {e}")
        return []

async def close_redis_client():
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None
        logging.info("[REDIS_CLOSE] Conexión a Redis cerrada.")

# Rate limiting simple (token bucket básico con INCR + EXPIRE)
RATE_LIMIT_PREFIX = 'rl:'
async def is_rate_limited(bucket: str, limit: int, window_seconds: int) -> bool:
    rc = await init_redis_client()
    if not rc: return False  # si no hay Redis no limitamos para no romper funcionalidad (alternativa: bloquear)
    key = f"{RATE_LIMIT_PREFIX}{bucket}"
    try:
        val = await rc.incr(key)
        if val == 1:
            await rc.expire(key, window_seconds)
        return val > limit
    except Exception as e:
        logging.error(f"[RATE-LIMIT] Error aplicando RL {bucket}: {e}")
        return False

# Exponer métricas
__all__ = ['get_redis_metrics_summary', 'batch_add_messages', 'is_rate_limited']
