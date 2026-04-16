"""
Módulo para procesamiento de mensajes en el servidor WebSocket.
Contiene funciones para manejar diferentes tipos de mensajes y acciones.
"""
import asyncio
import logging
import time
import jwt
import uuid
import tornado.websocket
import json
from config import DOZE_TIMEOUT

from session import (
    get_session, set_session, delete_session,
    get_client, set_client, delete_client,
    add_jti_to_blacklist, add_message_to_queue, get_pending_messages,
    batch_add_messages, is_rate_limited
)

from config import JWT_ALGORITHM, JWT_SECRET_KEY, JWT_EXPIRATION_DELTA_SECONDS

from handlers.room_manager import cleanup_client_and_session, send_error_and_close
from handlers.state_model import (
    ConnectionState, ParticipationState, PresenceState,
    update_client_states, get_client_cached
)

async def process_message(handler, message_data, active_websockets):
    """
    Procesa un mensaje recibido del cliente y ejecuta la acción correspondiente.
    """
    # Validación de current_jti (anti token reuse si perdió rotación)
    provided_jti = message_data.get('current_jti') or message_data.get('jti')
    if provided_jti and handler.client_id:
        client_record = await get_client(handler.client_id)
        stored_jti = client_record.get('current_jti') if client_record else None
        if not stored_jti or stored_jti != provided_jti:
            logging.warning(f"[ON-MSG] JTI inconsistente para {handler.client_id} (prov={provided_jti} stored={stored_jti}). Rechazando mensaje")
            await handler.write_message({'error': 'Token no vigente.', 'code': 'STALE_JWT'} )
            return

    action = message_data.get('action')
    content = message_data.get('message')

    if action:
        if action == 'leave':
            reason = message_data.get('reason', 'client_request')
            jwt_token_in_message = message_data.get('jwt_token')
            await handle_leave_action(handler, reason, jwt_token_in_message, active_websockets)
        elif action == 'doze_start':
            await handle_doze_start_action(handler, active_websockets)
        else:
            logging.warning(f"[ON-MSG] {handler.client_id} envió acción desconocida: {action}")
            await handler.write_message({'error': 'Acción no válida.', 'code': 'INVALID_ACTION'})
    elif content is not None:
        await broadcast_message(handler, content, message_data.get('message_type', 'generic'), active_websockets)
    else:
        logging.warning(f"[ON-MSG] {handler.client_id} envió mensaje sin acción ni contenido 'message'.")
        await handler.write_message({'error': 'Mensaje vacío o malformado.', 'code': 'EMPTY_OR_MALFORMED_MESSAGE'})

async def handle_leave_action(handler, reason, jwt_token_to_invalidate=None, active_websockets=None):
    """Maneja la acción 'leave' de un cliente."""
    logging.info(f"[LEAVE] Cliente {handler.client_id} saliendo de sala {handler.room_id}. Razón: {reason}")
    handler.intentional_disconnect_flag = True

    # Invalidar JTI del cliente que se va
    jti_to_blacklist = None
    client_data = await get_client(handler.client_id)

    if jwt_token_to_invalidate:
        try:
            unverified_payload = jwt.decode(jwt_token_to_invalidate, algorithms=[JWT_ALGORITHM], options={"verify_signature": False, "verify_exp": False})
            jti_to_blacklist = unverified_payload.get('jti')
        except jwt.PyJWTError as e:
            logging.warning(f"[LEAVE-JTI] Error decodificando JWT para {handler.client_id}: {e}")
    
    if not jti_to_blacklist and client_data and client_data.get('current_jti'):
        jti_to_blacklist = client_data.get('current_jti')

    if jti_to_blacklist:
        await add_jti_to_blacklist(jti_to_blacklist, JWT_EXPIRATION_DELTA_SECONDS)
        logging.info(f"[LEAVE-JTI] JTI {jti_to_blacklist} para {handler.client_id} añadido a lista negra.")
        if client_data:
            client_data.pop('current_jti', None)
            await set_client(handler.client_id, client_data)
    else:
        logging.warning(f"[LEAVE-JTI] No se encontró JTI para blacklisting para {handler.client_id}.")

    # Obtener la sesión actual
    session_data = await get_session(handler.room_id)
    
    # Notificar a los demás participantes
    if session_data:
        other_clients = [cid for cid in session_data.get('clients', []) if cid != handler.client_id]
        for peer_id in other_clients:
            if peer_id in active_websockets:
                try:
                    active_websockets[peer_id].write_message({
                        'event': 'peer_left_room', 
                        'client_id': handler.client_id,
                        'reason': reason,
                        'message': 'El otro usuario salió de la sala.'
                    })
                except tornado.websocket.WebSocketClosedError:
                    pass
    
    # MODIFICACIÓN: Eliminar el cliente directamente cuando se recibe leave explícito
    # en lugar de marcarlo como pending_reconnect
    is_mobile_client = not handler.client_id.startswith("web-")
    
    # Para todos los clientes que envíen explícitamente "leave", eliminarlos completamente
    # Esto aplica tanto para móviles como para web cuando el leave es intencional
    keep_message_queue = False
    if reason in ["user_request", "user_logout", "doze_start"]:
        logging.info(f"[LEAVE] Cliente {handler.client_id} envió leave con razón '{reason}'. Eliminando completamente.")
        await delete_client(handler.client_id, keep_message_queue=keep_message_queue)
        
        # También eliminar de la sesión
        if session_data:
            if handler.client_id in session_data.get('clients', []):
                session_data['clients'].remove(handler.client_id)
                
            # Si la sesión quedó vacía, eliminarla
            if not session_data['clients']:
                await delete_session(handler.room_id)
                logging.info(f"[LEAVE] Sala {handler.room_id} eliminada por quedar vacía tras leave de {handler.client_id}")
            else:
                session_data['last_activity'] = time.time()
                await set_session(handler.room_id, session_data)
    else:
        # Para otro tipo de leave, usar la lógica anterior
        await cleanup_client_and_session(handler, is_leaving_normally=True, active_websockets=active_websockets)
    
    await handler.write_message({'event': 'left_room', 'message': 'Has salido de la sala.'})
    handler.close(code=1000, reason="Client initiated leave")

    # Actualizar modelo unificado: participation -> left, connection -> closed
    try:
        if handler.client_id:
            await update_client_states(handler.client_id, participation_state=ParticipationState.left, connection_state=ConnectionState.closed, touch_last_seen=False)
    except Exception as e:
        logging.error(f"[LEAVE] Error actualizando estados unificados: {e}")

async def handle_doze_start_action(handler, active_websockets=None):
    """Maneja la acción de un cliente (móvil) para entrar en modo 'doze'."""
    if not handler.client_id or handler.client_id.startswith("web-"):
        logging.warning(f"[DOZE] Cliente {handler.client_id or 'Desconocido'} intentó entrar en doze (no permitido para web).")
        await handler.write_message({'error': 'Acción no permitida para este tipo de cliente.', 'code': 'DOZE_NOT_ALLOWED'})
        return

    logging.info(f"[DOZE] Cliente {handler.client_id} entrando en modo doze en sala {handler.room_id}.")
    handler.intentional_disconnect_flag = True

    client_data = await get_client(handler.client_id)
    if not client_data:
        logging.error(f"[DOZE] No se encontraron datos para {handler.client_id} al intentar entrar en doze.")
        handler.close(code=1008, reason="Internal server error processing doze")
        return
    
    # Calcular deadline
    deadline = time.time() + DOZE_TIMEOUT
    client_data['status'] = 'dozing'  # legacy
    client_data['dozing'] = True
    client_data['doze_start'] = time.time()
    client_data['doze_id'] = uuid.uuid4().hex
    client_data['doze_deadline'] = deadline
    await set_client(handler.client_id, client_data)  # Mantener para compatibilidad legacy mínima

    # Actualizar nuevo modelo de estados
    try:
        await update_client_states(handler.client_id, presence_state=PresenceState.doze, connection_state=ConnectionState.closed, touch_last_seen=False, extra_updates={'doze_deadline': deadline})
    except Exception as e:
        logging.error(f"[DOZE] Error actualizando estados unificados: {e}")

    session_data = await get_session(handler.room_id)
    if not session_data:
        logging.error(f"[DOZE] No se encontró sala {handler.room_id} para {handler.client_id} al entrar en doze.")
        handler.close(code=1008, reason="Internal server error finding session for doze")
        return

    session_data['has_dozing_client'] = True
    session_data['doze_client_id'] = handler.client_id
    session_data['doze_start_time'] = time.time()
    session_data['last_activity'] = time.time()
    await set_session(handler.room_id, session_data)

    # Notificar al peer que este cliente entró en doze
    for peer_id in session_data.get('clients', []):
        if peer_id != handler.client_id and active_websockets and peer_id in active_websockets:
            try:
                await active_websockets[peer_id].write_message({
                    'event': 'peer_doze_mode_started',
                    'client_id': handler.client_id,
                    'message': f'Usuario {handler.client_id} ha entrado en modo de bajo consumo.'
                })
            except tornado.websocket.WebSocketClosedError:
                pass
    
    # Confirmar al cliente que la acción fue procesada
    await handler.write_message({'event': 'doze_mode_acknowledged', 'client_id': handler.client_id})
    
    # Remover de active_websockets y cerrar la conexión actual
    if active_websockets and handler.client_id in active_websockets:
        del active_websockets[handler.client_id]
    handler.close(code=1000, reason="Client initiated doze mode")

async def broadcast_message(handler, content, message_type="generic", active_websockets=None):
    """Envía un mensaje a los otros participantes en la sala con optimizaciones de E/S."""
    if not handler.client_id or not handler.room_id:
        return

    # Rate limit simple (protege recursos)
    if await is_rate_limited(f"bc:{handler.client_id}", limit=60, window_seconds=10):
        await handler.write_message({'error': 'Rate limit excedido.', 'code': 'RATE_LIMITED'})
        return

    # Validar tamaño
    try:
        serialized_len = len(json.dumps(content)) if not isinstance(content, str) else len(content)
    except Exception:
        serialized_len = 0
    MAX_MESSAGE_BYTES = 16_384
    if serialized_len > MAX_MESSAGE_BYTES:
        logging.warning(f"[BROADCAST] Mensaje demasiado grande ({serialized_len}B) de {handler.client_id}")
        await handler.write_message({'error': 'Mensaje demasiado grande.', 'code': 'MESSAGE_TOO_LARGE'})
        return

    # Normalizar tipo
    ALLOWED_TYPES = {'generic','text','image','control'}
    if message_type not in ALLOWED_TYPES:
        message_type = 'generic'

    session_data = await get_session(handler.room_id)
    if not session_data:
        logging.warning(f"[BROADCAST] {handler.client_id} - No se encontró sala {handler.room_id} para broadcast.")
        await handler.write_message({'error': 'Sala no encontrada.', 'code': 'ROOM_NOT_FOUND_FOR_BROADCAST'})
        return

    if handler.client_id not in session_data.get('clients', []):
        logging.warning(f"[BROADCAST] Cliente {handler.client_id} no pertenece ya a sala {handler.room_id}. Abortando envío.")
        await handler.write_message({'error': 'No perteneces a la sala.', 'code': 'NOT_IN_ROOM'})
        return

    recipients = [cid for cid in session_data.get('clients', []) if cid != handler.client_id]
    if not recipients:
        return

    message_id = f"{time.time()}-{uuid.uuid4().hex[:8]}"
    base_payload = {
        'event': 'new_message',
        'sender_id': handler.client_id,
        'room_id': handler.room_id,
        'message': content,
        'message_type': message_type,
        'timestamp': time.time(),
        'message_id': message_id
    }

    # Prefetch estados de destinatarios (concurrencia)
    states = await asyncio.gather(*[get_client_cached(rid) for rid in recipients])
    direct_targets = []
    queue_targets = []
    for rid, st in zip(recipients, states):
        if not st:
            queue_targets.append(rid)
            continue
        if active_websockets and rid in active_websockets and st.presence_state != PresenceState.doze:
            direct_targets.append(rid)
        else:
            queue_targets.append(rid)

    delivered = 0
    # Envío directo concurrente limitado
    async def _send_direct(rid):
        nonlocal delivered
        try:
            await active_websockets[rid].write_message(base_payload)
            delivered += 1
        except Exception as e:
            logging.debug(f"[BROADCAST] Falló envío directo a {rid}: {e}. Se pasará a cola.")
            queue_targets.append(rid)

    if active_websockets and direct_targets:
        # Limitar concurrencia para no bloquear loop con demasiados tasks
        SEM_LIMIT = 20
        sem = asyncio.Semaphore(SEM_LIMIT)
        async def _guarded_send(r):
            async with sem:
                await _send_direct(r)
        await asyncio.gather(*[_guarded_send(r) for r in direct_targets])

    # Encolar restantes en lote
    if queue_targets:
        mapping = {rid: [base_payload] for rid in queue_targets}
        await batch_add_messages(mapping)
        logging.debug(f"[BROADCAST] Encolados {len(queue_targets)} destinatarios para mensaje {message_id}")

    # Confirmación al emisor
    direct_attempts = len(recipients)
    await handler.write_message({
        'event': 'message_sent_confirmation',
        'message_id': message_id,
        'recipients_attempted': direct_attempts,
        'recipients_delivered_directly': delivered,
        'queued_recipients': len(queue_targets)
    })
    logging.info(f"[BROADCAST] Mensaje {message_id} de {handler.client_id}: directos={delivered} cola={len(queue_targets)} total={len(recipients)}")

async def send_message_to_peer(sender_id, recipient_id, message_payload, active_websockets=None):
    """
    Intenta enviar un mensaje directamente a un peer. Si falla o el peer está en doze,
    lo encola. Devuelve True si se envió directamente, False si se encoló o falló.
    
    Args:
        sender_id: ID del remitente
        recipient_id: ID del destinatario
        message_payload: Objeto completo del mensaje
        active_websockets: Diccionario de websockets activos
    """
    recipient_data = await get_client_cached(recipient_id)
    if not recipient_data:
        logging.warning(f"[SEND-PEER] Destinatario {recipient_id} no encontrado en Redis.")
        return False

    is_recipient_dozing = recipient_data.presence_state == PresenceState.doze
    
    if active_websockets and recipient_id in active_websockets and not is_recipient_dozing:
        try:
            await active_websockets[recipient_id].write_message(message_payload)
            logging.debug(f"[SEND-PEER] Mensaje enviado directamente a {recipient_id}.")
            return True
        except tornado.websocket.WebSocketClosedError:
            logging.warning(f"[SEND-PEER] WS cerrado para {recipient_id} al intentar enviar. Se encolará.")
        except Exception as e:
            logging.error(f"[SEND-PEER] Error enviando a {recipient_id}: {e}. Se encolará.")
    
    # Encolar si no se pudo entregar o si está en doze
    if await add_message_to_queue(recipient_id, message_payload):
        logging.info(f"[SEND-PEER] Mensaje para {recipient_id} (dozing: {is_recipient_dozing}) encolado.")
    else:
        logging.error(f"[SEND-PEER] Error al encolar mensaje para {recipient_id}.")
    return False

async def send_pending_messages(handler):
    """
    Envía mensajes pendientes al cliente recién conectado/reconectado.
    Prioriza mensajes de usuario sobre notificaciones del sistema.
    Optimizado: envío concurrente limitado para media/baja prioridad y requeue batch en fallo.
    """
    if not handler.client_id:
        return

    pending_messages = await get_pending_messages(handler.client_id, delete_queue=True)
    if not pending_messages:
        return

    logging.info(f"[PENDING-MSG] Enviando {len(pending_messages)} mensajes pendientes a {handler.client_id}.")

    high_priority_msgs: list = []
    medium_priority_msgs: list = []
    low_priority_msgs: list = []

    for msg_payload in pending_messages:
        if not isinstance(msg_payload, dict):
            high_priority_msgs.append(msg_payload); continue
        event_type = msg_payload.get('event', '')
        if event_type == 'new_message':
            high_priority_msgs.append(msg_payload)
        elif event_type in ['peer_disconnected_unexpectedly', 'peer_reconnect_failed']:
            peer_id = msg_payload.get('client_id')
            if peer_id:
                peer_data = await get_client_cached(peer_id)
                if peer_data and getattr(peer_data, 'connection_state', None) == ConnectionState.disconnected:
                    low_priority_msgs.append(msg_payload)
                else:
                    logging.info(f"[PENDING-MSG] Descartando notificación obsoleta de desconexión para {peer_id}")
            else:
                low_priority_msgs.append(msg_payload)
        elif event_type in ['jwt_updated', 'room_updated', 'peer_joined']:
            medium_priority_msgs.append(msg_payload)
        else:
            medium_priority_msgs.append(msg_payload)

    logging.debug(f"[PENDING-MSG] Clasificación para {handler.client_id}: {len(high_priority_msgs)} alta, {len(medium_priority_msgs)} media, {len(low_priority_msgs)} baja.")

    # Envío secuencial de alta prioridad (preserva orden)
    for msg in high_priority_msgs:
        try:
            await handler.write_message(msg)
        except Exception as e:
            logging.warning(f"[PENDING-MSG] Fallo enviando alta prioridad; reencolando resto: {e}")
            # Reencolar mensaje fallido + restantes altas + todas medias/bajas
            to_requeue = [msg] + high_priority_msgs[high_priority_msgs.index(msg)+1:] + medium_priority_msgs + low_priority_msgs
            if to_requeue:
                await batch_add_messages({handler.client_id: to_requeue})
            return

    # Función envío concurrente limitado
    async def _send_group(messages, label, concurrency=10):
        if not messages:
            return True
        sem = asyncio.Semaphore(concurrency)
        failed: list = []
        async def _send_one(m):
            async with sem:
                try:
                    await handler.write_message(m)
                except Exception as e:
                    failed.append(m)
        await asyncio.gather(*[_send_one(m) for m in messages])
        if failed:
            logging.warning(f"[PENDING-MSG] {len(failed)}/{len(messages)} mensajes {label} fallaron; se reencolan.")
            await batch_add_messages({handler.client_id: failed})
            return False
        return True

    if not await _send_group(medium_priority_msgs, 'media'):
        # Si fallan medias, no intentamos bajas (ya reencoladas)
        return

    # Pausa ligera antes de bajas
    if low_priority_msgs:
        await asyncio.sleep(0.3)
        await _send_group(low_priority_msgs, 'baja')

    logging.info(f"[PENDING-MSG] Finalizado envío de pendientes para {handler.client_id} (high={len(high_priority_msgs)}, med={len(medium_priority_msgs)}, low={len(low_priority_msgs)}).")
