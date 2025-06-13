"""
Módulo para procesamiento de mensajes en el servidor WebSocket.
Contiene funciones para manejar diferentes tipos de mensajes y acciones.
"""

import logging
import time
import jwt
import uuid
import tornado.websocket

from session import (
    get_session, set_session, delete_session,
    get_client, set_client, delete_client,
    add_jti_to_blacklist, add_message_to_queue, get_pending_messages
)

from config import JWT_ALGORITHM, JWT_SECRET_KEY, JWT_EXPIRATION_DELTA_SECONDS

from handlers.room_manager import cleanup_client_and_session, send_error_and_close

async def process_message(handler, message_data, active_websockets):
    """
    Procesa un mensaje recibido del cliente y ejecuta la acción correspondiente.
    """
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

    # Lógica de limpieza de sesión y cliente
    await cleanup_client_and_session(handler, is_leaving_normally=True, active_websockets=active_websockets)
    
    await handler.write_message({'event': 'left_room', 'message': 'Has salido de la sala.'})
    handler.close(code=1000, reason="Client initiated leave")

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
    
    client_data['status'] = 'dozing'
    client_data['dozing'] = True
    client_data['doze_start'] = time.time()
    client_data['doze_id'] = uuid.uuid4().hex
    await set_client(handler.client_id, client_data)

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
    """Envía un mensaje a los otros participantes en la sala."""
    if not handler.client_id or not handler.room_id:
        return

    session_data = await get_session(handler.room_id)
    if not session_data:
        logging.warning(f"[BROADCAST] {handler.client_id} - No se encontró sala {handler.room_id} para broadcast.")
        await handler.write_message({'error': 'Sala no encontrada.', 'code': 'ROOM_NOT_FOUND_FOR_BROADCAST'})
        return

    message_id = f"{time.time()}-{uuid.uuid4().hex[:8]}"
    payload_to_send = {
        'event': 'new_message',
        'sender_id': handler.client_id,
        'room_id': handler.room_id,
        'message_content': content,
        'message_type': message_type,
        'timestamp': time.time(),
        'message_id': message_id
    }

    recipients_notified = 0
    for recipient_id in session_data.get('clients', []):
        if recipient_id == handler.client_id:
            continue

        delivered = await send_message_to_peer(handler.client_id, recipient_id, payload_to_send, active_websockets)
        if delivered:
            recipients_notified += 1
    
    if recipients_notified > 0:
        await handler.write_message({
            'event': 'message_sent_confirmation',
            'message_id': message_id,
            'recipients_attempted': len(session_data.get('clients', [])) - 1,
            'recipients_delivered_directly': recipients_notified,
            'info': 'Mensaje enviado a los participantes conectados.'
        })
        logging.info(f"[BROADCAST] Mensaje {message_id} de {handler.client_id} enviado en sala {handler.room_id}.")
    else:
        logging.info(f"[BROADCAST] Mensaje {message_id} de {handler.client_id} en sala {handler.room_id}. No hubo destinatarios directos.")

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
    recipient_data = await get_client(recipient_id)
    if not recipient_data:
        logging.warning(f"[SEND-PEER] Destinatario {recipient_id} no encontrado en Redis.")
        return False

    is_recipient_dozing = recipient_data.get('status') == 'dozing'
    
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
    """
    if not handler.client_id:
        return

    pending_messages = await get_pending_messages(handler.client_id, delete_queue=True)
    if pending_messages:
        logging.info(f"[PENDING-MSG] Enviando {len(pending_messages)} mensajes pendientes a {handler.client_id}.")
        
        # Clasificar mensajes por tipo y prioridad
        high_priority_msgs = []  # Mensajes de usuario y contenido real
        medium_priority_msgs = []  # Confirmaciones y eventos de sistema importantes
        low_priority_msgs = []  # Notificaciones que pueden esperar (ej: desconexiones)
        
        for msg_payload in pending_messages:
            if not isinstance(msg_payload, dict):
                high_priority_msgs.append(msg_payload)
                continue
                
            event_type = msg_payload.get('event', '')
            
            # Mensajes de usuario tienen máxima prioridad
            if event_type == 'new_message':
                high_priority_msgs.append(msg_payload)
            # Notificaciones de desconexión tienen baja prioridad
            elif event_type in ['peer_disconnected_unexpectedly', 'peer_reconnect_failed']:
                # Solo incluir si la desconexión aún es relevante (peer no reconectado)
                peer_id = msg_payload.get('client_id')
                if peer_id:
                    peer_data = await get_client(peer_id)
                    if peer_data and peer_data.get('status') == 'pending_reconnect':
                        low_priority_msgs.append(msg_payload)
                    else:
                        logging.info(f"[PENDING-MSG] Descartando notificación obsoleta de desconexión para {peer_id} (ya reconectado)")
                else:
                    low_priority_msgs.append(msg_payload)
            # Actualización de JWT y eventos importantes en prioridad media
            elif event_type in ['jwt_updated', 'room_updated', 'peer_joined']:
                medium_priority_msgs.append(msg_payload)
            # Otros eventos en prioridad media por defecto
            else:
                medium_priority_msgs.append(msg_payload)
        
        # Log detallado de la clasificación
        logging.debug(f"[PENDING-MSG] Clasificación para {handler.client_id}: {len(high_priority_msgs)} alta, " +
                     f"{len(medium_priority_msgs)} media, {len(low_priority_msgs)} baja prioridad.")
        
        # Función para enviar mensajes con manejo de errores
        async def send_with_error_handling(messages, priority_name):
            for msg in messages:
                try:
                    await handler.write_message(msg)
                    logging.debug(f"[PENDING-MSG] Enviado mensaje {priority_name} prioridad a {handler.client_id}")
                except tornado.websocket.WebSocketClosedError:
                    logging.warning(f"[PENDING-MSG] WebSocket cerrado para {handler.client_id} enviando {priority_name} prioridad")
                    # Reencolar los mensajes no entregados
                    remaining = [msg] + [m for m in messages if m != msg]
                    for remaining_msg in remaining:
                        await add_message_to_queue(handler.client_id, remaining_msg)
                    return False
                except Exception as e:
                    logging.error(f"[PENDING-MSG] Error enviando a {handler.client_id}: {e}")
                    return False
            return True
        
        # Enviar en orden de prioridad
        if not await send_with_error_handling(high_priority_msgs, "alta"):
            return
        
        if not await send_with_error_handling(medium_priority_msgs, "media"):
            return
            
        # Pequeña pausa antes de enviar mensajes de baja prioridad
        # para evitar que interfieran con la interacción inmediata
        if low_priority_msgs:
            await asyncio.sleep(0.5)
            await send_with_error_handling(low_priority_msgs, "baja")
            
        logging.info(f"[PENDING-MSG] Finalizado envío de mensajes pendientes para {handler.client_id}.")
