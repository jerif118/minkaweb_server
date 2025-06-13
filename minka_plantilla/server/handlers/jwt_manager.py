"""
Módulo para gestión de tokens JWT en el servidor WebSocket.
Maneja la generación, distribución y verificación de tokens JWT.
"""

import logging
import uuid
import time
import tornado.websocket

from session import (
    get_client, set_client, add_message_to_queue
)

from utils import crear_token_jwt

async def generate_and_distribute_jwts(session_data, client_data_of_joiner=None, active_websockets=None):
    """
    Genera (o regenera) JWTs para todos los clientes en una sesión y los distribuye.
    Actualiza 'current_jti' en los datos de cada cliente en Redis.
    """
    room_id_for_jwt = session_data.get('room_id')
    if not room_id_for_jwt:
        logging.error("[JWT-DIST] No se pudo obtener room_id de session_data para generar JWTs.")
        return

    # Primera pasada: asegurar que todos los clientes existan en Redis
    for participant_id in session_data.get('clients', []):
        if client_data_of_joiner and participant_id == client_data_of_joiner.get('client_id'):
            # Ya tenemos los datos del cliente que se une
            continue
            
        participant_client_data = await get_client(participant_id)
        if not participant_client_data:
            # Crear un registro básico si no existe
            is_mobile = not participant_id.startswith('web-')
            logging.info(f"[JWT-DIST] Creando registro base para cliente {participant_id} (móvil: {is_mobile}) en sala {room_id_for_jwt}")
            participant_client_data = {
                'client_id': participant_id,
                'room_id': room_id_for_jwt,
                'status': 'pending',
                'is_web': not is_mobile,
                'is_mobile': is_mobile,
                'last_seen': time.time()
            }
            await set_client(participant_id, participant_client_data)
    
    # Segunda pasada: generar y distribuir los JWTs
    for participant_id in session_data.get('clients', []):
        # Si estamos en el flujo de "join" y tenemos los datos del cliente que se une, usar esos
        if client_data_of_joiner and participant_id == client_data_of_joiner.get('client_id'):
            participant_client_data = client_data_of_joiner
        else:
            participant_client_data = await get_client(participant_id)

        if not participant_client_data:
            logging.warning(f"[JWT-DIST] Aún no se encontraron datos para el cliente {participant_id} en sala {room_id_for_jwt}.")
            continue

        # Identificar si es un cliente móvil
        is_mobile = participant_client_data.get('is_mobile', not participant_id.startswith('web-'))
        
        # Generar un nuevo JTI para el token
        new_jti = uuid.uuid4().hex
        
        # Crear el token JWT
        token = crear_token_jwt(participant_id, room_id_for_jwt, jti=new_jti)

        if token:
            # Actualizar el JTI actual del cliente en Redis
            participant_client_data['current_jti'] = new_jti
            # Si no estamos usando la referencia client_data_of_joiner, necesitamos guardar aquí
            if not (client_data_of_joiner and participant_id == client_data_of_joiner.get('client_id')):
                await set_client(participant_id, participant_client_data)
            
            # Mensaje de token universal para ambos tipos de cliente
            token_message = {
                'event': 'jwt_updated',
                'jwt_token': token,
                'client_id': participant_id,
                'room_id': room_id_for_jwt,
                'status': 'success',
                'timestamp': time.time(),
                'is_mobile': is_mobile
            }
            
            # Priorizar la entrega inmediata para ambos tipos de clientes
            if active_websockets and participant_id in active_websockets:
                try:
                    await active_websockets[participant_id].write_message(token_message)
                    logging.info(f"[JWT-DIST] Token JWT actualizado y enviado a {participant_id} (móvil: {is_mobile}) en sala {room_id_for_jwt}.")
                except tornado.websocket.WebSocketClosedError:
                    logging.warning(f"[JWT-DIST] Conexión cerrada para {participant_id} al enviar JWT. Se encolará.")
                    await add_message_to_queue(participant_id, token_message)
            else:
                logging.info(f"[JWT-DIST] Cliente {participant_id} (móvil: {is_mobile}) no conectado. JWT encolado.")
                await add_message_to_queue(participant_id, token_message)
        else:
            logging.error(f"[JWT-DIST] No se pudo generar JWT para {participant_id} (móvil: {is_mobile}) en sala {room_id_for_jwt}.")
