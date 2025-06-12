"""
Módulo para manejo de operaciones de salas en el servidor WebSocket.
Contiene funciones para crear, unir y gestionar salas y clientes.
"""

import logging
import time
import uuid
import tornado.websocket
import asyncio  # Importación faltante que causa el error

from session import (
    get_session, set_session, delete_session,
    get_client, set_client, delete_client,
    add_jti_to_blacklist, is_jti_blacklisted,
    add_message_to_queue
)

from utils import (
    crear_token_jwt, verificar_token_jwt, verificar_token_jwt_async,
    generar_identificador_unico, generar_password_sala
)

from handlers.jwt_manager import generate_and_distribute_jwts

async def send_error_and_close(handler, error_message, error_code_str, close_code=1008):
    """Envía un mensaje de error JSON al cliente y luego cierra la conexión."""
    try:
        await handler.write_message({'error': error_message, 'code': error_code_str})
    except tornado.websocket.WebSocketClosedError:
        pass
    finally:
        if handler.ws_connection:  # Simplificado: el objeto ws_connection ya es suficiente
            # Marcar como cierre intencional para evitar procesamiento adicional en on_close
            handler.intentional_disconnect_flag = True
            handler.close(code=close_code, reason=error_code_str[:120])

async def handle_create_room(handler, creator_client_id, active_websockets):
    """Maneja la lógica para crear una nueva sala."""
    handler.client_id = creator_client_id
    handler.room_id = generar_identificador_unico()
    room_password = generar_password_sala()

    client_data = {
        'client_id': handler.client_id,
        'room_id': handler.room_id,
        'status': 'waiting_for_peer',
        'is_initiator': True,
        'is_web': handler.client_id.startswith("web-"),
        'last_seen': time.time(),
        'current_jti': None
    }
    await set_client(handler.client_id, client_data)

    session_data = {
        'room_id': handler.room_id,
        'password': room_password,
        'clients': [handler.client_id],
        'initiator_id': handler.client_id,
        'status': 'waiting_for_peer',
        'created_at': time.time(),
        'last_activity': time.time(),
        'has_dozing_client': False
    }
    await set_session(handler.room_id, session_data)
    
    active_websockets[handler.client_id] = handler
    handler.is_authenticated = True

    await handler.write_message({
        'event': 'room_created',
        'room_id': handler.room_id,
        'password': room_password,
        'client_id': handler.client_id
    })
    logging.info(f"[WS-OPEN] Sala {handler.room_id} creada por {handler.client_id}. Contraseña: {room_password}. Esperando peer.")

async def handle_join_room(handler, joiner_client_id, room_id_to_join, password_to_join, active_websockets):
    """Maneja la lógica para que un cliente se una a una sala existente."""
    if not room_id_to_join or not password_to_join:
        logging.warning(f"[WS-OPEN] {joiner_client_id} - Faltan room_id o password para unirse.")
        await send_error_and_close(handler, 'Faltan room_id o password para unirse.', 'JOIN_MISSING_PARAMS')
        return

    session_to_join = await get_session(room_id_to_join)

    if not session_to_join or session_to_join.get("password") != password_to_join:
        logging.warning(f"[WS-OPEN] {joiner_client_id} - Intento de unirse a sala {room_id_to_join} con ID/password incorrecto.")
        await send_error_and_close(handler, 'ID de sala o contraseña incorrectos.', 'INVALID_ROOM_CREDENTIALS')
        return

    if len(session_to_join.get('clients', [])) >= 2 and not session_to_join.get('has_dozing_client'):
        logging.warning(f"[WS-OPEN] {joiner_client_id} - Intento de unirse a sala {room_id_to_join} que ya está llena.")
        await send_error_and_close(handler, 'La sala ya está llena.', 'ROOM_FULL')
        return

    # Configurar el handler primero para asegurar que esté en active_websockets
    handler.client_id = joiner_client_id
    handler.room_id = room_id_to_join
    handler.is_authenticated = True
    active_websockets[handler.client_id] = handler
    
    # Lógica para manejar si el que se une es un cliente que estaba en "doze"
    is_resuming_doze = False
    if session_to_join.get('has_dozing_client') and session_to_join.get('doze_client_id') == handler.client_id:
        logging.info(f"[WS-JOIN] Cliente {handler.client_id} (estaba en doze) volviendo a sala {handler.room_id}.")
        session_to_join['has_dozing_client'] = False
        session_to_join.pop('doze_client_id', None)
        session_to_join.pop('doze_start_time', None)
        is_resuming_doze = True

    # Determinar si es un cliente móvil
    is_mobile = not handler.client_id.startswith("web-")
    
    client_data = await get_client(handler.client_id) or {}
    client_data.update({
        'client_id': handler.client_id,
        'room_id': handler.room_id,
        'status': 'connected',
        'is_initiator': client_data.get('is_initiator', False),
        'is_web': not is_mobile,
        'is_mobile': is_mobile,
        'last_seen': time.time(),
        'dozing': False
    })
    client_data.pop('doze_start', None)
    client_data.pop('doze_id', None)
    
    if handler.client_id not in session_to_join.get('clients', []):
        session_to_join.get('clients', []).append(handler.client_id)
    
    session_to_join['status'] = 'active'
    session_to_join['last_activity'] = time.time()
    
    # Guardar los datos antes de generar JWTs
    await set_client(handler.client_id, client_data)
    await set_session(handler.room_id, session_to_join)
    
    # Generar y distribuir JWTs para ambos clientes
    await generate_and_distribute_jwts(session_to_join, client_data_of_joiner=client_data, active_websockets=active_websockets)
    
    # Mensaje de confirmación universal para ambos tipos de cliente
    join_confirmation = {
        'event': 'joined_room' if not is_resuming_doze else 'resumed_session',
        'room_id': handler.room_id,
        'client_id': handler.client_id,
        'status': 'success',
        'password': session_to_join.get('password'),
        'clients': session_to_join.get('clients', []),
        'is_initiator': client_data.get('is_initiator', False),
        'is_mobile': is_mobile,
        'timestamp': time.time()
    }
    
    logging.info(f"[WS-JOIN] Cliente {handler.client_id} (móvil: {is_mobile}) {'resumió' if is_resuming_doze else 'se unió a'} sala {handler.room_id}.")
    
    try:
        await handler.write_message(join_confirmation)
    except tornado.websocket.WebSocketClosedError:
        logging.error(f"[WS-JOIN] Error al enviar confirmación a {handler.client_id}: WebSocket cerrado.")
        return
    
    # Notificar al otro participante
    other_client_ids = [cid for cid in session_to_join.get('clients', []) if cid != handler.client_id]
    for other_id in other_client_ids:
        if other_id in active_websockets:
            try:
                await active_websockets[other_id].write_message({
                    'event': 'peer_joined' if not is_resuming_doze else 'peer_resumed',
                    'peer_client_id': handler.client_id,
                    'room_id': handler.room_id,
                    'is_mobile': is_mobile
                })
            except tornado.websocket.WebSocketClosedError:
                logging.warning(f"[WS-NOTIFY] Conexión cerrada para {other_id} al notificar unión de {handler.client_id}.")
        else:
            await add_message_to_queue(other_id, {
                'event': 'peer_joined' if not is_resuming_doze else 'peer_resumed',
                'peer_client_id': handler.client_id,
                'room_id': handler.room_id,
                'is_mobile': is_mobile,
                'timestamp': time.time()
            })

async def handle_reconnection_with_jwt(handler, token, client_id_from_param, active_websockets):
    """Maneja la lógica de reconexión de un cliente usando un token JWT."""
    # Verificar el token JWT usando la función asíncrona
    async def check_jti_blacklisted(jti):
        return await is_jti_blacklisted(jti)
    
    payload = await verificar_token_jwt_async(token, check_jti_blacklisted)

    if not payload:
        logging.warning(f"[WS-OPEN] {client_id_from_param} - Token JWT inválido o expirado al reconectar.")
        await send_error_and_close(handler, 'Token inválido o expirado.', 'INVALID_TOKEN')
        return

    jwt_client_id = payload.get('client_id')
    jwt_room_id = payload.get('room_id')

    if client_id_from_param and client_id_from_param != jwt_client_id:
        logging.warning(f"[WS-OPEN] Discrepancia de client_id: param ({client_id_from_param}) vs JWT ({jwt_client_id}).")
        await send_error_and_close(handler, 'client_id no coincide con JWT.', 'CLIENT_ID_MISMATCH')
        return
    
    handler.client_id = jwt_client_id
    handler.room_id = jwt_room_id

    client_data = await get_client(handler.client_id)
    if not client_data:
        logging.warning(f"[WS-OPEN] {handler.client_id} - JWT válido pero cliente no encontrado en Redis.")
        await send_error_and_close(handler, 'Token válido pero cliente no registrado.', 'CLIENT_NOT_FOUND')
        return

    if client_data.get('room_id') != handler.room_id:
        logging.warning(f"[WS-OPEN] {handler.client_id} - JWT válido pero sala no coincide.")
        await send_error_and_close(handler, 'Token no corresponde a la sala del cliente.', 'ROOM_MISMATCH')
        return

    # Verificar JTI
    stored_jti = client_data.get('current_jti')
    jwt_jti = payload.get('jti')
    if stored_jti and jwt_jti and stored_jti != jwt_jti:
        logging.warning(f"[WS-OPEN] {handler.client_id} intentó usar un JTI ({jwt_jti}) diferente al actual ({stored_jti}).")
        await send_error_and_close(handler, 'Token JWT no es el último emitido para este cliente.', 'INVALID_TOKEN_JTI')
        return

    # Actualizar estado del cliente y sesión
    client_data['status'] = 'connected'
    client_data['last_seen'] = time.time()
    await set_client(handler.client_id, client_data)

    session_data = await get_session(handler.room_id)
    if not session_data:
        logging.warning(f"[WS-OPEN] {handler.client_id} intentó unirse a sala {handler.room_id} inexistente con JWT.")
        await send_error_and_close(handler, 'Sala no existe.', 'ROOM_NOT_FOUND')
        return
    
    if handler.client_id not in session_data.get('clients', []):
        session_data.get('clients', []).append(handler.client_id)
    session_data['last_activity'] = time.time()
    session_data['has_dozing_client'] = False
    session_data.pop('doze_client_id', None)
    await set_session(handler.room_id, session_data)

    active_websockets[handler.client_id] = handler
    handler.is_authenticated = True
    await handler.write_message({'event': 'reconnected', 'client_id': handler.client_id, 'room_id': handler.room_id})
    logging.info(f"[WS-OPEN] Cliente {handler.client_id} reconectado y validado en sala {handler.room_id}.")

    # Enviar mensajes pendientes
    from handlers.message_processor import send_pending_messages
    await send_pending_messages(handler)

async def cleanup_client_and_session(handler, is_leaving_normally=False, active_websockets=None):
    """Limpia los datos del cliente y actualiza/elimina la sesión según corresponda."""
    if not handler.client_id or not handler.room_id:
        logging.error("[CLEANUP-CS] Faltan client_id o room_id para limpieza.")
        return

    client_data = await get_client(handler.client_id)
    if client_data:
        if client_data.get('status') == 'dozing':
            logging.info(f"[CLOSE] Cliente {handler.client_id} ya estaba en doze. Manteniendo estado doze.")
        elif client_data.get('status') == 'connected':
            client_data['status'] = 'pending_reconnect'
            client_data['pending_since'] = time.time()
            await set_client(handler.client_id, client_data)
            logging.info(f"[CLOSE] Cliente {handler.client_id} marcado como 'pending_reconnect'.")
        else:
            logging.info(f"[CLOSE] Cliente {handler.client_id} estaba en estado '{client_data.get('status')}', no se marca como pending_reconnect.")

        # Notificar al peer sobre la desconexión inesperada
        session_data = await get_session(handler.room_id)
        if session_data and active_websockets:
            for peer_id in session_data.get('clients', []):
                if peer_id != handler.client_id:
                    disconnect_notification = {
                        'event': 'peer_disconnected_unexpectedly',
                        'client_id': handler.client_id,
                        'message': f'Usuario {handler.client_id} perdió conexión. Esperando reconexión…',
                        'timestamp': time.time(),
                        'priority': 'low'  # Marcar como baja prioridad para procesamiento
                    }
                    
                    # Verificar si el peer está en el diccionario active_websockets
                    if peer_id in active_websockets:
                        try:
                            # Añadir un pequeño retraso antes de enviar la notificación de desconexión
                            # para que no interfiera con mensajes inmediatos post-reconexión
                            await asyncio.sleep(0.5)
                            
                            # Verificar nuevamente que el peer sigue en active_websockets después del sleep
                            # ya que podría haber sido eliminado durante el sleep
                            if peer_id in active_websockets:
                                await active_websockets[peer_id].write_message(disconnect_notification)
                            else:
                                # Si ya no está en el diccionario, encolar el mensaje
                                logging.debug(f"[CLOSE] Peer {peer_id} ya no está en active_websockets, encolando notificación.")
                                await add_message_to_queue(peer_id, disconnect_notification)
                        except tornado.websocket.WebSocketClosedError:
                            await add_message_to_queue(peer_id, disconnect_notification)
                        except KeyError:
                            # En caso de que el peer sea eliminado justo entre la verificación y el acceso
                            logging.warning(f"[CLOSE] KeyError al intentar enviar mensaje a {peer_id}. Encolando en su lugar.")
                            await add_message_to_queue(peer_id, disconnect_notification)
                        except Exception as e:
                            # Capturar cualquier otra excepción para evitar que interrumpa el proceso
                            logging.error(f"[CLOSE] Error al enviar notificación a {peer_id}: {e}")
                            await add_message_to_queue(peer_id, disconnect_notification)
                    else:
                        await add_message_to_queue(peer_id, disconnect_notification)
    else:
        logging.warning(f"[CLOSE] No se encontraron datos para {handler.client_id} tras desconexión inesperada.")
