"""
Paquete de handlers para el servidor Minka.

Contiene todas las clases de manejo de rutas HTTP y WebSocket.
"""

# Importar handlers base y HTTP
from handlers.base import BaseHandler
from handlers.http_handlers import MainHandler, HealthHandler, MonitorHandler

# Importar handler WebSocket y su diccionario de conexiones activas
from handlers.websocket_handler import WebSocketHandler, active_websockets

# Importar funciones de manejo de sesiones
from handlers.session_cleanup import cleanup_sessions, initialize_server_state_from_redis

# Importar funciones de manejo de salas
from handlers.room_manager import handle_create_room, handle_join_room, handle_reconnection_with_jwt, send_error_and_close

# Importar funciones de procesamiento de mensajes
from handlers.message_processor import process_message, send_pending_messages, broadcast_message

# Importar funciones de manejo de JWT
from handlers.jwt_manager import generate_and_distribute_jwts

__all__ = [
    # Handlers base y HTTP
    'BaseHandler', 'MainHandler', 'HealthHandler', 'MonitorHandler',
    
    # Handler WebSocket y diccionario de conexiones
    'WebSocketHandler', 'active_websockets',
    
    # Funciones de manejo de sesiones
    'cleanup_sessions', 'initialize_server_state_from_redis',
    
    # Funciones de manejo de salas
    'handle_create_room', 'handle_join_room', 'handle_reconnection_with_jwt',
    
    # Funciones de procesamiento de mensajes
    'process_message', 'send_pending_messages', 'broadcast_message',
    
    # Funciones de manejo de JWT
    'generate_and_distribute_jwts'
]
