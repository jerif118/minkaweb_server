"""
Módulo para limpieza y mantenimiento de sesiones del servidor.
Maneja la limpieza periódica de sesiones y clientes expirados.
"""

import time
import logging
import asyncio

# Importar funciones de Redis
from session import (
    get_session, set_session, delete_session, get_all_session_keys,
    get_client, set_client, delete_client, get_all_client_keys,
    add_message_to_queue
)

# Importar configuraciones
from config import (
    SESSION_TIMEOUT, RECONNECT_GRACE_PERIOD, DOZE_TIMEOUT,
    WEB_RECONNECT_TIMEOUT
)

# Referencia a active_websockets
# Nota: Esto crea una referencia circular, pero es necesario para notificar a clientes activos
from handlers.websocket_handler import active_websockets
from handlers.state_model import (
    ConnectionState, ParticipationState, PresenceState,
    get_client_cached, update_client_states
)

async def cleanup_sessions():
    """
    Limpia sesiones y clientes expirados en Redis.
    - Sesiones vacías o inactivas
    - Clientes que no se reconectaron
    - Actualiza estado de clientes y sesiones
    """
    now = time.time()
    expired_session_ids = []
    
    # Procesar todas las sesiones
    all_s_keys = await get_all_session_keys()
    for room_id_key in all_s_keys:
        session_info = await get_session(room_id_key)
        if not session_info:
            continue

        has_dozing_mobile_in_session = False
        active_clients_in_session = []  # Clientes que aún existen en Redis
        
        # Verificar cada cliente en la sesión
        for cid_in_sess in session_info.get('clients', []):
            client_info_sess = await get_client(cid_in_sess)
            if client_info_sess:
                active_clients_in_session.append(cid_in_sess)
                # Detectar si hay un cliente móvil en modo doze
                if not cid_in_sess.startswith("web-") and client_info_sess.get('status') == 'dozing':
                    has_dozing_mobile_in_session = True
                    break
        
        # Actualizar la lista de clientes en la sesión con los activos
        if len(active_clients_in_session) != len(session_info.get('clients', [])):
            session_info['clients'] = active_clients_in_session
        
        # Si la sesión está vacía después de filtrar, marcarla para eliminación
        if not session_info['clients']:
            expired_session_ids.append(room_id_key)
            logging.info(f"[CLEANUP] Sesión {room_id_key} marcada para expirar (sin clientes válidos).")
            continue

        # Usar timeout diferente si hay un cliente móvil en doze
        timeout_for_this_session = DOZE_TIMEOUT if has_dozing_mobile_in_session else SESSION_TIMEOUT
        if now - session_info.get('last_activity', now) > timeout_for_this_session:
            logging.info(f"[CLEANUP] Sesión {room_id_key} marcada para expirar (timeout: {timeout_for_this_session}s). Dozing mobile: {has_dozing_mobile_in_session}")
            expired_session_ids.append(room_id_key)
        else:
            # Si la sesión no expira, pero se modificó la lista de clientes, guardarla
            if len(active_clients_in_session) != len(session_info.get('clients', [])):
                await set_session(room_id_key, session_info)

    # Expirar clientes que no se reconectaron
    all_c_keys = await get_all_client_keys()
    for client_id_key in all_c_keys:
        client_info_cleanup = await get_client_cached(client_id_key)
        if not client_info_cleanup:
            continue
        conn_state = client_info_cleanup.connection_state.value if hasattr(client_info_cleanup, 'connection_state') else None
        part_state = client_info_cleanup.participation_state.value if hasattr(client_info_cleanup, 'participation_state') else None
        presence_state = client_info_cleanup.presence_state.value if hasattr(client_info_cleanup, 'presence_state') else None

        # Usar deadlines preferidos
        if conn_state == 'disconnected' and part_state in ['active', 'waiting_peer']:
            deadline = client_info_cleanup.reconnect_deadline or (getattr(client_info_cleanup, 'last_seen', time.time()) + RECONNECT_GRACE_PERIOD)
            if time.time() > deadline:
                logging.info(f"[CLEANUP] Cliente {client_id_key} no reconectó (deadline alcanzado). Cerrando y marcando left/offline.")
                await update_client_states(client_id_key, connection_state=ConnectionState.closed, participation_state=ParticipationState.left, presence_state=PresenceState.offline, touch_last_seen=False)
        elif presence_state == 'doze':
            d_deadline = client_info_cleanup.doze_deadline or (getattr(client_info_cleanup, 'last_seen', time.time()) + DOZE_TIMEOUT)
            if time.time() > d_deadline:
                logging.info(f"[CLEANUP] Cliente {client_id_key} excedió doze_deadline. Marcando offline.")
                await update_client_states(client_id_key, presence_state=PresenceState.offline, connection_state=ConnectionState.closed, touch_last_seen=False)
    
async def initialize_server_state_from_redis():
    """
    Inicializa el estado del servidor desde Redis al arrancar.
    - Procesa clientes en estado conectado/waiting y los marca como pending_reconnect
    - Limpia clientes que llevan demasiado tiempo en pending_reconnect
    - Mantiene clientes en doze
    """
    logging.info("[INIT] Inicializando estado del servidor desde Redis...")
    now = time.time()
    
    client_keys_init = await get_all_client_keys()
    sessions_to_update_init = {}
    clients_to_delete_init = []

    # Procesar todos los clientes registrados
    for cid_key_init in client_keys_init:
        client_data_init = await get_client(cid_key_init)
        if not client_data_init:
            logging.warning(f"[INIT] Cliente {cid_key_init} en keys pero sin datos. Omitiendo.")
            continue

        current_status_init = client_data_init.get('status')
        room_id_init = client_data_init.get('room_id')

        # Verificar estado del cliente
        if current_status_init == 'pending_reconnect':
            # Cliente ya en pending_reconnect, verificar si expiró
            pending_since_init = client_data_init.get('pending_since', 0)
            is_web_init_flag = client_data_init.get('is_web', False)
            timeout_init = WEB_RECONNECT_TIMEOUT if is_web_init_flag else RECONNECT_GRACE_PERIOD

            if now - pending_since_init > timeout_init:
                logging.info(f"[INIT] Cliente {cid_key_init} (web: {is_web_init_flag}) en pending_reconnect expiró ({timeout_init}s). Limpiando.")
                clients_to_delete_init.append(cid_key_init)
                
                # Actualizar sesión relacionada
                if room_id_init:
                    if room_id_init not in sessions_to_update_init:
                        sessions_to_update_init[room_id_init] = await get_session(room_id_init)
                    if sessions_to_update_init[room_id_init] and cid_key_init in sessions_to_update_init[room_id_init].get('clients', []):
                        sessions_to_update_init[room_id_init]['clients'].remove(cid_key_init)
        
        elif current_status_init in ['connected', 'waiting']:
            # Cliente que estaba conectado, marcar como pending_reconnect
            logging.info(f"[INIT] Cliente {cid_key_init} estaba {current_status_init}. Marcando como pending_reconnect.")
            client_data_init['status'] = 'pending_reconnect'
            client_data_init['pending_since'] = now
            client_data_init['is_web'] = cid_key_init.startswith("web-")
            await set_client(cid_key_init, client_data_init)
            
            # Actualizar sesión relacionada
            if room_id_init:
                if room_id_init not in sessions_to_update_init:
                    sessions_to_update_init[room_id_init] = await get_session(room_id_init)
                if sessions_to_update_init[room_id_init]:
                    sessions_to_update_init[room_id_init]['last_activity'] = now
        
        elif current_status_init == 'dozing':
            # Cliente en doze, mantener estado
            logging.info(f"[INIT] Cliente {cid_key_init} está en dozing. Manteniendo estado. Actualizando last_activity de sesión.")
            if room_id_init:
                if room_id_init not in sessions_to_update_init:
                    sessions_to_update_init[room_id_init] = await get_session(room_id_init)
                if sessions_to_update_init[room_id_init]:
                    sessions_to_update_init[room_id_init]['last_activity'] = now

    # Actualizar sesiones modificadas
    for room_id_upd, session_data_upd in sessions_to_update_init.items():
        if session_data_upd:
            if not session_data_upd.get('clients'):
                logging.info(f"[INIT] Sesión {room_id_upd} quedó vacía durante init. Eliminando.")
                await delete_session(room_id_upd)
            else:
                session_data_upd['last_activity'] = now
                await set_session(room_id_upd, session_data_upd)
        else:
            logging.warning(f"[INIT] Sesión {room_id_upd} no encontrada al intentar actualizarla (pudo ser eliminada).")

    # Eliminar clientes marcados para borrar
    for cid_del_init in clients_to_delete_init:
        await delete_client(cid_del_init, keep_message_queue=False)

    # Limpiar diccionario de websockets activos
    # Ya debería estar vacío al inicio, pero por seguridad
    active_websockets.clear()
    
    logging.info("[INIT] Inicialización de estado del servidor desde Redis completada.")
