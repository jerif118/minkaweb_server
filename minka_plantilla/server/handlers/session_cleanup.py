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
        client_info_cleanup = await get_client(client_id_key)
        if not client_info_cleanup:
            continue

        if client_info_cleanup.get('status') == 'pending_reconnect':
            is_web_cleanup = client_info_cleanup.get('is_web', False)
            timeout_for_client_cleanup = WEB_RECONNECT_TIMEOUT if is_web_cleanup else RECONNECT_GRACE_PERIOD
            
            # Verificar si expiró el tiempo de reconexión
            if now - client_info_cleanup.get('pending_since', now) > timeout_for_client_cleanup:
                logging.info(f'[CLEANUP] Cliente {client_id_key} (web: {is_web_cleanup}) no reconectó ({timeout_for_client_cleanup}s). Limpiando.')
                room_id_of_client = client_info_cleanup.get('room_id')
                
                # Actualizar sesión si existe
                if room_id_of_client:
                    session_info_of_client = await get_session(room_id_of_client)
                    if session_info_of_client:
                        # Notificar al peer si está activo
                        for peer_cid in session_info_of_client.get('clients', []):
                            if peer_cid != client_id_key and peer_cid in active_websockets:
                                try:
                                    active_websockets[peer_cid].write_message({
                                        'event': 'peer_reconnect_failed', 
                                        'client_id': client_id_key
                                    })
                                except Exception:
                                    pass
                        
                        # Remover cliente de la sesión
                        if client_id_key in session_info_of_client.get('clients', []):
                            session_info_of_client['clients'].remove(client_id_key)
                            session_info_of_client['last_activity'] = now
                            
                            # Si la sesión queda vacía, marcarla para eliminar
                            if not session_info_of_client['clients']:
                                if room_id_of_client not in expired_session_ids:
                                    expired_session_ids.append(room_id_of_client)
                                logging.info(f"[CLEANUP] Sesión {room_id_of_client} marcada para expirar (último cliente {client_id_key} no reconectó).")
                            else:
                                await set_session(room_id_of_client, session_info_of_client)
                
                # Determinar si se debe conservar la cola de mensajes
                was_dozing_before_pending = client_info_cleanup.get('dozing', False)
                await delete_client(client_id_key, keep_message_queue=was_dozing_before_pending)
                if was_dozing_before_pending:
                    logging.info(f"[CLEANUP] Cliente {client_id_key} estaba en doze antes de pending_reconnect, cola conservada.")
    
    # Limpiar las sesiones marcadas como expiradas
    for rid_to_delete in set(expired_session_ids):  # Usar set para evitar duplicados
        logging.info(f'[CLEANUP] Eliminando sesión expirada {rid_to_delete} y sus clientes restantes.')
        session_being_deleted = await get_session(rid_to_delete)
        if session_being_deleted:
            for cid_in_del_session in session_being_deleted.get('clients', []):
                if cid_in_del_session in active_websockets:
                    try:
                        active_websockets[cid_in_del_session].write_message({
                            'info': 'Sesión expirada por inactividad o limpieza.',
                            'code': 'SESSION_EXPIRED'
                        })
                        active_websockets[cid_in_del_session].close(code=1000, reason="Sesión expirada")
                    except Exception:
                        pass  # Ignorar errores al cerrar
                
                # Eliminar cliente (sin conservar cola)
                await delete_client(cid_in_del_session, keep_message_queue=False)
                
        # Finalmente eliminar la sesión
        await delete_session(rid_to_delete)

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
