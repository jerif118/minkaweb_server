import tornado.web
import tornado.websocket
import tornado.ioloop
import tornado.escape
import json
import time
import uuid
import logging
import signal
import asyncio # Importar asyncio
import os
import re
import jwt
import logging.handlers
import collections

from tornado.ioloop import PeriodicCallback

# Importar funciones de session.py (ahora asíncronas)
from session import (
    get_session, set_session, delete_session, get_all_session_keys,
    get_client, set_client, delete_client, get_all_client_keys,
    add_jti_to_blacklist, is_jti_blacklisted, generate_jwt, verify_jwt, 
    add_message_to_queue, get_pending_messages,
    init_redis_client, # Para inicializar y cerrar
    close_redis_client
)

# Importar configuración
from config import (
    SESSION_TIMEOUT, RECONNECT_GRACE_PERIOD, MESSAGE_TTL_SECONDS, 
    JWT_SECRET_KEY, JWT_ALGORITHM, JWT_EXPIRATION_DELTA_SECONDS,
    MESSAGE_QUEUE_KEY_PREFIX, CLIENT_KEY_PREFIX, SESSION_KEY_PREFIX, 
    WEB_RECONNECT_TIMEOUT, DOZE_TIMEOUT, SERVER_PORT, SERVER_HOST,
    LOG_LEVEL, LOG_DIR, 
    LOG_TO_FILE, LOG_TO_CONSOLE, LOG_MAX_BYTES, LOG_BACKUP_COUNT
)

# Configuración del logger (movida de logger.py si es que existía y se quiere integrar aquí)
# Si tienes un logger.py separado, asegúrate que su configuración no colisione o úsalo directamente.
if not logging.getLogger().hasHandlers(): # Evitar múltiples handlers si se recarga
    log_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    logging.getLogger().setLevel(LOG_LEVEL)

    if LOG_TO_CONSOLE:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_formatter)
        logging.getLogger().addHandler(console_handler)

    if LOG_TO_FILE:
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR)
        
        # Log general
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, 'minka_server.log'), 
            maxBytes=LOG_MAX_BYTES, 
            backupCount=LOG_BACKUP_COUNT
        )
        file_handler.setFormatter(log_formatter)
        logging.getLogger().addHandler(file_handler)

        # Log de errores separado
        error_file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, 'minka_server_error.log'),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT
        )
        error_file_handler.setFormatter(log_formatter)
        error_file_handler.setLevel(logging.ERROR)
        logging.getLogger().addHandler(error_file_handler)

# Diccionario para mantener los websockets activos (cliente_id -> WebSocketHandler)
active_websockets = {}

async def _is_client_slot_active(client_id: str) -> bool:
    """True si el cliente sigue contando en la sala."""
    client = await get_client(client_id)
    if not client:
        return False  # ya fue limpiado

    status = client.get("status")
    if status == "pending_reconnect":
        is_web = client.get("is_web", False)
        timeout = WEB_RECONNECT_TIMEOUT if is_web else RECONNECT_GRACE_PERIOD
        return (time.time() - client.get("pending_since", 0)) < timeout

    # dozing, connected, waiting → plaza ocupada
    return True

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.write("Servidor Minka WebSockets V2 está operativo.")

class HealthHandler(tornado.web.RequestHandler):
    async def get(self):
        health_data = {
            "status": "ok",
            "timestamp": time.time(),
            "server_info": {
                "version": "MinkaV2 (Redis-based)",
                "port": SERVER_PORT,
                "host": SERVER_HOST
            }
        }
        
        # Verificar conexión a Redis
        redis_ok = False
        redis_info = {}
        try:
            rc = await init_redis_client()
            if rc:
                await rc.ping()
                redis_ok = True
                # Obtener información adicional de Redis
                info = await rc.info()
                redis_info = {
                    "connected": True,
                    "version": info.get('redis_version', 'unknown'),
                    "used_memory": info.get('used_memory_human', 'unknown'),
                    "uptime": info.get('uptime_in_seconds', 0)
                }
        except Exception as e:
            logging.error(f"[HEALTH] Error de Redis: {e}")
            redis_info = {
                "connected": False,
                "error": str(e)
            }
        
        health_data["redis"] = redis_info
        
        # Obtener estadísticas del servidor
        try:
            session_keys = await get_all_session_keys()
            client_keys = await get_all_client_keys()
            
            # Contar sesiones activas y clientes conectados
            active_sessions = 0
            active_clients = 0
            dozing_clients = 0
            
            for session_key in session_keys:
                session_data = await get_session(session_key)
                if session_data:
                    active_sessions += 1
            
            for client_key in client_keys:
                client_data = await get_client(client_key)
                if client_data:
                    active_clients += 1
                    if client_data.get('status') == 'dozing':
                        dozing_clients += 1
            
            health_data["server_stats"] = {
                "active_sessions": active_sessions,
                "active_clients": active_clients,
                "dozing_clients": dozing_clients,
                "active_websockets": len(active_websockets),
                "websocket_connections": list(active_websockets.keys())
            }
            
        except Exception as e:
            logging.error(f"[HEALTH] Error obteniendo estadísticas: {e}")
            health_data["server_stats"] = {
                "error": f"No se pudieron obtener estadísticas: {str(e)}"
            }
        
        # Determinar el estado general
        if not redis_ok:
            health_data["status"] = "degraded"
            self.set_status(503)  # Service Unavailable
        
        self.write(health_data)

class MonitorHandler(tornado.web.RequestHandler):
    async def get(self):
        # Esta función ahora debe ser asíncrona debido a las llamadas a Redis
        sessions = []
        clients = []
        try:
            session_keys = await get_all_session_keys()
            for room_id in session_keys:
                session_data = await get_session(room_id)
                if session_data:
                    sessions.append({room_id: session_data})
            
            client_keys = await get_all_client_keys()
            for client_id in client_keys:
                client_data = await get_client(client_id)
                if client_data:
                    # No mostrar JWTs completos en el monitor
                    if 'current_jti' in client_data: client_data['current_jti'] = "****"
                    clients.append({client_id: client_data})
        except Exception as e:
            logging.error(f"[MONITOR] Error al obtener datos de Redis: {e}")
            self.set_status(500)
            self.write({"error": "Error al contactar con Redis", "details": str(e)})
            return

        active_ws_info = []
        for client_id, ws_handler in active_websockets.items():
            active_ws_info.append({
                "client_id": client_id,
                "room_id": getattr(ws_handler, 'room_id', 'N/A'),
                "handler_class": ws_handler.__class__.__name__,
                "remote_ip": ws_handler.request.remote_ip
            })

        # Generar HTML para el monitor
        html = f"""
        <html>
        <head>
          <title>Monitor - Minka WebSocket Server</title>
          <meta charset="UTF-8">
          <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            h1, h2 {{ color: #333; }}
            .stats {{ display: flex; gap: 20px; margin-bottom: 20px; }}
            .stat-card {{ background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .stat-number {{ font-size: 2em; font-weight: bold; color: #007bff; }}
            .stat-label {{ color: #666; font-size: 0.9em; }}
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
            th {{ background-color: #007bff; color: white; font-weight: bold; }}
            tr:nth-child(even) {{ background-color: #f8f9fa; }}
            .status-connected {{ color: #28a745; font-weight: bold; }}
            .status-dozing {{ color: #ffc107; font-weight: bold; }}
            .status-pending {{ color: #dc3545; font-weight: bold; }}
            .json-data {{ background: #f8f9fa; padding: 10px; border-radius: 4px; font-family: monospace; font-size: 0.9em; max-height: 200px; overflow-y: auto; }}
          </style>
        </head>
        <body>
          <div class="container">
            <h1>Monitor - Servidor WebSocket Minka</h1>
            <div class="stats">
              <div class="stat-card">
                <div class="stat-number">{len(sessions)}</div>
                <div class="stat-label">Sesiones Activas</div>
              </div>
              <div class="stat-card">
                <div class="stat-number">{len(clients)}</div>
                <div class="stat-label">Clientes Registrados</div>
              </div>
              <div class="stat-card">
                <div class="stat-number">{len(active_websockets)}</div>
                <div class="stat-label">WebSockets Conectados</div>
              </div>
            </div>

            <h2>Conexiones WebSocket Activas</h2>
            <table>
              <tr>
                <th>Client ID</th>
                <th>Room ID</th>
                <th>IP Remota</th>
                <th>Handler</th>
              </tr>"""
        
        for ws_info in active_ws_info:
            html += f"""
              <tr>
                <td>{tornado.escape.xhtml_escape(ws_info['client_id'])}</td>
                <td>{tornado.escape.xhtml_escape(str(ws_info['room_id']))}</td>
                <td>{tornado.escape.xhtml_escape(ws_info['remote_ip'])}</td>
                <td>{tornado.escape.xhtml_escape(ws_info['handler_class'])}</td>
              </tr>"""
        
        html += """
            </table>

            <h2>Sesiones (Salas)</h2>
            <table>
              <tr>
                <th>Room ID</th>
                <th>Contraseña</th>
                <th>Clientes</th>
                <th>Última Actividad</th>
                <th>Detalles</th>
              </tr>"""
        
        for session_dict in sessions:
            for room_id, session_data in session_dict.items():
                clients_list = ", ".join(session_data.get('clients', []))
                last_activity = session_data.get('last_activity', 0)
                import datetime
                last_activity_str = datetime.datetime.fromtimestamp(last_activity).strftime('%Y-%m-%d %H:%M:%S') if last_activity else 'N/A'
                
                html += f"""
              <tr>
                <td>{tornado.escape.xhtml_escape(room_id)}</td>
                <td>{tornado.escape.xhtml_escape(session_data.get('password',''))}</td>
                <td>{tornado.escape.xhtml_escape(clients_list)}</td>
                <td>{last_activity_str}</td>
                <td><details><summary>Ver datos</summary><div class="json-data">{tornado.escape.xhtml_escape(str(session_data))}</div></details></td>
              </tr>"""
        
        html += """
            </table>

            <h2>Clientes Registrados</h2>
            <table>
              <tr>
                <th>Client ID</th>
                <th>Room ID</th>
                <th>Estado</th>
                <th>Última Conexión</th>
                <th>Detalles</th>
              </tr>"""
        
        for client_dict in clients:
            for client_id, client_data in client_dict.items():
                room_id = client_data.get('room_id', 'N/A')
                status = client_data.get('status', 'unknown')
                last_seen = client_data.get('last_seen', 0)
                last_seen_str = datetime.datetime.fromtimestamp(last_seen).strftime('%Y-%m-%d %H:%M:%S') if last_seen else 'N/A'
                
                status_class = ""
                if status == 'connected':
                    status_class = "status-connected"
                elif status == 'dozing':
                    status_class = "status-dozing"
                elif status == 'pending_reconnect':
                    status_class = "status-pending"
                
                html += f"""
              <tr>
                <td>{tornado.escape.xhtml_escape(client_id)}</td>
                <td>{tornado.escape.xhtml_escape(room_id)}</td>
                <td class="{status_class}">{tornado.escape.xhtml_escape(status)}</td>
                <td>{last_seen_str}</td>
                <td><details><summary>Ver datos</summary><div class="json-data">{tornado.escape.xhtml_escape(str(client_data))}</div></details></td>
              </tr>"""
        
        html += """
            </table>
            <p><small>Presiona F5 para refrescar manualmente</small></p>
          </div>
        </body>
        </html>"""
        
        self.set_header("Content-Type", "text/html; charset=UTF-8")
        self.write(html)

class WebSocketHandler(tornado.websocket.WebSocketHandler):
    
    HEARTBEAT_INTERVAL = 45  # Intervalo de heartbeat en segundos
    HEARTBEAT_TIMEOUT = 15  # Timeout para heartbeat en segundos
    HANDSHAKE_WINDOW = 5  # Ventana de tiempo para handshake en segundos
    MAX_HANDSHAKES_PER_WINDOW = 200  # Máximo de handshakes permitidos en la ventana
    handshake_times = collections.deque()

    def check_origin(self, origin):
        """Permitir conexiones cross-origin para desarrollo y testing.
        En producción, esto debería ser más restrictivo."""
        return True
    
    async def open(self, *args, **kwargs):
        # Todas las operaciones de Redis deben ser awaited
        # Parámetros esperados según documentación:
        # - client_id: obligatorio para todas las conexiones
        # - action: create, join (opcional si hay jwt_token)
        # - jwt_token: para reconexiones
        # - room_id, room_password: para join
        now = time.time()
        self.__class__.handshake_times.append(now)
        while (self.__class__.handshake_times
               and self.__class__.handshake_times[0] < now - self.HANDSHAKE_WINDOW):
                self.__class__.handshake_times.popleft()
        if len(self.__class__.handshake_times) > self.MAX_HANDSHAKES_PER_WINDOW:
            logging.warning("[RL] desmasiado handshakes, rechazando...")
            self.close(code=1013, reason ="Server overloaded, retry later")
            return

        self.client_id = None
        self.room_id = None
        self.intentional_disconnect = False
        
        # Obtener parámetros de la URL
        client_id_param = self.get_argument("client_id", None)
        action = self.get_argument("action", None)
        token = self.get_argument("jwt_token", None) or self.get_argument("jwt", None)
        
        # También verificar headers para JWT
        if not token and self.request.headers.get("Sec-WebSocket-Protocol"):
            protocols = [p.strip() for p in self.request.headers.get("Sec-WebSocket-Protocol", "").split(',')]
            for p in protocols:
                if len(p) > 30 and not p.startswith("action-"):
                    token = p
                    break
        
        # REQUISITO ESTRICTO: client_id siempre obligatorio según documentación TXT
        if not client_id_param:
            logging.warning(f"[WS-OPEN] Conexión rechazada: client_id es obligatorio según documentación")
            self.write_message({
                'error': 'client_id es obligatorio en todos los tipos de conexión', 
                'code': 'CLIENT_ID_REQUIRED',
                'documentation': 'Usar: ws://servidor/ws?client_id=YOUR_CLIENT_ID&action=create'
            })
            self.close()
            return

        if token: # Cliente reconectando con JWT
            payload = await verify_jwt(token)
            logging.info(f"[WS-OPEN] Verificación JWT para {client_id_param}: {'válido' if payload else 'inválido'}")
            if payload:
                self.client_id = payload.get('client_id')
                self.room_id = payload.get('room_id')
                
                # Si se proporciona client_id en parámetros, debe coincidir con el JWT
                if client_id_param and client_id_param != self.client_id:
                    logging.warning(f"[WS-OPEN] client_id en parámetros ({client_id_param}) no coincide con JWT ({self.client_id})")
                    self.write_message({'error': 'client_id no coincide con JWT', 'code': 'CLIENT_ID_MISMATCH'})
                    self.close()
                    return
                
                logging.info(f"[WS-OPEN] Cliente {self.client_id} autenticado con JWT para sala {self.room_id}")
                
                client_data = await get_client(self.client_id)
                if not client_data or client_data.get('room_id') != self.room_id:
                    logging.warning(f"[WS-OPEN] JWT válido para {self.client_id} pero datos de cliente no coinciden o no existen. Rechazando.")
                    self.write_message({'error': 'Token inválido o sesión caducada', 'code': 'INVALID_TOKEN_SESSION'})
                    self.close()
                    return
                
                # Marcar como conectado
                client_data['status'] = 'connected'
                client_data['last_seen'] = time.time()
                await set_client(self.client_id, client_data)
                
                session_data = await get_session(self.room_id)
                if not session_data:
                    logging.warning(f"[WS-OPEN] Cliente {self.client_id} intentó unirse a sala {self.room_id} inexistente con JWT. Rechazando.")
                    self.write_message({'error': 'Sala no existe', 'code': 'ROOM_NOT_FOUND'})
                    self.close()
                    return
                
                if self.client_id not in session_data.get('clients', []):
                    session_data.get('clients', []).append(self.client_id)
                session_data['last_activity'] = time.time()
                await set_session(self.room_id, session_data)

                active_websockets[self.client_id] = self
                self.write_message({'event': 'reconnected', 'client_id': self.client_id, 'room_id': self.room_id})
                logging.info(f"[WS-OPEN] Cliente {self.client_id} reconectado/validado en sala {self.room_id}")
                
                # Enviar mensajes pendientes si los hay
                pending_messages = await get_pending_messages(self.client_id, delete_queue=True)
                if pending_messages:
                    logging.info(f"[WS-OPEN] Enviando {len(pending_messages)} mensajes pendientes a {self.client_id}")
                    for msg_payload in pending_messages:
                        try:
                            self.write_message(msg_payload)
                        except tornado.websocket.WebSocketClosedError:
                            logging.warning(f"[WS-OPEN] WebSocket cerrado para {self.client_id} al enviar mensajes pendientes. Re-encolando...")
                            await add_message_to_queue(self.client_id, msg_payload)
                            break
                # --- start heartbeat ---
                self._last_pong = time.time()
                self._hb = PeriodicCallback(self._send_ping,
                                            self.HEARTBEAT_INTERVAL * 1000)
                self._hb.start()
                return # Conexión establecida
            else:
                logging.warning(f"[WS-OPEN] Token JWT inválido o expirado presentado. Rechazando.")
                self.write_message({'error': 'Token inválido o expirado', 'code': 'INVALID_TOKEN'})
                self.close()
                return
        
        # Usar client_id proporcionado en parámetros para todas las acciones
        if not action:
            logging.warning(f"[WS-OPEN] Conexión sin acción especificada. Se requiere action=create o action=join")
            self.write_message({'error': 'Acción requerida (create o join)', 'code': 'ACTION_REQUIRED'})
            self.close()
            return

        if action == "create":
            # Usar el client_id proporcionado en los parámetros
            if not client_id_param:
                logging.warning(f"[WS-OPEN] action=create requiere client_id en parámetros")
                self.write_message({'error': 'client_id requerido para crear sala', 'code': 'CLIENT_ID_REQUIRED'})
                self.close()
                return
                
            self.client_id = client_id_param
            self.room_id = uuid.uuid4().hex
            room_password = uuid.uuid4().hex[:6].upper() # Generar contraseña de 6 dígitos

            client_data = {
                'client_id': self.client_id,
                'room_id': self.room_id,
                'status': 'waiting', # Esperando al peer
                'is_initiator': True,
                'is_web': self.client_id.startswith("web-"), # Marcar si es web
                'last_seen': time.time()
            }
            await set_client(self.client_id, client_data)

            session_data = {
                'room_id': self.room_id,
                'password': room_password,
                'clients': [self.client_id],
                'initiator_id': self.client_id,
                'status': 'waiting_for_peer',
                'created_at': time.time(),
                'last_activity': time.time(),
                'has_dozing_client': False
            }
            await set_session(self.room_id, session_data)
            active_websockets[self.client_id] = self
            
            # NO generar JWT hasta que AMBOS clientes estén conectados
            # Esto mantiene el QR pequeño (solo room_id + password)
            
            # Respuesta inicial: solo credenciales para compartir
            response_message = {
                'room_created': True,  # Frontend React espera esta propiedad
                'room_id': self.room_id,
                'password': room_password
                # NO enviar jwt_token aquí - se enviará cuando ambos estén conectados
            }
                
            self.write_message(response_message)
            logging.info(f"[WS-OPEN] Sala {self.room_id} creada por {self.client_id}. Contraseña: {room_password}. Esperando peer para generar JWT.")
        
        elif action == "join":
            room_id_join = self.get_argument("room_id", None)
            room_password_join = self.get_argument("room_password", None) or self.get_argument("password", None)
            
            # Debug: Log de parámetros recibidos
            logging.info(f"[WS-JOIN-DEBUG] Parámetros recibidos: client_id={client_id_param}, room_id={room_id_join}, password={room_password_join}")
            
            # Usar client_id de parámetros (obligatorio según documentación)
            if not client_id_param:
                logging.warning(f"[WS-OPEN] action=join requiere client_id en parámetros")
                self.write_message({'error': 'client_id requerido para unirse a sala', 'code': 'CLIENT_ID_REQUIRED'})
                self.close()
                return

            if not room_id_join or not room_password_join:
                self.write_message({'error': 'Faltan room_id o password para unirse', 'code': 'JOIN_MISSING_PARAMS'})
                self.close()
                return
            
            # Validación de formato (opcional pero recomendado)
            # Ajustar regex para UUID v4 (36 chars con guiones, o 32 sin guiones)
            if not re.match(r'^[0-9a-fA-F]{32}$', room_id_join) and not re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', room_id_join):
                 self.write_message({'error': 'Formato de room_id inválido', 'code': 'INVALID_ROOM_ID_FORMAT'})
                 self.close()
                 return
            if not re.match(r'^[A-Z0-9]{6}$', room_password_join): # Ajustado para coincidir con generación (MAYÚSCULAS)
                 self.write_message({'error': 'Formato de contraseña inválido', 'code': 'INVALID_PASSWORD_FORMAT'})
                 self.close()
                 return

            current_session_join = await get_session(room_id_join)
            clients_in_room = current_session_join.get('clients', [])
            already_in_room = client_id_param in clients_in_room

            # Exigir JWT para reconexión de cliente existente
            if already_in_room and not token:
                logging.warning(f"[WS-OPEN] Reconexión sin token rechazada para {client_id_param}")
                self.write_message({'error': 'Token JWT requerido para reconexión', 'code': 'TOKEN_REQUIRED'})
                self.close()
                return

            active_slots = 0
            for cid in clients_in_room:
                if cid == client_id_param:
                    continue  # el que se reconecta no cuenta doble
                if await _is_client_slot_active(cid):
                    active_slots += 1

            if (not already_in_room) and active_slots >= 2 and not current_session_join.get('has_dozing_client'):
                self.write_message({'error': 'La sala ya está llena', 'code': 'ROOM_FULL'})
                self.close()
                return
                
            # Lógica para re-unión de cliente en doze o unión de nuevo cliente
            has_dozing_client = current_session_join.get('has_dozing_client', False)
            dozing_client_id = current_session_join.get('doze_client_id')

            # Si el cliente que se une es el que estaba en doze
            if has_dozing_client and dozing_client_id == client_id_param:
                self.client_id = dozing_client_id
                self.room_id = room_id_join
                logging.info(f"[WS-JOIN] Cliente {self.client_id} (estaba en doze) volviendo a sala {self.room_id}")
                client_data_doze = await get_client(self.client_id)
                if client_data_doze:
                    client_data_doze['status'] = 'connected'
                    client_data_doze['dozing'] = False # Ya no está en doze
                    client_data_doze.pop('doze_start', None)
                    client_data_doze.pop('doze_id', None)
                    await set_client(self.client_id, client_data_doze)
                
                current_session_join['has_dozing_client'] = False # Ya no hay cliente en doze
                current_session_join.pop('doze_client_id', None)
                current_session_join.pop('doze_start_time', None)
                # No quitar de previous_clients aún, podría ser útil
            else:
                # Nuevo cliente uniéndose - usar client_id de parámetros
                self.client_id = client_id_param
                
                self.room_id = room_id_join

                if self.client_id in current_session_join.get('clients',[]):
                    logging.warning(f"[WS-JOIN] Cliente {self.client_id} ya está en la sala {self.room_id}. Permitir reconexión.")
                    # Podría ser una reconexión web o un reintento móvil
                else:
                    current_session_join.get('clients', []).append(self.client_id)

            # Actualizar datos del cliente que se une/reconecta
            client_data_join = await get_client(self.client_id) or {}
            client_data_join.update({
                'client_id': self.client_id,
                'room_id': self.room_id,
                'status': 'connected',
                'is_initiator': client_data_join.get('is_initiator', False), # Mantener si ya existía
                'is_web': self.client_id.startswith("web-"),
                'last_seen': time.time()
            })
            await set_client(self.client_id, client_data_join)
            
            current_session_join['status'] = 'active' # Sala activa con dos clientes
            current_session_join['last_activity'] = time.time()
            await set_session(self.room_id, current_session_join)
            active_websockets[self.client_id] = self

            # Verificar si ahora hay 2 clientes activos para generar JWT
            active_clients_in_room = [cid for cid in current_session_join.get('clients', []) if cid in active_websockets]
            both_users_connected = len(active_clients_in_room) >= 2
            
            jwt_token_for_joiner = None
            
            if both_users_connected:
                # ¡AMBOS usuarios están conectados! Generar JWT para TODOS los clientes
                logging.info(f"[WS-JOIN] Ambos usuarios conectados en sala {self.room_id}. Generando JWT para todos los clientes.")
                
                for client_id_for_jwt in active_clients_in_room:
                    # Generar JWT para todos los clientes (web y móvil)
                    jwt_token_for_client = generate_jwt(client_id_for_jwt, self.room_id)
                    try:
                        payload = jwt.decode(jwt_token_for_client, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_exp": False})
                        client_data_for_jwt = await get_client(client_id_for_jwt)
                        if client_data_for_jwt:
                            client_data_for_jwt['current_jti'] = payload.get('jti')
                            await set_client(client_id_for_jwt, client_data_for_jwt)
                            
                        # Enviar JWT al cliente correspondiente
                        if client_id_for_jwt == self.client_id:
                            jwt_token_for_joiner = jwt_token_for_client
                        elif client_id_for_jwt in active_websockets:
                            active_websockets[client_id_for_jwt].write_message({
                                'event': 'joined',  # Frontend React espera 'joined'
                                'jwt_token': jwt_token_for_client,
                                'peer_id': self.client_id,
                                'room_id': self.room_id,
                                'info': 'Ambos usuarios conectados. JWT generado para reconexiones futuras.'
                            })
                            logging.info(f"[WS-JOIN] JWT enviado a cliente existente {client_id_for_jwt}")
                            
                    except jwt.PyJWTError as e:
                        logging.error(f"[WS-JOIN] Error al decodificar JWT para {client_id_for_jwt}: {e}")

            # Enviar respuesta al cliente que se unió
            response_message = {
                'event': 'joined',  # Frontend React espera 'joined'
                'room_id': self.room_id,
                'client_id': self.client_id
            }
            
            if jwt_token_for_joiner:
                response_message['jwt_token'] = jwt_token_for_joiner
                response_message['connection_status'] = 'both_connected'
                response_message['info'] = 'Ambos usuarios conectados. JWT generado para reconexiones futuras.'
                logging.info(f"[WS-JOIN] Cliente {self.client_id} se unió a sala {self.room_id} - Ambos usuarios conectados, JWT enviado")
            else:
                response_message['connection_status'] = 'waiting_for_peer'
                response_message['info'] = 'Esperando que se conecte el otro usuario para generar JWT.'
                logging.info(f"[WS-JOIN] Cliente {self.client_id} se unió a sala {self.room_id} - Esperando peer para enviar JWT")
            
            self.write_message(response_message)
            logging.info(f"[WS-JOIN] Cliente {self.client_id} se unió a sala {self.room_id}")

            # Notificar al otro peer
            other_peers = [cid for cid in current_session_join.get('clients', []) if cid != self.client_id]
            for peer_id in other_peers:
                if peer_id in active_websockets:
                    try:
                        active_websockets[peer_id].write_message({
                            'event': 'peer_joined',
                            'peer_id': self.client_id,
                            'room_id': self.room_id
                        })
                    except tornado.websocket.WebSocketClosedError:
                        logging.warning(f"[WS-JOIN] Error al notificar a {peer_id}: WebSocket cerrado.")
                else:
                    # Si el peer no está activo (ej. móvil en doze que se acaba de despertar o web desconectado)
                    # Se podría encolar una notificación, pero 'peer_joined' es más para conexión en tiempo real.
                    logging.info(f"[WS-JOIN] Peer {peer_id} no tiene websocket activo. No se envió notificación de unión.")
            
            # Enviar mensajes pendientes al cliente que se une/reconecta
            pending_messages_join = await get_pending_messages(self.client_id, delete_queue=True)
            if pending_messages_join:
                logging.info(f"[WS-JOIN] Enviando {len(pending_messages_join)} mensajes pendientes a {self.client_id}")
                for msg_payload in pending_messages_join:
                    try:
                        self.write_message(msg_payload)
                    except tornado.websocket.WebSocketClosedError:
                        logging.warning(f"[WS-JOIN] WebSocket cerrado para {self.client_id} al enviar mensajes pendientes. Re-encolando...")
                        await add_message_to_queue(self.client_id, msg_payload)
                        break
        else:
            self.write_message({'error': 'Acción no válida', 'code': 'INVALID_ACTION'})
            self.close()
            return
        self._last_pong = time.time()
        self._hb = PeriodicCallback(self._send_ping, self.HEARTBEAT_INTERVAL *1000)
        self._hb.start()
    async def _send_ping(self):
        try:
            self.ping(b"hb")
            if time.time() - self._last_pong > (
                self.HEARTBEAT_INTERVAL + self.HEARTBEAT_TIMEOUT):
                logging.warning(f"[HB]{self.client_id} sin pong; cerrando WebSocket.")
                self.close(code=4000, reason="Heartbeat timeout")
        except tornado.websocket.WebSocketClosedError:
            pass

    async def on_message(self, message):
        # Todas las operaciones de Redis deben ser awaited
        logging.info(f"[DEBUG] on_message de {getattr(self, 'client_id', 'N/A')} raw: {message[:200]}") # Limitar log
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logging.warning(f"[ON_MSG] JSON inválido recibido de {getattr(self, 'client_id', 'desconocido')}: {message[:100]}")
            self.write_message({'error': 'JSON inválido', 'code': 'INVALID_JSON'})
            self.close()
            return
            
        if not self.client_id or not self.room_id: # Si no se identificó en open()
            # Podría ser un cliente móvil enviando su primer mensaje de identificación
            # O un error si se esperaba que ya estuviera identificado.
            # Esta lógica dependerá de tu protocolo exacto para móviles.
            action = data.get('action')
            if action == 'identify_mobile': # Ejemplo de acción de identificación
                # ... lógica para identificar y registrar cliente móvil ...
                # self.client_id = ... ; self.room_id = ...
                # active_websockets[self.client_id] = self
                logging.info(f"[IDENTIFY] Cliente móvil identificado (lógica pendiente)")
                # await self.process_message_data(data) # Luego procesar el mensaje original
                return 
            else:
                logging.warning(f"[ON_MSG] Mensaje de WebSocket no identificado/no autenticado. Cerrando.")
                self.write_message({'error': 'No autenticado o identificado', 'code': 'AUTH_REQUIRED'})
                self.close()
                return
        
        # Llamar a un método separado para procesar los datos del mensaje
        await self.process_message_data(data)

    async def process_message_data(self, data):
        # Este método contiene la lógica que estaba en on_message después de la carga del JSON
        # y la verificación inicial de client_id/room_id.

        # Validar que el mensaje tenga una estructura válida
        action = data.get('action')
        message_content = data.get('message')
        
        # Si no hay acción ni mensaje, es inválido
        if not action and message_content is None:
            logging.warning(f"[PROCESS-MSG] Cliente {self.client_id} envió datos sin acción ni mensaje. Cerrando.")
            self.write_message({'error': 'Falta acción o mensaje', 'code': 'MISSING_ACTION_OR_MESSAGE'})
            self.close()
            return
            
        # Si hay acción, validar que sea una acción conocida
        valid_actions = ['leave']  # Lista de acciones válidas
        if action and action not in valid_actions:
            logging.warning(f"[PROCESS-MSG] Cliente {self.client_id} envió acción inválida: {action}. Cerrando.")
            self.write_message({'error': 'Acción no válida', 'code': 'INVALID_ACTION'})
            self.close()
            return
            
        # Si es solo un mensaje (sin acción), debe tener contenido
        if not action and message_content is not None:
            # Esto es un mensaje normal, continuar con la lógica de broadcast
            pass
        elif action:
            # Es una acción válida, continuar con la lógica de acción
            pass

        # Mejora de diagnóstico: Registrar el estado de la sala para depuración
        room_info = await get_session(self.room_id)
        if room_info:
            logging.info(f"[MESSAGE-DEBUG] Sala {self.room_id}: {room_info.get('status')}, Clientes: {room_info.get('clients', [])}")
        else:
            logging.warning(f"[MESSAGE-DEBUG] No se encontró info para sala {self.room_id} (cliente {self.client_id})")

        jwt_in_message = data.get('jwt_token') # Cliente podría enviar su JWT para ser invalidado en 'leave'

        if data.get('action') == 'leave':
            self.intentional_disconnect = True
            reason = data.get('reason', '')
            logging.info(f"[LEAVE] Cliente {self.client_id} deja sala {self.room_id}, razón: {reason}")
            
            # Blacklisting del JTI para clientes web
            if self.client_id.startswith("web-"):
                jti_to_blacklist = None
                # Prioridad si el cliente envía el token actual en el mensaje de 'leave'
                if jwt_in_message: 
                    try:
                        # No verificar expiración aquí, solo queremos el JTI de un token que el cliente *tenía*
                        payload = jwt.decode(jwt_in_message, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_exp": False}) 
                        jti_to_blacklist = payload.get('jti')
                        if jti_to_blacklist:
                            logging.info(f"[LEAVE-BLACKLIST] JTI {jti_to_blacklist} de JWT en mensaje para {self.client_id}")
                        else:
                            logging.warning(f"[LEAVE-BLACKLIST] JWT en mensaje para {self.client_id} no contenía JTI.")
                    except jwt.PyJWTError as e:
                        logging.warning(f"[LEAVE-BLACKLIST] Error decodificando JWT (para JTI) de {self.client_id}: {e}")
                
                if not jti_to_blacklist:
                    client_data_for_jti = await get_client(self.client_id)
                    if client_data_for_jti and client_data_for_jti.get('current_jti'):
                        jti_to_blacklist = client_data_for_jti.get('current_jti')
                        logging.info(f"[LEAVE-BLACKLIST] JTI {jti_to_blacklist} de almacenamiento para {self.client_id}")

                if jti_to_blacklist:
                    await add_jti_to_blacklist(jti_to_blacklist, JWT_EXPIRATION_DELTA_SECONDS)
                    logging.info(f"[LEAVE-BLACKLIST] JTI {jti_to_blacklist} para {self.client_id} en lista negra (TTL {JWT_EXPIRATION_DELTA_SECONDS}s)." )
                    client_data_to_clear_jti = await get_client(self.client_id)
                    if client_data_to_clear_jti:
                        if 'current_jti' in client_data_to_clear_jti:
                            del client_data_to_clear_jti['current_jti']
                            await set_client(self.client_id, client_data_to_clear_jti)
                            logging.info(f"[LEAVE-BLACKLIST] current_jti eliminado de Redis para {self.client_id}")
                else:
                    logging.warning(f"[LEAVE-BLACKLIST] No se encontró JTI para blacklisting para {self.client_id}.")

            # Lógica de 'leave' para cliente web cuando hay un móvil en doze
            if self.client_id.startswith("web-"):
                current_session_leave = await get_session(self.room_id)
                if current_session_leave:
                    peer_list = current_session_leave.get("clients", [])
                    other_ids = [cid for cid in peer_list if cid != self.client_id]
                    
                    mobile_in_doze = False
                    dozing_mobile_id = None
                    for peer_id in other_ids:
                        peer_data = await get_client(peer_id)
                        if peer_data and not peer_id.startswith("web-") and peer_data.get('status') == 'dozing':
                            mobile_in_doze = True
                            dozing_mobile_id = peer_id
                            break
                    
                    if mobile_in_doze:
                        logging.info(f"[LEAVE] Web {self.client_id} sale, móvil {dozing_mobile_id} en doze. Manteniendo sala {self.room_id}.")
                        if self.client_id in current_session_leave.get("clients", []):
                            current_session_leave["clients"].remove(self.client_id)
                        if "previous_clients" not in current_session_leave: current_session_leave["previous_clients"] = []
                        if self.client_id not in current_session_leave["previous_clients"]: current_session_leave["previous_clients"].append(self.client_id)
                        current_session_leave['last_activity'] = time.time()
                        await set_session(self.room_id, current_session_leave)
                        
                        # Notificar al móvil (si estuviera conectado, aunque está en doze)
                        if dozing_mobile_id and dozing_mobile_id in active_websockets:
                            try:
                                active_websockets[dozing_mobile_id].write_message({'event': 'peer_left', 'client_id': self.client_id, 'message': 'Cliente web desconectado. Permaneces en doze.'}) 
                            except tornado.websocket.WebSocketClosedError:
                                pass # Ya cerrado
                        
                        await delete_client(self.client_id, keep_message_queue=False) # Web no necesita cola al salir
                        if self.client_id in active_websockets: del active_websockets[self.client_id]
                    else:
                        # Comportamiento normal: eliminar sala completa si el web se va y no hay móvil en doze
                        logging.info(f"[LEAVE] Web {self.client_id} sale. Sala {self.room_id} y peers serán eliminados.")
                        for peer_id in other_ids:
                            if peer_id in active_websockets:
                                try: active_websockets[peer_id].write_message({'event': 'peer_left', 'client_id': self.client_id, 'message': 'El otro usuario se desconectó.'}) 
                                except tornado.websocket.WebSocketClosedError: pass
                            await delete_client(peer_id) 
                            if peer_id in active_websockets: del active_websockets[peer_id]
                        await delete_client(self.client_id, keep_message_queue=False)
                        if self.client_id in active_websockets: del active_websockets[self.client_id]
                        await delete_session(self.room_id)
                self.close()
                return

            # Móvil entrando en modo doze
            if reason == "doze":
                if self.client_id.startswith("web-"):
                    logging.warning(f"[LEAVE-DOZE] Cliente web {self.client_id} no puede entrar en doze.")
                    self.write_message({'error': 'Clientes web no pueden usar doze', 'code': 'WEB_CANT_DOZE'})
                    # No cerrar conexión necesariamente, solo rechazar acción
                    return 

                client_data_doze = await get_client(self.client_id)
                if not client_data_doze:
                    logging.error(f"[LEAVE-DOZE] No hay datos para {self.client_id} al entrar en doze")
                    self.close(); return
                
                client_data_doze['status'] = 'dozing'
                client_data_doze['dozing'] = True
                client_data_doze['doze_start'] = time.time()
                client_data_doze['doze_id'] = uuid.uuid4().hex
                await set_client(self.client_id, client_data_doze)
                logging.info(f"[LEAVE-DOZE] Cliente {self.client_id} en modo doze en sala {self.room_id}. Cola de mensajes se conservará.")
                
                current_session_doze = await get_session(self.room_id)
                if not current_session_doze:
                    logging.error(f"[LEAVE-DOZE] No se encontró sala {self.room_id} para {self.client_id}")
                    self.close(); return
                
                if "previous_clients" not in current_session_doze: current_session_doze["previous_clients"] = []
                for cid in current_session_doze.get('clients', []):
                    if cid != self.client_id and cid not in current_session_doze["previous_clients"]:
                        current_session_doze["previous_clients"].append(cid)
                
                current_session_doze['has_dozing_client'] = True
                current_session_doze['doze_client_id'] = self.client_id
                current_session_doze['doze_start_time'] = time.time()
                current_session_doze['last_activity'] = time.time()
                await set_session(self.room_id, current_session_doze)
                
                for peer_id in current_session_doze.get('clients', []):
                    if peer_id != self.client_id and peer_id in active_websockets:
                        try:
                            active_websockets[peer_id].write_message({
                                'event': 'peer_doze_mode', 'client_id': self.client_id,
                                'message': f'Usuario {self.client_id} en modo bajo consumo.',
                                'timestamp': time.time(), 'room_id': self.room_id
                            })
                        except tornado.websocket.WebSocketClosedError: pass
                
                if self.client_id in active_websockets: del active_websockets[self.client_id]
                
                # Crear cola de mensajes si no existe (add_message_to_queue lo hará si es necesario)
                # Pero podemos añadir un mensaje inicial de 'doze_start' explícitamente
                await add_message_to_queue(self.client_id, {
                    'event': 'doze_start_marker', 'timestamp': time.time()
                })
                logging.info(f"[LEAVE-DOZE] Cola para {self.client_id} verificada/preparada.")
                self.close()
                return

            # Leave normal (no web con móvil en doze, no móvil entrando en doze)
            current_session_normal_leave = await get_session(self.room_id)
            if current_session_normal_leave:
                other_ids = [cid for cid in current_session_normal_leave.get("clients", []) if cid != self.client_id]
                for peer_id in other_ids:
                    if peer_id in active_websockets:
                        try: active_websockets[peer_id].write_message({'event': 'peer_left', 'client_id': self.client_id, 'message': 'El otro usuario se desconectó.'}) 
                        except tornado.websocket.WebSocketClosedError: pass
                
                if self.client_id in current_session_normal_leave.get("clients", []):
                    current_session_normal_leave["clients"].remove(self.client_id)
                
                # Determinar si se debe conservar la cola (ej. si el cliente es móvil y podría reconectar)
                # Por defecto, para un leave normal, no se conserva la cola.
                keep_queue_on_leave = False # Cambiar si hay lógica específica para móviles en leave normal
                await delete_client(self.client_id, keep_message_queue=keep_queue_on_leave)
                if self.client_id in active_websockets: del active_websockets[self.client_id]
                
                # Actualizar clientes en sesión (filtrar los que ya no existen)
                valid_clients_in_session = []
                for cid_in_s in current_session_normal_leave.get("clients", []):
                    if await get_client(cid_in_s): # Verificar si el cliente aún existe en Redis
                        valid_clients_in_session.append(cid_in_s)
                current_session_normal_leave["clients"] = valid_clients_in_session
                
                if not current_session_normal_leave["clients"]:
                    await delete_session(self.room_id)
                    logging.info(f"[LEAVE] Cliente {self.client_id} salió. Sala {self.room_id} eliminada (vacía).")
                else:
                    current_session_normal_leave['last_activity'] = time.time()
                    await set_session(self.room_id, current_session_normal_leave)
                    logging.info(f"[LEAVE] Cliente {self.client_id} salió de {self.room_id}. Restantes: {len(current_session_normal_leave['clients'])}.")
            else:
                await delete_client(self.client_id) # No conservar cola si la sesión no existe
                if self.client_id in active_websockets: del active_websockets[self.client_id]
                logging.warning(f"[LEAVE] Cliente {self.client_id} intentó salir de sala ({self.room_id}) inexistente. Cliente eliminado.")

            self.close()
            return

        # Lógica de broadcast de mensajes (no acciones)
        msg_content = data.get('message')
        if msg_content is None: # Asegurar que haya algo que enviar
            logging.warning(f"[BROADCAST] Cliente {self.client_id} envió mensaje sin contenido 'message'. Ignorando.")
            # self.write_message({'error': 'Mensaje sin contenido 'message'', 'code': 'EMPTY_MESSAGE'})
            return

        try:
            msg_serializado = json.dumps(msg_content) # Serializar solo el contenido del mensaje
        except TypeError: # Si msg_content no es serializable
            self.write_message({'error': 'Contenido de mensaje no serializable', 'code': 'UNSERIALIZABLE_MESSAGE'})
            return

        if len(msg_serializado) > 1024 * 5: # Límite de 5KB para el contenido del mensaje
            self.write_message({'error': 'Mensaje demasiado largo (max 5KB)', 'code': 'MESSAGE_TOO_LONG'})
            return            
        
        # Broadcast
        current_session_bcast = await get_session(self.room_id)
        if current_session_bcast and len(current_session_bcast.get("clients",[])) > 1:
            other_clients_bcast = [cid for cid in current_session_bcast["clients"] if cid != self.client_id]
            
            if not other_clients_bcast:
                logging.info(f"[BROADCAST] No hay otros clientes en {self.room_id} para {self.client_id} para enviar mensaje.")
                # Podría ser una condición de carrera si el otro cliente acaba de irse
                self.write_message({'info': 'No hay destinatario en la sala.', 'code': 'NO_RECIPIENT'})
                return

            message_id = f"{time.time()}-{uuid.uuid4().hex[:8]}"
            message_payload_to_send = {
                'sender_id': self.client_id,
                'message': msg_content, # El contenido original del mensaje
                'timestamp': time.time(),
                'message_id': message_id
            }

            # Lógica actual 1-a-1, tomar el primero. Para múltiples, iterar.
            # for recipient_id in other_clients_bcast:
            recipient_id = other_clients_bcast[0]
            
            recipient_client_data = await get_client(recipient_id)
            is_recipient_dozing = recipient_client_data and recipient_client_data.get('status') == 'dozing'
            delivery_confirmed_to_sender = False

            if recipient_id in active_websockets and not is_recipient_dozing:
                try:
                    active_websockets[recipient_id].write_message(message_payload_to_send)
                    delivery_confirmed_to_sender = True
                    self.write_message({
                        'event': 'message_delivered', 'message_id': message_id,
                        'recipient_id': recipient_id, 'timestamp': time.time()
                    })
                    logging.info(f"[BROADCAST] Mensaje {message_id} de {self.client_id} entregado a {recipient_id}")
                except tornado.websocket.WebSocketClosedError:
                    logging.warning(f"[BROADCAST] WS cerrado para {recipient_id} al enviar. Se encolará.")
                except Exception as e:
                    logging.error(f"[BROADCAST] Error enviando a {recipient_id}: {e}. Se encolará.")
            
            if not delivery_confirmed_to_sender:
                # Encolar si no se pudo entregar directamente o si está en doze
                if await add_message_to_queue(recipient_id, message_payload_to_send):
                    logging.info(f"[BROADCAST] Mensaje {message_id} de {self.client_id} encolado para {recipient_id}.")
                    self.write_message({
                        'event': 'message_queued', 'recipient_id': recipient_id, 'message_id': message_id,
                        'original_message': msg_content, 'timestamp': time.time(),
                        'info': 'Mensaje encolado para entrega posterior.'
                    })
                else:
                    logging.error(f"[BROADCAST] Error al encolar mensaje {message_id} para {recipient_id}")
                    self.write_message({
                        'event': 'message_queue_failed', 'recipient_id': recipient_id, 'message_id': message_id,
                        'error': 'No se pudo encolar el mensaje.'
                    })
        else:
            # No hay otros clientes o la sesión no existe como se esperaba
            self.write_message({
                'info': 'Aún no emparejado o no hay otros participantes.',
                'code': 'NOT_PAIRED_OR_ALONE',
                'debug': {'original_message': msg_content, 'timestamp': time.time(),
                          'client_id': self.client_id, 'room_id': self.room_id}
            })

    def on_pong(self, data):
        self._last_pong = time.time()

    # ------------------------------------------------------------------
    #  Cierre del WebSocket
    #  Mantener el método síncrono como requiere Tornado, pero lanzar la
    #  lógica asíncrona dentro de una corrutina aparte.
    # ------------------------------------------------------------------
    def on_close(self):
        """Maneja tanto cierres voluntarios como inesperados."""
        if hasattr(self, "_hb"):
            self._hb.stop()
            logging.info(f"[CLOSE] Heartbeat detenido para "
                         f"{getattr(self, 'client_id', 'N/A')} "
                         f"en {getattr(self, 'room_id', 'N/A')}.")

        async def _cleanup_async():
            client_id_log = getattr(self, 'client_id', 'N/A')
            room_id_log   = getattr(self, 'room_id', 'N/A')

            if getattr(self, 'intentional_disconnect', False):
                logging.info(f"[CLOSE] Cierre voluntario de {client_id_log} en {room_id_log}. Limpieza por on_message/'leave'.")
                if hasattr(self, 'client_id') and self.client_id and self.client_id in active_websockets and active_websockets[self.client_id] == self:
                    del active_websockets[self.client_id]
                return

            # --- Desconexión Involuntaria / Inesperada ---
            client_id_on_close = getattr(self, 'client_id', None)
            room_id_on_close   = getattr(self, 'room_id', None)

            if not client_id_on_close or not room_id_on_close:
                logging.warning(f"[CLOSE] Desconexión inesperada ANTES de asignación client/room. WebSocket: {self}")
                return

            is_web_client_on_close = client_id_on_close.startswith("web-")
            logging.info(f"[CLOSE] Desconexión inesperada de {client_id_on_close} "
                         f"(web: {is_web_client_on_close}) en {room_id_on_close}. "
                         f"Marcando para reconexión.")

            client_data_on_close = await get_client(client_id_on_close)
            if client_data_on_close:
                if client_data_on_close.get('status') == 'dozing':
                    logging.info(f"[CLOSE] Cliente {client_id_on_close} ya estaba en doze, manteniendo estado.")
                else:
                    client_data_on_close['status'] = 'pending_reconnect'
                    client_data_on_close['pending_since'] = time.time()

                client_data_on_close['is_web'] = is_web_client_on_close
                await set_client(client_id_on_close, client_data_on_close)

                if is_web_client_on_close:
                    session_data_on_close = await get_session(room_id_on_close)
                    if session_data_on_close:
                        session_data_on_close.setdefault("previous_clients", []).append(client_id_on_close)
                        await set_session(room_id_on_close, session_data_on_close)
            else:
                logging.warning(f"[CLOSE] No se encontraron datos para {client_id_on_close} "
                                f"(sala {room_id_on_close}) tras desconexión.")

            if client_id_on_close in active_websockets and active_websockets[client_id_on_close] == self:
                del active_websockets[client_id_on_close]

            current_session_on_close = await get_session(room_id_on_close)
            if current_session_on_close:
                for peer_id in current_session_on_close.get('clients', []):
                    if peer_id != client_id_on_close and peer_id in active_websockets:
                        try:
                            active_websockets[peer_id].write_message({
                                'event': 'peer_disconnected_unexpectedly',
                                'client_id': client_id_on_close,
                                'message': 'Usuario perdió conexión. Esperando reconexión…'
                            })
                        except tornado.websocket.WebSocketClosedError:
                            pass
                        except Exception as e:
                            logging.error(f"[CLOSE] Excepción notificando a {peer_id}: {e}")
        tornado.ioloop.IOLoop.current().spawn_callback(_cleanup_async)

async def cleanup_sessions(): # Ahora es una corutina
    now = time.time()
    expired_session_ids = []
    
    all_s_keys = await get_all_session_keys()
    for room_id_key in all_s_keys:
        session_info = await get_session(room_id_key) # room_id_key ya es solo el ID
        if not session_info: continue

        has_dozing_mobile_in_session = False
        active_clients_in_session = [] # Clientes que aún existen en Redis
        connected_clients_in_session = [] # Clientes conectados actualmente
        
        for cid_in_sess in session_info.get('clients', []):
            client_info_sess = await get_client(cid_in_sess)
            if client_info_sess:
                active_clients_in_session.append(cid_in_sess)
                if cid_in_sess in active_websockets:
                    connected_clients_in_session.append(cid_in_sess)
                if not cid_in_sess.startswith("web-") and client_info_sess.get('status') == 'dozing':
                    has_dozing_mobile_in_session = True
        
        # Actualizar la lista de clientes en la sesión solo con los activos
        if len(active_clients_in_session) != len(session_info.get('clients', [])):
            session_info['clients'] = active_clients_in_session
            # Si después de esto la sesión queda vacía, se marcará para eliminar
        
        if not session_info['clients']: # Si la sesión está vacía después de filtrar
            expired_session_ids.append(room_id_key)
            logging.info(f"[CLEANUP] Sesión {room_id_key} marcada para expirar (sin clientes válidos)." )
            continue # Pasar a la siguiente sesión

        # LÓGICA ESPECIAL PARA SALAS INCOMPLETAS/ABANDONADAS
        session_age = now - session_info.get('created_at', now)
        session_status = session_info.get('status', 'unknown')
        last_activity_age = now - session_info.get('last_activity', now)
        
        # Caso 1: Sala creada pero nadie se unió (waiting_for_peer por mucho tiempo)
        if session_status == 'waiting_for_peer' and len(active_clients_in_session) == 1:
            # Solo 1 cliente en sala esperando, sin generar JWT
            INCOMPLETE_ROOM_TIMEOUT = 600  # 10 minutos para que alguien se una
            if session_age > INCOMPLETE_ROOM_TIMEOUT:
                expired_session_ids.append(room_id_key)
                logging.info(f"[CLEANUP] Sala incompleta {room_id_key} expirada (solo 1 cliente por {session_age:.0f}s, límite: {INCOMPLETE_ROOM_TIMEOUT}s)")
                continue
        
        # Caso 2: Sala sin clientes conectados por mucho tiempo (abandonada)
        if not connected_clients_in_session and not has_dozing_mobile_in_session:
            ABANDONED_ROOM_TIMEOUT = 1800  # 30 minutos sin nadie conectado
            if last_activity_age > ABANDONED_ROOM_TIMEOUT:
                expired_session_ids.append(room_id_key)
                logging.info(f"[CLEANUP] Sala abandonada {room_id_key} expirada (sin conexiones por {last_activity_age:.0f}s, límite: {ABANDONED_ROOM_TIMEOUT}s)")
                continue
        
        # Caso 3: Timeout normal (doze vs session timeout)
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
        if not client_info_cleanup: continue

        if client_info_cleanup.get('status') == 'pending_reconnect':
            is_web_cleanup = client_info_cleanup.get('is_web', False)
            timeout_for_client_cleanup = WEB_RECONNECT_TIMEOUT if is_web_cleanup else RECONNECT_GRACE_PERIOD
            
            if now - client_info_cleanup.get('pending_since', now) > timeout_for_client_cleanup:
                logging.info(f'[CLEANUP] Cliente {client_id_key} (web: {is_web_cleanup}) no reconectó ({timeout_for_client_cleanup}s). Limpiando.')
                room_id_of_client = client_info_cleanup.get('room_id')
                
                if room_id_of_client:
                    session_info_of_client = await get_session(room_id_of_client)
                    if session_info_of_client:
                        # Notificar al peer si está activo
                        for peer_cid in session_info_of_client.get('clients', []):
                            if peer_cid != client_id_key and peer_cid in active_websockets:
                                try: active_websockets[peer_cid].write_message({'event': 'peer_reconnect_failed', 'client_id': client_id_key}) 
                                except: pass
                        
                        if client_id_key in session_info_of_client.get('clients', []):
                            session_info_of_client['clients'].remove(client_id_key)
                            session_info_of_client['last_activity'] = now
                            if not session_info_of_client['clients']:
                                if room_id_of_client not in expired_session_ids: # Evitar duplicados
                                    expired_session_ids.append(room_id_of_client)
                                logging.info(f"[CLEANUP] Sesión {room_id_of_client} marcada para expirar (último cliente {client_id_key} no reconectó).")
                            else:
                                await set_session(room_id_of_client, session_info_of_client)
                
                was_dozing_before_pending = client_info_cleanup.get('dozing', False) # Chequear si *antes* de pending_reconnect estaba en doze
                await delete_client(client_id_key, keep_message_queue=was_dozing_before_pending)
                if was_dozing_before_pending:
                    logging.info(f"[CLEANUP] Cliente {client_id_key} estaba en doze antes de pending_reconnect, cola conservada.")
    
    # Limpiar las sesiones marcadas como expiradas
    for rid_to_delete in set(expired_session_ids): # Usar set para evitar procesar duplicados
        logging.info(f'[CLEANUP] Eliminando sesión expirada {rid_to_delete} y sus clientes restantes.')
        session_being_deleted = await get_session(rid_to_delete)
        if session_being_deleted:
            for cid_in_del_session in session_being_deleted.get('clients', []):
                if cid_in_del_session in active_websockets:
                    try: 
                        active_websockets[cid_in_del_session].write_message({'info': 'Sesión expirada por inactividad o limpieza.', 'code': 'SESSION_EXPIRED'})
                        active_websockets[cid_in_del_session].close(code=1000, reason="Sesión expirada")
                    except: pass # Ignorar errores al cerrar
                    # No es necesario borrar de active_websockets aquí, on_close lo hará
                
                # Determinar si se debe mantener la cola para este cliente
                # Por lo general, si la sesión expira, las colas de los clientes también se limpian,
                # a menos que haya una razón específica para conservarlas (ej. cliente en doze persistente).
                # Aquí, como la sesión completa expira, borramos las colas.
                await delete_client(cid_in_del_session, keep_message_queue=False) 
        await delete_session(rid_to_delete)

async def initialize_server_state_from_redis(): # Ahora es una corutina
    logging.info("[INIT] Inicializando estado del servidor desde Redis...")
    now = time.time()
    
    try:
        await init_redis_client() # Asegurar que el cliente Redis esté listo
    except Exception as e:
        logging.critical(f"[INIT] FALLO CRÍTICO al conectar con Redis durante la inicialización: {e}. El servidor podría no funcionar correctamente.")
        # Dependiendo de la criticidad, podrías querer salir de la aplicación aquí.
        # exit(1)
        return # No continuar si Redis no está

    client_keys_init = await get_all_client_keys()
    sessions_to_update_init = {} 
    clients_to_delete_init = []

    for cid_key_init in client_keys_init:
        client_data_init = await get_client(cid_key_init)
        if not client_data_init:
            logging.warning(f"[INIT] Cliente {cid_key_init} en keys pero sin datos. Omitiendo.")
            continue

        current_status_init = client_data_init.get('status')
        room_id_init = client_data_init.get('room_id')

        if current_status_init == 'pending_reconnect':
            pending_since_init = client_data_init.get('pending_since', 0)
            is_web_init_flag = client_data_init.get('is_web', False)
            timeout_init = WEB_RECONNECT_TIMEOUT if is_web_init_flag else RECONNECT_GRACE_PERIOD

            if now - pending_since_init > timeout_init:
                logging.info(f"[INIT] Cliente {cid_key_init} (web: {is_web_init_flag}) en pending_reconnect expiró ({timeout_init}s). Limpiando.")
                clients_to_delete_init.append(cid_key_init)
                if room_id_init:
                    if room_id_init not in sessions_to_update_init:
                        sessions_to_update_init[room_id_init] = await get_session(room_id_init)
                    if sessions_to_update_init[room_id_init] and cid_key_init in sessions_to_update_init[room_id_init].get('clients', []):
                        sessions_to_update_init[room_id_init]['clients'].remove(cid_key_init)
            # else: cliente sigue en pending_reconnect, se manejará por cleanup_sessions normal
        
        elif current_status_init in ['connected', 'waiting']:
            logging.info(f"[INIT] Cliente {cid_key_init} estaba {current_status_init}. Marcando como pending_reconnect.")
            client_data_init['status'] = 'pending_reconnect'
            client_data_init['pending_since'] = now
            client_data_init['is_web'] = cid_key_init.startswith("web-")
            await set_client(cid_key_init, client_data_init)
            if room_id_init:
                if room_id_init not in sessions_to_update_init:
                    sessions_to_update_init[room_id_init] = await get_session(room_id_init)
                if sessions_to_update_init[room_id_init]:
                     sessions_to_update_init[room_id_init]['last_activity'] = now 
        
        elif current_status_init == 'dozing':
            logging.info(f"[INIT] Cliente {cid_key_init} está en dozing. Manteniendo estado. Actualizando last_activity de sesión.")
            if room_id_init:
                if room_id_init not in sessions_to_update_init:
                    sessions_to_update_init[room_id_init] = await get_session(room_id_init)
                if sessions_to_update_init[room_id_init]:
                    sessions_to_update_init[room_id_init]['last_activity'] = now
        # else: estado desconocido o ya limpio, omitir

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

    for cid_del_init in clients_to_delete_init:
        # Para clientes eliminados en init, usualmente no se conserva la cola a menos que haya una lógica muy específica
        await delete_client(cid_del_init, keep_message_queue=False) 

    active_websockets.clear()
    logging.info("[INIT] Inicialización de estado del servidor desde Redis completada.")


async def main(): # Nueva función async main para gestionar el ciclo de vida
    try:
        await init_redis_client() # Inicializar Redis al arrancar
    except Exception as e:
        logging.critical(f"[MAIN] No se pudo inicializar Redis al arrancar: {e}. Saliendo.")
        return # Salir si Redis no está disponible

    app = tornado.web.Application([
        (r"/", MainHandler),
        (r"/ws", WebSocketHandler), # Considerar pasar config o cliente Redis si es necesario
        (r"/monitor", MonitorHandler),
        (r"/health", HealthHandler),
        (r"/api/status", HealthHandler),  # Alias para health check
        (r"/(test_client_txt.html)", tornado.web.StaticFileHandler, {"path": os.path.dirname(__file__)}),
    ], debug=(LOG_LEVEL == 'DEBUG')) # debug=True si LOG_LEVEL es DEBUG
    
    app.listen(SERVER_PORT, SERVER_HOST)
    logging.info(f'Servidor MinkaV2 (Async Redis) iniciado en http://{SERVER_HOST}:{SERVER_PORT}')
    
    # Inicializar estado desde Redis ANTES de que el cleanup_sessions empiece a correr con datos viejos
    await initialize_server_state_from_redis()
    
    # Iniciar tareas periódicas
    cleanup_task = PeriodicCallback(cleanup_sessions, 60000) # 60 segundos
    cleanup_task.start()
    
    # Mantener el servidor corriendo (Tornado IOLoop se encarga de esto implícitamente con app.listen)
    # Para un cierre elegante:
    shutdown_event = asyncio.Event()
    async def graceful_shutdown():
        logging.info("Iniciando cierre elegante...")
        cleanup_task.stop()
        # Cerrar websockets activos
        for client_id, ws in list(active_websockets.items()): # Iterar sobre una copia
            ws.close(code=1001, reason="Servidor apagándose")
            del active_websockets[client_id]
        await asyncio.sleep(1) # Dar tiempo para que los cierres se procesen
        await close_redis_client() # Cerrar conexión a Redis
        tornado.ioloop.IOLoop.current().stop()
        shutdown_event.set()
        logging.info("Cierre elegante completado.")

    def sig_handler(sig, frame):
        logging.warning(f"Recibida señal {sig}. Planificando cierre elegante.")
        # Es importante llamar a add_callback_from_signal para operaciones async desde un signal handler
        tornado.ioloop.IOLoop.current().add_callback_from_signal(lambda: asyncio.create_task(graceful_shutdown()))

    signal.signal(signal.SIGTERM, sig_handler)
    signal.signal(signal.SIGINT, sig_handler)

    # El IOLoop de Tornado ya está corriendo debido a app.listen()
    # Esperar al evento de shutdown para finalizar main()
    await shutdown_event.wait()

if __name__ == "__main__":
    # tornado.ioloop.IOLoop.current().start() # No iniciar aquí, main() lo gestiona
    try:
        asyncio.run(main()) # Usar asyncio.run para el punto de entrada principal
    except KeyboardInterrupt:
        logging.info("Proceso interrumpido por el usuario (KeyboardInterrupt).")
    finally:
        logging.info("Servidor MinkaV2 detenido.")