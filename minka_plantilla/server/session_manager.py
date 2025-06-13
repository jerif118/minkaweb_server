"""
Módulo de gestión de sesiones para el servidor Minka WebSocket.

Este módulo coordina la inicialización del estado del servidor y la limpieza periódica
de sesiones y clientes expirados, actuando como punto central para la gestión del estado.
"""

import logging
import time

# Importar funciones de Redis desde session.py
from session import (
    get_session, set_session, delete_session, get_all_session_keys,
    get_client, set_client, delete_client, get_all_client_keys,
    add_message_to_queue, init_redis_client
)

# Importar configuraciones relacionadas con timeouts
from config import (
    SESSION_TIMEOUT, RECONNECT_GRACE_PERIOD, DOZE_TIMEOUT,
    WEB_RECONNECT_TIMEOUT
)

# Importar active_websockets
from handlers.websocket_handler import active_websockets

# Importar funciones de cleanup
from handlers.session_cleanup import cleanup_sessions, initialize_server_state_from_redis

# Funciones auxiliares para la gestión de sesiones
async def get_session_status(room_id):
    """
    Obtiene el estado detallado de una sesión específica.
    
    Args:
        room_id: ID de la sala a consultar
        
    Returns:
        dict: Información detallada sobre la sesión o None si no existe
    """
    session_data = await get_session(room_id)
    if not session_data:
        return None
    
    # Obtener información adicional de los clientes en la sesión
    clients_info = []
    for client_id in session_data.get('clients', []):
        client_data = await get_client(client_id)
        if client_data:
            is_connected = client_id in active_websockets
            clients_info.append({
                'client_id': client_id,
                'status': client_data.get('status', 'unknown'),
                'is_web': client_data.get('is_web', False),
                'is_connected': is_connected,
                'last_seen': client_data.get('last_seen', 0)
            })
    
    # Añadir información de los clientes a los datos de la sesión
    session_info = {
        **session_data,
        'clients_detailed': clients_info,
        'clients_count': len(clients_info),
        'has_connected_clients': any(c.get('is_connected', False) for c in clients_info)
    }
    
    return session_info

async def force_cleanup_session(room_id):
    """
    Fuerza la limpieza de una sesión específica y sus clientes.
    
    Args:
        room_id: ID de la sala a limpiar
        
    Returns:
        bool: True si la limpieza fue exitosa, False en caso contrario
    """
    try:
        session_data = await get_session(room_id)
        if not session_data:
            return False
        
        # Notificar a los clientes conectados
        for client_id in session_data.get('clients', []):
            if client_id in active_websockets:
                try:
                    active_websockets[client_id].write_message({
                        'info': 'Sesión terminada por administrador.',
                        'code': 'SESSION_FORCE_CLOSED'
                    })
                    active_websockets[client_id].close(code=1000, reason="Sesión forzada a cerrar")
                except Exception as e:
                    logging.error(f"Error al cerrar WebSocket de {client_id}: {e}")
            
            # Eliminar datos del cliente
            await delete_client(client_id, keep_message_queue=False)
        
        # Eliminar la sesión
        await delete_session(room_id)
        logging.info(f"[FORCE-CLEANUP] Sesión {room_id} forzada a cerrar y eliminar.")
        return True
    except Exception as e:
        logging.error(f"[FORCE-CLEANUP] Error al forzar limpieza de sesión {room_id}: {e}")
        return False

# Módulo también disponible para otras implementaciones específicas
__all__ = [
    'cleanup_sessions', 'initialize_server_state_from_redis',
    'get_session_status', 'force_cleanup_session'
]
