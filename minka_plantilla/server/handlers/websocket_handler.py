import tornado.web
import tornado.websocket
import tornado.escape
import json
import time
import logging
import asyncio  # Añadir esta importación
from config import RECONNECT_GRACE_PERIOD  # Nuevo import para deadlines

# Importaciones de módulos de la aplicación
from session import (
    get_session, set_session, delete_session,
    get_client, set_client, delete_client,
    add_jti_to_blacklist, is_jti_blacklisted,
    add_message_to_queue, get_pending_messages
)

# Importar los nuevos módulos de manejo especializado
from handlers.room_manager import (
    handle_create_room, handle_join_room, handle_reconnection_with_jwt,
    send_error_and_close, cleanup_client_and_session
)
from handlers.message_processor import process_message, send_pending_messages
from handlers.jwt_manager import generate_and_distribute_jwts
from handlers.state_model import (
    ConnectionState, ParticipationState, PresenceState,
    update_client_states, get_client_cached
)

# Diccionario global para mantener los websockets activos
active_websockets = {}

class WebSocketHandler(tornado.websocket.WebSocketHandler):
    """
    Maneja las conexiones WebSocket, la autenticación de clientes, la creación/unión a salas,
    y el intercambio de mensajes entre participantes.
    """
    
    def check_origin(self, origin):
        """
        Permite conexiones cross-origin. En producción, debería ser más restrictivo.
        """
        # TODO: Implementar una política de origen más segura para producción.
        return True
    
    async def open(self, *args, **kwargs):
        """
        Se invoca cuando se establece una nueva conexión WebSocket.
        Maneja la lógica de conexión inicial, incluyendo la autenticación por JWT
        o la creación/unión a una sala.
        """
        self.client_id = None
        self.room_id = None
        self.is_authenticated = False
        self.intentional_disconnect_flag = False

        client_id_param = self.get_argument("client_id", None)
        action_param = self.get_argument("action", None)
        jwt_token_param = self.get_argument("jwt_token", None) or self.get_argument("jwt", None)

        # También verificar headers para JWT
        if not jwt_token_param and self.request.headers.get("Sec-WebSocket-Protocol"):
            protocols = [p.strip() for p in self.request.headers.get("Sec-WebSocket-Protocol", "").split(',')]
            for p_val in protocols:
                if len(p_val) > 30 and '.' in p_val and not p_val.lower().startswith("action-"):
                    jwt_token_param = p_val
                    logging.debug(f"[WS-OPEN] Token JWT encontrado en Sec-WebSocket-Protocol: {jwt_token_param[:20]}...")
                    break
        
        if not client_id_param:
            logging.warning("[WS-OPEN] Conexión rechazada: client_id es obligatorio.")
            await send_error_and_close(self, 'client_id es obligatorio.', 'CLIENT_ID_REQUIRED')
            return

        # Flujo 1: Reconexión con JWT
        if jwt_token_param:
            await handle_reconnection_with_jwt(self, jwt_token_param, client_id_param, active_websockets)
            return

        # Flujo 2: Nueva conexión (crear o unirse a sala)
        if not action_param:
            logging.warning(f"[WS-OPEN] {client_id_param} - Conexión sin acción. Se requiere 'create' o 'join'.")
            await send_error_and_close(self, 'Acción requerida (create o join).', 'ACTION_REQUIRED')
            return

        # Al inicio marcamos estado connecting
        if client_id_param:
            await update_client_states(client_id_param, connection_state=ConnectionState.connecting, participation_state=ParticipationState.joining, presence_state=PresenceState.foreground, touch_last_seen=True)

        if action_param == "create":
            # Tras crear sala, actualizar estados unificados
            await handle_create_room(self, client_id_param, active_websockets)
            await update_client_states(client_id_param, connection_state=ConnectionState.connected, participation_state=ParticipationState.waiting_peer, presence_state=PresenceState.foreground, extra_updates={'room_id': self.room_id, 'reconnect_deadline': time.time() +  RECONNECT_GRACE_PERIOD})
        elif action_param == "join":
            # Unirse a sala
            room_id_join = self.get_argument("room_id", None)
            room_password_join = self.get_argument("room_password", None) or self.get_argument("password", None)
            await handle_join_room(self, client_id_param, room_id_join, room_password_join, active_websockets)
            await update_client_states(client_id_param, connection_state=ConnectionState.connected, participation_state=ParticipationState.active, presence_state=PresenceState.foreground, extra_updates={'room_id': self.room_id, 'reconnect_deadline': time.time() +  RECONNECT_GRACE_PERIOD})
        else:
            logging.warning(f"[WS-OPEN] {client_id_param} - Acción desconocida: {action_param}.")
            await send_error_and_close(self, f"Acción desconocida: {action_param}.", 'UNKNOWN_ACTION')
    
    async def on_message(self, message_str):
        """
        Se invoca cuando se recibe un mensaje del cliente WebSocket.
        """
        if not self.is_authenticated or not self.client_id or not self.room_id:
            logging.warning(f"[ON-MSG] Mensaje recibido de WebSocket no autenticado o no identificado. Ignorando.")
            return

        logging.debug(f"[ON-MSG] Raw de {self.client_id} en {self.room_id}: {message_str[:250]}")

        try:
            message_data = tornado.escape.json_decode(message_str)
        except json.JSONDecodeError:
            logging.warning(f"[ON-MSG] JSON inválido de {self.client_id}: {message_str[:100]}")
            await send_error_and_close(self, 'JSON inválido.', 'INVALID_JSON_FORMAT')
            return

        # Touch last_seen de forma liviana sin recalcular (solo si autenticado)
        if self.client_id:
            asyncio.create_task(update_client_states(self.client_id, touch_last_seen=True))

        # Procesar el mensaje usando el módulo especializado
        await process_message(self, message_data, active_websockets)

    def on_close(self):
        """
        Se invoca cuando la conexión WebSocket se cierra.
        Esta es una función sincrónica que inicia una tarea asíncrona para la limpieza.
        """
        client_id_log = getattr(self, 'client_id', 'N/A_NO_ID')
        room_id_log = getattr(self, 'room_id', 'N/A_NO_ROOM')
        
        # Determinar si es un cliente móvil
        is_mobile = client_id_log and not client_id_log.startswith("web-")
        
        # Información más detallada sobre la desconexión
        close_code = self.close_code if hasattr(self, 'close_code') else 'N/A'
        close_reason = self.close_reason if hasattr(self, 'close_reason') else 'N/A'
        
        logging.info(f"[CLOSE] Conexión WebSocket cerrada para {client_id_log} (móvil: {is_mobile}) en {room_id_log}. "
                    f"Intencional: {self.intentional_disconnect_flag}, Código: {close_code}, Razón: {close_reason}")

        if self.client_id in active_websockets and active_websockets[self.client_id] == self:
            del active_websockets[self.client_id]
            logging.debug(f"[CLOSE] Handler removido de active_websockets para {self.client_id} (móvil: {is_mobile}).")
        
        if self.intentional_disconnect_flag:
            logging.info(f"[CLOSE] Cierre fue intencional para {self.client_id} (móvil: {is_mobile}). Limpieza principal ya realizada.")
            return

        # Desconexión Inesperada
        if not self.client_id or not self.room_id:
            logging.warning(f"[CLOSE] Desconexión inesperada ANTES de asignación completa de client/room.")
            return

        # Actualizar estados unificados según tipo de cierre
        if self.client_id:
            if self.intentional_disconnect_flag:
                # Cierre voluntario -> connection_state.closed (participation se mantiene si leave ya lo cambió)
                asyncio.create_task(update_client_states(self.client_id, connection_state=ConnectionState.closed, touch_last_seen=False))
            else:
                # Desconexión inesperada -> disconnected
                asyncio.create_task(update_client_states(self.client_id, connection_state=ConnectionState.disconnected, touch_last_seen=False, extra_updates={'reconnect_deadline': time.time() + RECONNECT_GRACE_PERIOD}))

        # Crear una tarea separada para la limpieza asíncrona con prioridad equivalente para ambos tipos
        tornado.ioloop.IOLoop.current().add_callback(
            lambda: asyncio.create_task(
                cleanup_client_and_session(self, is_leaving_normally=False, active_websockets=active_websockets)
            )
        )

# Exportar la clase y el diccionario de websockets activos
__all__ = ['WebSocketHandler', 'active_websockets']