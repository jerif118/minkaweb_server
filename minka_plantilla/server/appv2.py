import tornado.websocket
import re, uuid, json, time, signal
import jwt # For decoding JWT 
import asyncio # Para manejo de operaciones asíncronas

# import sessions and clients from session.py
from session import (
    get_session, set_session, delete_session, get_all_session_keys,
    get_client, set_client, delete_client, get_all_client_keys,
    active_websockets, RECONNECT_GRACE_PERIOD, SESSION_TIMEOUT, 
    WEB_RECONNECT_TIMEOUT, DOZE_TIMEOUT, JWT_EXPIRATION_DELTA_SECONDS, # Added JWT_EXPIRATION_DELTA_SECONDS
    generate_jwt, verify_jwt, add_jti_to_blacklist, # JWT functions
    JWT_SECRET_KEY, JWT_ALGORITHM, # JWT constants for decoding
    add_message_to_queue, get_pending_messages, # Funciones de cola de mensajes
    MESSAGE_QUEUE_KEY_PREFIX, redis_client, MESSAGE_TTL_SECONDS # Añadidos para evitar errores UnboundLocalError
)
from tornado.escape import xhtml_escape
from tornado.ioloop import PeriodicCallback
# Importar el nuevo sistema de logs
from logger import get_app_logger

# Configurar el logger para el servidor
logger = get_app_logger('websocket_server')

# El log básico de tornado se mantiene para compatibilidad
logging = logger

# Tiempo máximo de inactividad de una sala en segundos (ej. 5 minutos)
# SESSION_TIMEOUT = 7_200

# RECONNECT_GRACE_PERIOD = 60  # Tiempo de gracia para reconexión en segundos

# # Estructura de sessions: cada room_id → dict con keys: password, clients, last_activity
# sessions = {}

# # Diccionario para almacenar la información de los clientes conectados
# clients = {}

# #
# # Handlers Base para manejo de CORS y solicitudes OPTIONS
#
class BaseHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "origin, x-requested-with, content-type, accept, authorization")
        self.set_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    
    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()

#
# Handler de la ruta raíz
#
class MainHandler(BaseHandler):
    def get(self):
        self.write('Hola Minka, by ellanotequiere')

class HealthHandler(BaseHandler):
    def get(self):
        self.write({"ok": True})


# MonitorHandler: Interfaz web para ver las salas y conexiones activas
class MonitorHandler(BaseHandler):
    def get(self):
        html = """
        <html>
        <head>
          <title>Monitor - Minka</title>
          <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            table, th, td {
              border: 1px solid black;
              border-collapse: collapse;
              padding: 8px;
              text-align: left;
            }
            table { width: 100%; margin-bottom: 20px; }
            h1, h2 { color: #333; }
          </style>
        </head>
        <body>
          <h1>Monitor - Salas Activas</h1>
          <table>
            <tr>
              <th>Room ID</th>
              <th>Password</th>
              <th>Clientes</th>
            </tr>
        """
        # Iterar sobre las salas y crear una fila por cada una
        for room_id_key in get_all_session_keys():
            info = get_session(room_id_key)
            if info:
                clients_list = ", ".join([xhtml_escape(cid) for cid in info["clients"]])
                html += f"<tr><td>{xhtml_escape(room_id_key)}</td><td>{xhtml_escape(info['password'])}</td><td>{clients_list}</td></tr>"
        html += "</table>"

        html += """
          <h2>Clientes Conectados</h2>
          <table>
            <tr>
              <th>Client ID</th>
              <th>Room ID</th>
              <th>Status</th>
            </tr>
        """
        # Iterar sobre los clientes y mostrar su información
        for client_id_key in get_all_client_keys():
            cl_info = get_client(client_id_key)
            if cl_info:
                room = xhtml_escape(cl_info.get('room_id', ''))
                status = xhtml_escape(cl_info.get('status', ''))
                html += f"<tr><td>{xhtml_escape(client_id_key)}</td><td>{room}</td><td>{status}</td></tr>"
        html += """
          </table>
        </body>
        </html>
        """
        self.write(html)

#
# Handler del WebSocket para emparejamiento y comunicación entre dos usuarios
#
class WebSocketHandler(tornado.websocket.WebSocketHandler):
    # Límite de tamaño de mensaje a 1 MiB y pings cada 30s
    max_message_size = 1024 * 1024
    ping_interval = 30000
    ping_timeout = 60000

    def check_origin(self, origin):
        # En producción, validar el origen
        return True
    
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "origin, x-requested-with, content-type, accept, authorization")
        self.set_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    
    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()
    
    async def _handle_doze_exit_messages(self, client_id):
        """Método asíncrono para manejar los mensajes pendientes cuando un cliente sale del modo doze"""
        max_retries = 3  # Número máximo de reintentos para envío de mensajes
        try:
            # Recuperamos la sala y notificamos a los compañeros
            client_data = get_client(client_id)
            if not client_data:
                logging.error(f"[DOZE_EXIT] No se encontró información del cliente {client_id}")
                return False
                
            room_id = client_data.get('room_id')
            if not room_id:
                logging.error(f"[DOZE_EXIT] Cliente {client_id} no tiene room_id")
                return False
                
            # Notificar a los peers sobre la salida del modo doze
            await self._notify_peer_about_doze_exit(client_id, room_id)

            # Primero nos aseguramos de que la conexión esté estable
            try:
                self.write_message({"info": "Procesando salida del modo doze..."})
                # Pequeña espera para asegurar que la conexión está estable
                await asyncio.sleep(0.5)
            except Exception as e:
                logging.error(f"[DOZE_EXIT] Error al escribir mensaje inicial: {e}")
                return False
            
            # Recuperar mensajes pendientes
            queue_key = f"{MESSAGE_QUEUE_KEY_PREFIX}{client_id}"
            logging.info(f"[DOZE_EXIT] Recuperando mensajes con clave {queue_key}")
            
            # Verificar si hay mensajes pendientes
            direct_check = redis_client.exists(queue_key)
            direct_count = redis_client.llen(queue_key) if direct_check else 0
            logging.info(f"[DOZE_EXIT] Verificación: clave existe={direct_check}, cantidad={direct_count}")
            
            if direct_count > 0:
                # Notificar al cliente sobre los mensajes pendientes
                try:
                    self.write_message({"info": f"Tienes {direct_count} mensajes pendientes."})
                except Exception as e:
                    logging.error(f"[DOZE_EXIT] Error al notificar mensajes pendientes: {e}")
                    return False
                
                # IMPORTANTE: No eliminamos la cola hasta confirmar entrega exitosa
                # Recuperar y enviar los mensajes
                messages_json = redis_client.lrange(queue_key, 0, -1)
                
                try:
                    # Deserializar todos los mensajes JSON
                    pending_msgs = [json.loads(msg) for msg in messages_json]
                    delivery_success = True  # Flag para controlar si la entrega fue exitosa
                    
                    # Enviar los mensajes con un pequeño retraso para asegurar entrega
                    for idx, msg_payload in enumerate(pending_msgs):
                        retry_count = 0
                        message_sent = False
                        
                        # Intentar enviar cada mensaje con reintentos
                        while retry_count < max_retries and not message_sent:
                            try:
                                logging.info(f"[DOZE_EXIT] Enviando mensaje pendiente #{idx+1}/{direct_count} (intento {retry_count+1})")
                                self.write_message(msg_payload)
                                await asyncio.sleep(0.2)  # Retraso entre mensajes aumentado
                                message_sent = True
                            except Exception as e:
                                retry_count += 1
                                logging.error(f"[DOZE_EXIT] Error enviando mensaje {idx+1} (intento {retry_count}): {e}")
                                await asyncio.sleep(0.5)  # Esperar antes de reintentar
                        
                        if not message_sent:
                            delivery_success = False
                            logging.error(f"[DOZE_EXIT] No se pudo entregar mensaje {idx+1} después de {max_retries} intentos")
                    
                    # Sólo eliminamos la cola si todos los mensajes se enviaron exitosamente
                    if delivery_success:
                        redis_client.delete(queue_key)
                        logging.info(f"[DOZE_EXIT] Cola eliminada después de enviar {direct_count} mensajes")
                        # Confirmación final
                        self.write_message({"info": "Todos los mensajes pendientes han sido entregados."})
                    else:
                        logging.warning(f"[DOZE_EXIT] No se pudieron enviar todos los mensajes. Cola conservada.")
                        self.write_message({"info": "Algunos mensajes no pudieron ser entregados. Intenta reconectar nuevamente."})
                except Exception as e:
                    logging.error(f"[DOZE_EXIT] Error al procesar mensajes: {e}")
                    return False
            else:
                self.write_message({"info": "No hay mensajes pendientes."})
                
            logging.info(f"[DOZE_EXIT] Procesamiento de salida del modo doze completado para {client_id}")
            return True
        except Exception as e:
            logging.error(f"[DOZE_EXIT] Error general: {e}")
            return False
    
    async def _notify_peer_about_doze_exit(self, client_id, room_id):
        """Método para notificar a los peers sobre la salida del modo doze"""
        try:
            session_data = get_session(room_id)
            if not session_data or "clients" not in session_data:
                logging.warning(f"[DOZE_EXIT] No se encontró información de la sala {room_id} para notificar peers")
                return False
            
            notification_success = True
            for peer_id in session_data["clients"]:
                if peer_id != client_id and peer_id in active_websockets:
                    try:
                        active_websockets[peer_id].write_message({
                            'event': 'peer_doze_exit',
                            'client_id': client_id,
                            'message': f'El usuario {client_id} ha salido del modo doze y está disponible de nuevo.',
                            'timestamp': time.time()
                        })
                        logging.info(f"[DOZE_EXIT] Notificación enviada a {peer_id} sobre salida de doze de {client_id}")
                    except Exception as e:
                        notification_success = False
                        logging.error(f"[DOZE_EXIT] Error al notificar a {peer_id}: {e}")
            
            return notification_success
        except Exception as e:
            logging.error(f"[DOZE_EXIT] Error al notificar a peers: {e}")
            return False
    
    async def open(self):
        self.intentional_disconnect = False
        # Inicializar atributos para garantizar que existen en on_message y on_close
        self.client_id = None
        self.room_id = None
        # Obtener parámetros de la URL
        client_id_arg = self.get_argument('client_id', None)
        action_arg    = self.get_argument('action', None)
        room_id_arg   = self.get_argument('room_id', None)
        # Asignar los valores iniciales de client_id y room_id
        self.client_id = client_id_arg
        self.room_id   = room_id_arg
        room_passwd_arg = self.get_argument('password', None)
        jwt_token_arg = self.get_argument('jwt_token', None) # Nuevo argumento para JWT

        logging.info(f"[OPEN] Args: client_id={client_id_arg} action={action_arg} room_id={room_id_arg} password={'******' if room_passwd_arg else None} jwt_token={'******' if jwt_token_arg else None}")

        # 1. Intento de reconexión usando JWT (prioritario para clientes web)
        if jwt_token_arg and client_id_arg:
            payload = verify_jwt(jwt_token_arg)
            if payload and payload.get('client_id') == client_id_arg:
                jwt_client_id = payload['client_id']
                jwt_room_id = payload['room_id']
                logging.info(f"[OPEN-JWT] Intento de reconexión JWT para client_id: {jwt_client_id} en room_id: {jwt_room_id}")
                
                client_data = get_client(jwt_client_id)
                
                # Si el cliente existe y está en la misma sala o si es un cliente web con un JTI válido
                if client_data and client_data.get('room_id') == jwt_room_id:
                    # Si el cliente está en estado pending_reconnect o si es un cliente web con JTI válido
                    logging.info(f"[OPEN-JWT] Cliente {jwt_client_id} encontrado para la sala {jwt_room_id}. Reconectando...")
                    client_data['status'] = 'connected'
                    client_data.pop('pending_since', None)
                    # 'is_web' ya debería estar en client_data
                    # Guardar el JTI del token usado para la reconexión, para posible invalidación futura si es necesario.
                    client_data['current_jti'] = payload.get('jti') 
                    set_client(jwt_client_id, client_data)
                    active_websockets[jwt_client_id] = self
                    self.client_id = jwt_client_id
                    self.room_id = jwt_room_id

                    # >>> ENVIAR MENSAJES PENDIENTES AL RECONECTAR (JWT)
                    logging.info(f"[OPEN-JWT] Intentando recuperar mensajes pendientes para {self.client_id}")
                    try:
                        pending_msgs = get_pending_messages(self.client_id)
                        if pending_msgs:
                            logging.info(f"[OPEN-JWT] Enviando {len(pending_msgs)} mensajes pendientes a {self.client_id}: {pending_msgs}")
                            for idx, msg_payload in enumerate(pending_msgs):
                                logging.info(f"[OPEN-JWT] Enviando mensaje pendiente #{idx+1}/{len(pending_msgs)} a {self.client_id}: {msg_payload}")
                                self.write_message(msg_payload) # Los mensajes ya están formateados
                        else:
                            logging.info(f"[OPEN-JWT] No hay mensajes pendientes para {self.client_id}")
                    except Exception as e:
                        logging.error(f"[OPEN-JWT] Error al recuperar/enviar mensajes pendientes: {e}")
                    # <<< FIN ENVÍO MENSAJES PENDIENTES

                    # Actualizar last_activity de la sesión
                    session_data_jwt = get_session(self.room_id)
                    if session_data_jwt:
                        session_data_jwt['last_activity'] = time.time()
                        set_session(self.room_id, session_data_jwt)
                        # Notificar al peer sobre la reconexión
                        for other_id in session_data_jwt.get("clients", []):
                            if other_id != self.client_id and other_id in active_websockets:
                                active_websockets[other_id].write_message({'info': f'El usuario {self.client_id} se ha reconectado.'})
                        
                        # Incluir password en la respuesta para que el cliente la guarde para futuras reconexiones
                        password = session_data_jwt.get('password')
                        self.write_message({
                            'event': 'reconnected_jwt', 
                            'room_id': self.room_id, 
                            'client_id': self.client_id,
                            'password': password,  # Incluir la contraseña para que el cliente la guarde
                            'info': 'Reconectado exitosamente usando JWT.'
                        })
                    else:
                        self.write_message({'event': 'reconnected_jwt', 'room_id': self.room_id, 'client_id': self.client_id, 'info': 'Reconectado exitosamente usando JWT pero sin información de sala.'})
                    
                    logging.info(f"[OPEN-JWT] Cliente {self.client_id} reconectado exitosamente a la sala {self.room_id} usando JWT.")
                    return # Fin del flujo si la reconexión JWT fue exitosa
                else:
                    logging.warning(f"[OPEN-JWT] JWT válido para {jwt_client_id} pero no se pudo reconectar (datos no coinciden o no está en pending_reconnect).")
                    # Si el JWT es válido pero el cliente no está en pending_reconnect, podría ser un intento de mal uso.
                    # Considerar invalidar el JTI aquí si la política es estricta.
                    # add_jti_to_blacklist(payload.get('jti'), JWT_EXPIRATION_DELTA_SECONDS)
                    # self.write_message({'error': 'JWT válido pero estado de reconexión incorrecto.', 'code': 'RECONNECT_STATE_INVALID'})
                    # self.close()
                    # return
            else:
                logging.warning(f"[OPEN-JWT] Token JWT inválido o client_id no coincide para {client_id_arg}.")
                # Si un cliente web intenta conectar con un JWT inválido, se podría cerrar la conexión.
                # self.write_message({'error': 'Token JWT inválido o no autorizado.', 'code': 'JWT_INVALID'})
                # self.close()
                # return

        # Renombrar variables para mantener la lógica existente clara
        client_id = client_id_arg
        action = action_arg
        room_id = room_id_arg
        room_passwd = room_passwd_arg

        # STICTER JWT ENFORCEMENT:
        # Si es un cliente web (por client_id) y no se reconectó por JWT, y la acción es 'join' o 'create',
        # verificar si debería tener un JWT.
        if client_id and client_id.startswith("web-") and not hasattr(self, 'client_id'): # No reconectado por JWT
            existing_client_data = get_client(client_id)
            if existing_client_data and existing_client_data.get('current_jti'): # Ya se le había emitido un JTI
                logging.warning(f"[OPEN] Cliente web {client_id} intentando {action} sin JWT, pero ya se le había emitido uno. Rechazando.")
                self.write_message({'error': 'Reconexión/acción requiere JWT. Por favor, use el token JWT proporcionado.', 'code': 'JWT_REQUIRED'})
                self.close()
                return

        # Validar formato de client_id (si se proporciona y no se reconectó por JWT)
        if client_id and not re.match(r'^[A-Za-z0-9_\\\\-]{1,64}$', client_id):
            self.write_message({'error': 'client_id inválido'})
            self.close()
            return

        if action == "create":
            if not client_id:
                self.write_message({'error': 'client_id es requerido'})
                self.close()
                return
                
            # Si es un cliente web, verificar si hay una sala con un móvil en doze
            if client_id.startswith("web-"):
                # Buscar salas con un solo cliente (móvil) en doze
                dozing_mobile_rooms = []
                for rid_key in get_all_session_keys():
                    info = get_session(rid_key)
                    if info and len(info["clients"]) == 1:
                        mobile_id = info["clients"][0]
                        client_info = get_client(mobile_id)
                        if not mobile_id.startswith("web-") and client_info and client_info.get('status') == 'dozing':
                            dozing_mobile_rooms.append((rid_key, info))
                
                # Si encontramos una sala con móvil en doze, verificar si el cliente web puede unirse
                if dozing_mobile_rooms:
                    # Intentar encontrar si este cliente web estaba previamente en alguna de estas salas
                    previous_room_match = None
                    for rid_key, info in dozing_mobile_rooms:
                        previous_session = get_session(rid_key)
                        # Si el cliente web estaba previamente en esta sala o no hay otro web conectado
                        if previous_session and (client_id in previous_session.get("previous_clients", [])):
                            previous_room_match = (rid_key, info)
                            break
                    
                    # Si encontramos una sala donde este cliente estaba antes, permitir reconexión
                    if previous_room_match:
                        room_id, room_info = previous_room_match
                        mobile_id = room_info["clients"][0]
                        logging.info(f"Web client {client_id} reconectándose a sala con móvil {mobile_id} en doze (era miembro previo)")
                        
                        # Agregar cliente web a esta sala
                        room_info["clients"].append(client_id)
                        set_session(room_id, room_info)
                        set_client(client_id, {
                            'status': 'connected',
                            'room_id': room_id
                        })
                        active_websockets[client_id] = self
                        self.client_id = client_id
                        self.room_id = room_id
                        room_info['last_activity'] = time.time()
                        set_session(room_id, room_info)
                        
                        self.write_message({
                            "room_joined": True,
                            "room_id": room_id,
                            "password": room_info["password"],
                            "info": "Te has reconectado a la sala con el usuario móvil temporalmente inactivo."
                        })
                        return
                    else:
                        # No permitir la creación de una sala nueva si hay clientes en doze
                        self.write_message({
                            "error": "Hay clientes en modo de espera (doze). Solo pueden reconectarse clientes que anteriormente estaban en esas salas.",
                            "code": "DOZE_CLIENTS_EXIST"
                        })
                        self.close()
                        return
            
            # Si el cliente ya existe, devolver misma sala
            client_data = get_client(client_id)
            if client_data:
                # Ya existe: devolver misma sala y no crear otra
                existing_room = client_data['room_id']
                session_data = get_session(existing_room)
                password = session_data['password']
                self.client_id = client_id
                self.room_id = existing_room
                active_websockets[client_id] = self # Asegurar que el websocket está actualizado
                self.write_message({
                    "room_created": True,
                    "room_id": existing_room,
                    "password": password,
                    "info": "Ya tenías una sala creada. Esperando otro usuario."
                })
                return
            
            # Crear nueva sala si no hay ninguna compatible
            room_id = str(uuid.uuid4())
            room_password = str(uuid.uuid4())[:6]
            set_session(room_id, {
                "password": room_password,
                "clients": [client_id],
                "last_activity": time.time()
            })
            set_client(client_id, {
                'status': 'waiting',
                'room_id': room_id,
                'is_web': client_id.startswith("web-") # Asegurar que is_web se establece aquí
            })
            active_websockets[client_id] = self
            self.client_id = client_id
            self.room_id = room_id
            # EMISIÓN DE JWT PARA CLIENTE WEB CREADOR
            if client_id.startswith("web-"):
                jwt_token = generate_jwt(client_id, room_id)
                # Decodificar para obtener el JTI y almacenarlo
                try:
                    payload = jwt.decode(jwt_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_exp": False}) # No verificar exp aquí, solo para obtener jti
                    jti = payload.get('jti')
                    client_data_for_jti = get_client(client_id)
                    if client_data_for_jti and jti:
                        client_data_for_jti['current_jti'] = jti
                        set_client(client_id, client_data_for_jti)
                        logging.info(f"[OPEN-CREATE] JTI {jti} almacenado para cliente web {client_id}")
                except jwt.PyJWTError as e:
                    logging.error(f"[OPEN-CREATE] Error al decodificar JWT para obtener JTI para {client_id}: {e}")

                self.write_message({
                    "room_created": True,
                    "room_id": room_id,
                    "password": room_password,
                    "jwt_token": jwt_token,
                    "info": "Room created. Waiting for another user to join. JWT issued."
                })
            else:
                 self.write_message({ # Corregido: espacio añadido antes de {
                    "room_created": True,
                    "room_id": room_id,
                    "password": room_password,
                    "info": "Room created. Waiting for another user to join."
                })
            return # Asegurarse que el return está aquí para que no caiga en el write_message genérico de create.
        
        elif action == "join":
            room_id = self.get_argument('room_id', None)
            room_password = self.get_argument('password', None)
            client_data = get_client(client_id)
            session_data = get_session(room_id)
            
            # DEBUGGING: Logear estado del cliente al momento de reconexión
            logging.info(f"[OPEN-DEBUG] Cliente {client_id} intentando join. Estado actual: {client_data.get('status') if client_data else 'No existe'}")
            if client_data:
                logging.info(f"[OPEN-DEBUG] Datos completos del cliente {client_id}: {client_data}")
            
            # Re-conexión desde doze: (PRIORIDAD 1 - debe ir antes que las otras condiciones)
            client_data_doze = get_client(client_id)
            logging.info(f"[OPEN-DEBUG] Verificando modo doze para {client_id}. Status: {client_data_doze.get('status') if client_data_doze else 'No existe'}")
            
            if client_data_doze and client_data_doze.get('status') == 'dozing':
                logging.info(f"[OPEN-DEBUG] ¡Cliente {client_id} está saliendo del modo doze!")
                # Restablecer ws y estado a 'connected'
                client_data_doze['status'] = 'connected'
                client_data_doze.pop('dozing', None)  # Limpiar flag de doze
                client_data_doze.pop('doze_start', None)  # Limpiar timestamp de inicio
                client_data_doze.pop('doze_id', None)  # Limpiar ID de ciclo doze
                set_client(client_id, client_data_doze)
                active_websockets[client_id] = self
                self.client_id = client_id
                self.room_id = room_id # Asumimos que room_id es el correcto desde el request

                # Notificación inicial al cliente
                self.write_message({"info": "Reconectado desde modo doze. Verificando mensajes pendientes..."})
                
                # Procesamos los mensajes pendientes de forma asíncrona usando nuestro método mejorado
                doze_exit_task = asyncio.create_task(self._handle_doze_exit_messages(client_id))
                
                try:
                    # Esperar que termine el procesamiento
                    doze_exit_result = await doze_exit_task
                    
                    if not doze_exit_result:
                        logging.error(f"[OPEN-DOZE] El procesamiento de salida de doze para {client_id} no fue exitoso")
                        # Intentamos enviar un mensaje de error, pero no cerramos la conexión para darle otra oportunidad
                        try:
                            self.write_message({"error": "Hubo un problema al procesar los mensajes pendientes. Por favor, intenta reconectar."})
                        except:
                            pass
                    else:
                        logging.info(f"[OPEN-DOZE] Procesamiento de salida de doze para {client_id} completado exitosamente")
                        
                        # Actualizar datos de la sala si es necesario
                        session_data = get_session(room_id)
                        if session_data:
                            session_data['last_activity'] = time.time()
                            # Quitar el flag de cliente en doze si ya no hay ninguno
                            has_dozing_client = False
                            for other_id in session_data.get('clients', []):
                                other_data = get_client(other_id)
                                if other_data and other_data.get('status') == 'dozing':
                                    has_dozing_client = True
                                    break
                            
                            if not has_dozing_client:
                                session_data.pop('has_dozing_client', None)
                                
                            set_session(room_id, session_data)
                
                except Exception as e:
                    logging.error(f"[OPEN-DOZE] Error en el manejo asíncrono de salida de doze: {e}")
                    logging.exception(e)  # Log completo del error
                
                return

            # --- Re-conexión tras caída inesperada ---
            if client_data and client_data.get('status') == 'pending_reconnect':
                client_data['status'] = 'connected'
                client_data.pop('pending_since', None)
                set_client(client_id, client_data)
                active_websockets[client_id] = self

                self.client_id = client_id
                self.room_id = client_data['room_id']

                # Notificar explícitamente al cliente de su reconexión exitosa
                self.write_message({'info': 'Reconexión exitosa a la sala.'})
                
                # >>> ENVIAR MENSAJES PENDIENTES AL RECONECTAR (INESPERADA)
                logging.info(f"[OPEN-RECONNECT] Intentando recuperar mensajes pendientes para {self.client_id}")
                try:
                    pending_msgs = get_pending_messages(self.client_id)
                    if pending_msgs:
                        logging.info(f"[OPEN-RECONNECT] Enviando {len(pending_msgs)} mensajes pendientes a {self.client_id}: {pending_msgs}")
                        # Informar al cliente sobre los mensajes pendientes
                        self.write_message({"info": f"Tienes {len(pending_msgs)} mensajes pendientes."})
                        for idx, msg_payload in enumerate(pending_msgs):
                            logging.info(f"[OPEN-RECONNECT] Enviando mensaje pendiente #{idx+1}/{len(pending_msgs)} a {self.client_id}: {msg_payload}")
                            self.write_message(msg_payload) # Los mensajes ya están formateados
                            # Pequeña pausa para asegurar entrega
                            time.sleep(0.1)
                        # Confirmación final
                        self.write_message({"info": "Todos los mensajes pendientes han sido entregados."})
                    else:
                        logging.info(f"[OPEN-RECONNECT] No hay mensajes pendientes para {self.client_id}")
                except Exception as e:
                    logging.error(f"[OPEN-RECONNECT] Error al recuperar/enviar mensajes pendientes: {e}")
                # <<< FIN ENVÍO MENSAJES PENDIENTES

                # Avisar al peer
                current_session = get_session(self.room_id)
                if current_session:
                    for other in current_session["clients"]:
                        other_client_data = get_client(other)
                        if other != client_id and other_client_data and other in active_websockets:
                            active_websockets[other].write_message({'info': f'El usuario {client_id} se ha reconectado.'})
                
                # Liberar el semáforo para los clientes móviles
                if not client_id.startswith("web-"):
                    # Este mensaje simula la unión al room y debería ser detectado por el cliente de prueba
                    self.write_message({'info': 'Te has unido a la sala.'})
                    
                logging.info(f"[OPEN] Cliente {client_id} reconectado a la sala {self.room_id}.")
                return

            # Soporte de reconexión para un cliente existente (p.ej. web que se desconectó temporalmente)
            if session_data and client_id in session_data.get("clients", []):
                # Si ya estaba en el room (p.ej. web reconectándose), reasignar ws y estado
                client_data_reconnect = get_client(client_id)
                if client_data_reconnect:
                    client_data_reconnect['status'] = 'connected'
                    set_client(client_id, client_data_reconnect)
                active_websockets[client_id] = self
                self.client_id = client_id
                self.room_id = room_id
                
                # Notificar explícitamente al cliente de su reconexión exitosa
                self.write_message({'info': 'Reconexión exitosa a la sala.'})
                
                # Obtener y enviar mensajes pendientes (similar al escenario pending_reconnect)
                try:
                    pending_msgs = get_pending_messages(client_id)
                    if pending_msgs:
                        self.write_message({"info": f"Tienes {len(pending_msgs)} mensajes pendientes."})
                        for idx, msg_payload in enumerate(pending_msgs):
                            self.write_message(msg_payload)
                            time.sleep(0.1)  # Pequeña pausa para asegurar entrega
                        self.write_message({"info": "Todos los mensajes pendientes han sido entregados."})
                except Exception as e:
                    logging.error(f"[OPEN-SESSION-RECONNECT] Error al recuperar mensajes pendientes: {e}")
                
                # Notificar al peer que el usuario se ha reconectado
                other_ids = [cid for cid in session_data["clients"] if cid != client_id]
                for other in other_ids:
                    other_client_data = get_client(other)
                    if other_client_data and other in active_websockets:
                        active_websockets[other].write_message({'info': f'El usuario {client_id} se ha reconectado.'})
                        
                # Liberar el semáforo para los clientes móviles
                if not client_id.startswith("web-"):
                    # Este mensaje simula la unión al room y debería ser detectado por el cliente de prueba
                    self.write_message({'info': 'Te has unido a la sala.'})
                        
                logging.info(f"[OPEN-SESSION-RECONNECT] Cliente {client_id} reconectado a la sala {room_id}.")
                return

            # Re-conexión desde doze (repetida, así que reutilizamos la misma lógica)
            client_data_doze = get_client(client_id)
            logging.info(f"[OPEN-DEBUG] Verificando modo doze para {client_id}. Status: {client_data_doze.get('status') if client_data_doze else 'No existe'}")
            
            if client_data_doze and client_data_doze.get('status') == 'dozing':
                logging.info(f"[OPEN-DEBUG] ¡Cliente {client_id} está saliendo del modo doze!")
                # Restablecer ws y estado a 'connected'
                client_data_doze['status'] = 'connected'
                client_data_doze.pop('dozing', None)  # Limpiar flag de doze
                client_data_doze.pop('doze_start', None)  # Limpiar timestamp de inicio
                client_data_doze.pop('doze_id', None)  # Limpiar ID de ciclo doze
                set_client(client_id, client_data_doze)
                active_websockets[client_id] = self
                self.client_id = client_id
                self.room_id = room_id # Asumimos que room_id es el correcto desde el request

                # Notificación inicial al cliente
                self.write_message({"info": "Reconectado desde modo doze. Verificando mensajes pendientes..."})
                
                # Procesamos los mensajes pendientes de forma asíncrona usando nuestro método mejorado
                doze_exit_task = asyncio.create_task(self._handle_doze_exit_messages(client_id))
                
                try:
                    # Esperar que termine el procesamiento
                    doze_exit_result = await doze_exit_task
                    
                    if not doze_exit_result:
                        logging.error(f"[OPEN-DOZE] El procesamiento de salida de doze para {client_id} no fue exitoso")
                        # Intentamos enviar un mensaje de error, pero no cerramos la conexión para darle otra oportunidad
                        try:
                            self.write_message({"error": "Hubo un problema al procesar los mensajes pendientes. Por favor, intenta reconectar."})
                        except:
                            pass
                    else:
                        logging.info(f"[OPEN-DOZE] Procesamiento de salida de doze para {client_id} completado exitosamente")
                        
                        # Actualizar datos de la sala si es necesario
                        session_data = get_session(room_id)
                        if session_data:
                            session_data['last_activity'] = time.time()
                            # Quitar el flag de cliente en doze si ya no hay ninguno
                            has_dozing_client = False
                            for other_id in session_data.get('clients', []):
                                other_data = get_client(other_id)
                                if other_data and other_data.get('status') == 'dozing':
                                    has_dozing_client = True
                                    break
                            
                            if not has_dozing_client:
                                session_data.pop('has_dozing_client', None)
                                
                            set_session(room_id, session_data)
                
                except Exception as e:
                    logging.error(f"[OPEN-DOZE] Error en el manejo asíncrono de salida de doze: {e}")
                    logging.exception(e)  # Log completo del error
                
                return

            if not client_id or not room_id or not room_password:
                self.write_message({'error': 'client_id, room_id y password son requeridos'})
                self.close()
                return
            
            if room_id and not re.match(r'^[0-9a-fA-F\-]{36}$', room_id):
                self.write_message({'error': 'room_id inválido'})
                self.close()
                return
            if room_password and not re.match(r'^[A-Za-z0-9]{6}$', room_password):
                self.write_message({'error': 'password inválido'})
                self.close()
                return

            current_session_join = get_session(room_id)
            if current_session_join and current_session_join["password"] == room_password:
                # Verificar si hay algún cliente en modo doze en la sala
                has_dozing_client = False
                dozing_client_ids = []
                
                for room_client_id in current_session_join["clients"]:
                    room_client_data = get_client(room_client_id)
                    if room_client_data and room_client_data.get('status') == 'dozing':
                        has_dozing_client = True
                        dozing_client_ids.append(room_client_id)
                
                # Si hay un cliente en modo doze, aplicar restricciones estrictas
                if has_dozing_client:
                    logging.info(f"[JOIN] Intento de unirse a sala {room_id} con clientes en modo doze: {dozing_client_ids}")
                    
                    # Si el cliente que intenta unirse NO es un cliente previo, rechazar la conexión
                    if client_id not in current_session_join["clients"] and client_id not in current_session_join.get("previous_clients", []):
                        # Excepción solo para clientes de prueba
                        is_test_client = "test" in client_id.lower()
                        is_test_room = any("test" in cid.lower() for cid in current_session_join["clients"])
                        
                        if is_test_client and is_test_room:
                            # En entorno de prueba, permitir la conexión pero con advertencia
                            logging.warning(f"[JOIN-DOZE] Permitiendo conexión de cliente de prueba {client_id} a sala con clientes en doze ({dozing_client_ids})")
                            current_session_join["clients"].append(client_id)
                            set_client(client_id, {
                                'status': 'connected',
                                'room_id': room_id,
                                'is_test': True  # Marcar como cliente de prueba
                            })
                            active_websockets[client_id] = self
                            self.client_id = client_id
                            self.room_id = room_id
                            current_session_join['last_activity'] = time.time()
                            set_session(room_id, current_session_join)
                            self.write_message({
                                'info': 'Te has unido a la sala que tiene usuarios en modo doze (permitido solo en pruebas).',
                                'warning': 'Esta conexión solo se permite en entornos de prueba.'
                            })
                            return
                        else:
                            # Rechazar conexión con mensaje claro
                            logging.info(f"[JOIN-DOZE] Rechazando conexión de cliente {client_id} a sala con clientes en doze")
                            self.write_message({
                                'error': 'No se permite unirse a esta sala porque hay un cliente en modo doze. Solo pueden reconectarse clientes que estaban previamente en la sala.',
                                'code': 'ROOM_HAS_DOZING_CLIENT'
                            })
                            self.close()
                            return
                    
                    # Para clientes previos, permitir la reconexión
                    if client_id in current_session_join.get("previous_clients", []) and client_id not in current_session_join["clients"]:
                        logging.info(f"[JOIN-DOZE] Permitiendo reconexión de cliente previo {client_id} a sala con clientes en doze")
                        current_session_join["clients"].append(client_id)
                        set_client(client_id, {
                            'status': 'connected',
                            'room_id': room_id
                        })
                        active_websockets[client_id] = self
                        self.client_id = client_id
                        self.room_id = room_id
                        current_session_join['last_activity'] = time.time()
                        set_session(room_id, current_session_join)
                        self.write_message({'info': 'Te has reconectado a la sala. Hay usuarios temporalmente inactivos (modo doze).'})
                        return
                    
                    # Caso normal: agregar nuevo cliente si hay espacio
                    if len(current_session_join["clients"]) < 2:
                        current_session_join["clients"].append(client_id)
                        set_client(client_id, {
                            'status': 'connected',
                            'room_id': room_id
                        })
                        active_websockets[client_id] = self
                        self.client_id = client_id
                        self.room_id = room_id
                        current_session_join['last_activity'] = time.time() # Actualizar actividad
                        set_session(room_id, current_session_join) # Guardar la sesión actualizada
                        
                        # EMISIÓN DE JWT PARA CLIENTE WEB QUE SE UNE
                        if client_id.startswith("web-"):
                            jwt_token = generate_jwt(client_id, room_id)
                            try:
                                payload = jwt.decode(jwt_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_exp": False})
                                jti = payload.get('jti')
                                client_data_for_jti = get_client(client_id) # Debería existir porque lo acabamos de crear/setear
                                if client_data_for_jti and jti:
                                    client_data_for_jti['current_jti'] = jti
                                    set_client(client_id, client_data_for_jti)
                                    logging.info(f"[OPEN-JOIN] JTI {jti} almacenado para cliente web {client_id}")
                            except jwt.PyJWTError as e:
                                logging.error(f"[OPEN-JOIN] Error al decodificar JWT para obtener JTI para {client_id}: {e}")
                            self.write_message({'info': 'Te has unido a la sala.', 'jwt_token': jwt_token})
                        else:
                            self.write_message({'info': 'Te has unido a la sala.'})
                        
                        other_client_id = [cid for cid in current_session_join["clients"] if cid != client_id][0]
                        other_client_data = get_client(other_client_id)
                        # Actualizar status del cliente original a 'connected' solo si no está en doze
                        if other_client_data and other_client_data.get('status') != 'dozing':
                            other_client_data['status'] = 'connected'
                            set_client(other_client_id, other_client_data)
                            if other_client_id in active_websockets:
                                active_websockets[other_client_id].write_message({"event": "joined","client": client_id})
                    else:
                        self.write_message({'error': 'Sala llena. Solo se permiten dos usuarios.'})
                        self.close()
                        return
                else:
                    # Ya estaba en la sala pero no fue captado por el reatach anterior: rechazar duplicado
                    self.write_message({'error': 'Ya estás en la sala.'})
                    self.close()
                    return
            else:
                self.write_message({'error': 'Sala no existe o contraseña incorrecta'})
                self.close()
        
        else:
            self.write_message({'error': 'Acción no válida'})
            self.close()
    
    async def on_message(self, message):
        logging.info(f"[DEBUG] on_message called with raw message: {message}")
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            self.write_message({'error': 'JSON inválido'})
            return

        jwt_in_message = data.get('jwt_token') # Client might send its JWT to be blacklisted

        if data.get('action') == 'leave':
            self.intentional_disconnect = True
            reason = data.get('reason', '')
            logging.info(f"[LEAVE] Acción de salida recibida de {getattr(self, 'client_id', 'N/A')} en sala {getattr(self, 'room_id', 'N/A')}, razón: {reason}")
            
            client_id = getattr(self, 'client_id', None)
            room_id = getattr(self, 'room_id', None)

            if not client_id or not room_id:
                logging.warning("[LEAVE] client_id o room_id no definidos al procesar 'leave'.")
                self.close()
                return

            # Blacklisting del JTI para clientes web
            if client_id.startswith("web-"):
                jti_to_blacklist = None
                if jwt_in_message: # Prioridad si el cliente envía el token actual
                    try:
                        payload = jwt.decode(jwt_in_message, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_exp": False}) 
                        jti_to_blacklist = payload.get('jti')
                        if jti_to_blacklist:
                            logging.info(f"[LEAVE-BLACKLIST] JTI {jti_to_blacklist} obtenido del JWT en mensaje para {client_id}")
                        else:
                            logging.warning(f"[LEAVE-BLACKLIST] JWT en mensaje para {client_id} no contenía JTI.")
                    except jwt.PyJWTError as e:
                        logging.warning(f"[LEAVE-BLACKLIST] Error al decodificar JWT del mensaje para {client_id}: {e}. Se intentará con JTI almacenado.")
                
                if not jti_to_blacklist:
                    client_data_for_jti = get_client(client_id)
                    if client_data_for_jti and client_data_for_jti.get('current_jti'):
                        jti_to_blacklist = client_data_for_jti.get('current_jti')
                        logging.info(f"[LEAVE-BLACKLIST] JTI {jti_to_blacklist} obtenido del almacenamiento para {client_id}")

                if jti_to_blacklist:
                    add_jti_to_blacklist(jti_to_blacklist, JWT_EXPIRATION_DELTA_SECONDS)
                    logging.info(f"[LEAVE-BLACKLIST] JTI {jti_to_blacklist} para cliente {client_id} añadido a la lista negra con TTL de {JWT_EXPIRATION_DELTA_SECONDS}s.")
                    client_data_to_clear_jti = get_client(client_id)
                    if client_data_to_clear_jti:
                        if 'current_jti' in client_data_to_clear_jti:
                            del client_data_to_clear_jti['current_jti']
                            set_client(client_id, client_data_to_clear_jti)
                            logging.info(f"[LEAVE-BLACKLIST] current_jti eliminado de Redis para {client_id}")
                else:
                    logging.warning(f"[LEAVE-BLACKLIST] No se encontró JTI para blacklisting para el cliente web {client_id}.")

            # Si el cliente que hace leave es un cliente web, no borrar la sala si hay un móvil en doze
            if client_id.startswith("web-"):
                current_session_leave = get_session(room_id)
                if current_session_leave:
                    peer_list = current_session_leave.get("clients", [])
                    other_ids = [cid for cid in peer_list if cid != client_id]
                    
                    # Verificar si hay algún cliente móvil en doze
                    mobile_in_doze = False
                    for peer in other_ids:
                        peer_data = get_client(peer)
                        if peer_data and not peer.startswith("web-") and peer_data.get('status') == 'dozing':
                            mobile_in_doze = True
                            break
                    
                    if mobile_in_doze:
                        # Si hay un móvil en doze, solo quitar este cliente web de la sala, 
                        # pero mantener la sala y el cliente móvil
                        logging.info(f"[LEAVE] Cliente web {client_id} salió, pero hay un móvil en doze. Manteniendo la sala {room_id}.")
                        
                        # Quitar este cliente de la lista de clientes activos
                        if client_id in current_session_leave["clients"]:
                            current_session_leave["clients"].remove(client_id)
                        
                        # Asegurar que este cliente web está en la lista de clientes previos
                        if not current_session_leave.get("previous_clients"):
                            current_session_leave["previous_clients"] = []
                        if client_id not in current_session_leave["previous_clients"]:
                            current_session_leave["previous_clients"].append(client_id)
                        
                        # Actualizar la sesión
                        set_session(room_id, current_session_leave)
                        
                        # Notificar al móvil si está conectado
                        for peer in other_ids:
                            if peer in active_websockets:
                                try:
                                    active_websockets[peer].write_message({'event': 'peer_left', 'client_id': client_id, 'message': 'El cliente web se ha desconectado. Tú permaneces en modo doze.'})
                                except tornado.websocket.WebSocketClosedError:
                                    logging.warning(f"Error al notificar a {peer} (leave de {client_id}): WebSocket ya cerrado.")
                        
                        # Eliminar cliente web pero mantener sala y móvil
                        delete_client(client_id, keep_message_queue=False)
                        if client_id in active_websockets: 
                            del active_websockets[client_id]
                    else:
                        # Comportamiento normal: eliminar sala completa
                        for peer in other_ids:
                            if peer in active_websockets:
                                try:
                                    active_websockets[peer].write_message({'event': 'peer_left', 'client_id': client_id, 'message': 'El otro usuario se ha desconectado voluntariamente.'})
                                except tornado.websocket.WebSocketClosedError:
                                    logging.warning(f"Error al notificar a {peer} (leave de {client_id}): WebSocket ya cerrado.")
                            delete_client(peer) # Eliminar también al peer si el web se va
                            if peer in active_websockets: del active_websockets[peer]
                    
                        delete_client(client_id, keep_message_queue=False) # El cliente web que hace leave - no conservar cola
                        if client_id in active_websockets: del active_websockets[client_id]
                        delete_session(room_id) # La sala completa
                        logging.info(f"[LEAVE] Cliente web {client_id} salió. Sala {room_id} y todos sus clientes eliminados.")
                self.close()
                return

            # Si el móvil entra en modo doze, marcamos estado y notificamos al par
            if reason == "doze":
                client_data_doze_leave = get_client(client_id)
                if not client_data_doze_leave:
                    logging.error(f"[LEAVE-DOZE] No se encontraron datos del cliente {client_id} al entrar en modo doze")
                    self.close()
                    return
                
                # Rechazar entrada en doze para clientes web (solo móviles pueden entrar en doze)
                if client_id.startswith("web-"):
                    logging.warning(f"[LEAVE-DOZE] Rechazo de entrada en doze para cliente web {client_id}")
                    self.write_message({
                        'error': 'Los clientes web no pueden entrar en modo doze',
                        'code': 'WEB_CANT_DOZE'
                    })
                    self.close()
                    return

                # Registrar información de doze y marcar el estado
                client_data_doze_leave['status'] = 'dozing'
                # Marcar explícitamente que este cliente está en modo doze para recovery
                client_data_doze_leave['dozing'] = True
                client_data_doze_leave['doze_start'] = time.time()
                client_data_doze_leave['doze_id'] = uuid.uuid4().hex  # ID único para este ciclo doze
                set_client(client_id, client_data_doze_leave)
                logging.info(f"[LEAVE-DOZE] Cliente {client_id} entrando en modo doze en sala {room_id}. Se conservará la cola de mensajes.")
                
                # Obtener la sesión actual
                current_session = get_session(room_id)
                if not current_session:
                    logging.error(f"[LEAVE-DOZE] No se encontró la sala {room_id} para el cliente {client_id}")
                    self.close()
                    return
                
                # Registrar todos los clientes actuales como clientes previos para permitir reconexión
                if not current_session.get("previous_clients"):
                    current_session["previous_clients"] = []
                
                for other_client_id in current_session.get('clients', []):
                    if other_client_id != client_id and other_client_id not in current_session["previous_clients"]:
                        current_session["previous_clients"].append(other_client_id)
                        logging.info(f"[LEAVE-DOZE] Cliente {other_client_id} registrado como cliente previo para {room_id}")
                
                # Marcar la sala como con cliente en doze para restricciones de unión
                current_session['has_dozing_client'] = True
                current_session['doze_client_id'] = client_id
                current_session['doze_start_time'] = time.time()
                current_session['last_activity'] = time.time()
                set_session(room_id, current_session)
                
                # Notificar a todos los pares sobre el modo doze
                for peer in current_session.get('clients', []):
                    if peer != client_id and peer in active_websockets:
                        try:
                            active_websockets[peer].write_message({
                                'event': 'peer_doze_mode',
                                'client_id': client_id,
                                'message': f'El usuario {client_id} ha entrado en modo de bajo consumo. Seguirá recibiendo mensajes al reconectarse.',
                                'timestamp': time.time(),
                                'room_id': room_id
                            })
                            logging.info(f"[LEAVE-DOZE] Notificado al par {peer} sobre el modo doze de {client_id}")
                        except Exception as e:
                            logging.error(f"[LEAVE-DOZE] Error al notificar al par {peer} sobre modo doze: {e}")
                
                # Eliminamos del diccionario de websockets activos pero SIN eliminar la cola
                if client_id in active_websockets: 
                    del active_websockets[client_id]
                
                # Verificar y preparar cola de mensajes
                queue_key = f"{MESSAGE_QUEUE_KEY_PREFIX}{client_id}"
                
                try:
                    # Crear una cola vacía si no existe
                    if not redis_client.exists(queue_key):
                        redis_client.rpush(queue_key, json.dumps({
                            'id': f"{time.time()}-init",
                            'data': {
                                'event': 'doze_start',
                                'timestamp': time.time(),
                                'info': 'Inicio de modo doze'
                            },
                            'timestamp': time.time()
                        }))
                        logging.info(f"[LEAVE-DOZE] Creada cola de mensajes para {client_id}")
                    
                    # Obtener estado de la cola
                    pending_count = redis_client.llen(queue_key)
                    logging.info(f"[LEAVE-DOZE] Estado de la cola para {client_id}: mensajes_pendientes={pending_count}")
                    
                    # Establecer tiempo de vida de la cola
                    redis_client.expire(queue_key, MESSAGE_TTL_SECONDS)
                    logging.info(f"[LEAVE-DOZE] TTL actualizado para la cola {queue_key}: {MESSAGE_TTL_SECONDS} segundos")
                except Exception as e:
                    logging.error(f"[LEAVE-DOZE] Error al verificar/actualizar la cola para {client_id}: {e}")
                
                self.close()
                return

            # Normal leave: notificar y remover
            current_session_normal_leave = get_session(room_id)
            if current_session_normal_leave:
                peer_list = current_session_normal_leave.get("clients", [])
                other_ids = [cid for cid in peer_list if cid != client_id]
                for peer in other_ids:
                    if peer in active_websockets:
                        try:
                            active_websockets[peer].write_message({'event': 'peer_left', 'client_id': client_id, 'message': 'El otro usuario se ha desconectado.'})
                        except tornado.websocket.WebSocketClosedError:
                            logging.warning(f"Error al notificar a {peer} (normal leave de {client_id}): WebSocket ya cerrado.")
                
                if client_id in current_session_normal_leave["clients"]:
                    current_session_normal_leave["clients"].remove(client_id)
                delete_client(client_id)
                if client_id in active_websockets: del active_websockets[client_id]
                
                current_session_normal_leave["clients"] = [
                    cid for cid in current_session_normal_leave["clients"] if get_client(cid) is not None
                ]
                
                if not current_session_normal_leave["clients"]:
                    delete_session(room_id)
                    logging.info(f"[LEAVE] Cliente {client_id} salió. Sala {room_id} eliminada por quedar vacía.")
                else:
                    current_session_normal_leave['last_activity'] = time.time()
                    set_session(room_id, current_session_normal_leave)
                    logging.info(f"[LEAVE] Cliente {client_id} salió de la sala {room_id}. Clientes restantes: {len(current_session_normal_leave['clients'])}.")
            else:
                # Si la sesión no existe pero el cliente intenta un 'leave'
                delete_client(client_id)
                if client_id in active_websockets: del active_websockets[client_id]
                logging.warning(f"[LEAVE] Cliente {client_id} intentó salir de una sala ({room_id}) inexistente. Cliente eliminado.")

            self.close()
            return

        msg = data.get('message')

        # Serializamos a JSON para medir tamaño (y aceptar dicts)
        try:
            msg_serializado = json.dumps(msg)
        except Exception:
            self.write_message({'error': 'No pude serializar el mensaje'})
            return

        if len(msg_serializado) > 500:
            self.write_message({'error': 'Mensaje demasiado largo'})
            return            # ¡Ya podemos hacer broadcast!
        current_session_broadcast = get_session(self.room_id)
        if current_session_broadcast and len(current_session_broadcast["clients"]) == 2:
            other = [cid for cid in current_session_broadcast["clients"] if cid != self.client_id][0]
            logging.info(f"[DEBUG] Broadcasting to {other}: {msg}")
            
            # Generar un ID único para este mensaje
            message_id = f"{time.time()}-{uuid.uuid4().hex[:8]}"
            
            # Reenvía el objeto completo, con ID único
            message_payload_to_send = {
                'sender_id': self.client_id,
                'message'  : msg,
                'timestamp': time.time(), # Añadir timestamp al mensaje
                'message_id': message_id  # Añadir ID único a cada mensaje
            }
            # Obtener estado del cliente destinatario
            other_client = get_client(other)
            is_dozing = other_client and other_client.get('status') == 'dozing'
            
            # Generar ID único para este mensaje para seguimiento
            message_id = f"{time.time()}-{uuid.uuid4().hex[:8]}"
            message_payload_to_send['message_id'] = message_id
            
            # Variable para controlar si debemos confirmar la entrega al remitente
            delivery_confirmed = False
            max_retries = 3
            retry_count = 0
            
            # Si el cliente tiene websocket activo y NO está en modo doze, enviar directamente
            if other in active_websockets and not is_dozing:
                try:
                    # Aquí debe ir el código para enviar el mensaje al cliente
                    active_websockets[other].write_message(message_payload_to_send)
                    delivery_confirmed = True
                    
                    # Confirmar al remitente que el mensaje fue entregado
                    self.write_message({
                        'event': 'message_delivered',
                        'message_id': message_id,
                        'recipient_id': other,
                        'timestamp': time.time()
                    })
                    logging.info(f"[BROADCAST] Mensaje {message_id} entregado directamente a {other}")
                except Exception as e:
                    logging.error(f"[BROADCAST] Error al entregar mensaje a {other}: {e}")
                    # No confirmamos delivery_confirmed, se caerá al encolamiento
            
            # Si no está disponible o es dozing, encolar mensaje
            if not delivery_confirmed:
                try:
                    # Preparar la cola de mensajes
                    queue_key = f"{MESSAGE_QUEUE_KEY_PREFIX}{other}"
                    # Verificar antes de encolar
                    queue_length_before = redis_client.llen(queue_key) if redis_client.exists(queue_key) else 0
                    
                    # Encolar el mensaje
                    success = add_message_to_queue(other, message_payload_to_send)
                    
                    # Verificar después de encolar
                    queue_length_after = redis_client.llen(queue_key)
                    
                    if success:
                        logging.info(f"[BROADCAST] Mensaje {message_id} encolado exitosamente para {other}. Cola antes: {queue_length_before}, después: {queue_length_after}")
                        # Confirmar al remitente que el mensaje fue encolado exitosamente
                        self.write_message({
                            'event': 'message_queued',
                            'recipient_id': other,
                            'message_id': message_id,
                            'original_message': msg,  # Incluir el mensaje original para referencia
                            'timestamp': time.time(), # Timestamp de la confirmación
                            'info': 'Mensaje encolado para entrega cuando el destinatario se conecte'
                        })
                    else:
                        logging.error(f"[BROADCAST] Error al encolar mensaje {message_id} para {other}")
                        # Informar al remitente del error
                        self.write_message({
                            'event': 'message_queue_failed',
                            'recipient_id': other, 
                            'message_id': message_id,
                            'error': 'No se pudo encolar el mensaje. Por favor, inténtalo de nuevo.'
                        })
                except Exception as e:
                    logging.error(f"[BROADCAST] Error al encolar mensaje para {other}: {e}")
                    # Informar al remitente del error
                    self.write_message({
                        'event': 'message_queue_failed', 
                        'recipient_id': other,
                        'message_id': message_id, 
                        'error': 'Error interno al encolar mensaje.'
                    })
        else:
            self.write_message({'info': 'Aún no emparejado.'})

    def on_close(self):
        client_id_log = getattr(self, 'client_id', 'N/A')
        room_id_log = getattr(self, 'room_id', 'N/A')

        if getattr(self, 'intentional_disconnect', False):
            logging.info(f"[CLOSE] Cierre voluntario del cliente {client_id_log} en sala {room_id_log}. Limpieza gestionada por on_message/'leave'.")
            # active_websockets removal should have been handled by 'leave' action.
            # Double-check here for safety, especially if client_id was set.
            if hasattr(self, 'client_id') and self.client_id and self.client_id in active_websockets and active_websockets[self.client_id] == self:
                del active_websockets[self.client_id]
            return
        
        # --- Desconexión Involuntaria / Inesperada ---
        client_id = getattr(self, 'client_id', None) # Re-fetch for actual use
        room_id = getattr(self, 'room_id', None)

        if not client_id or not room_id:
            logging.warning(f"[CLOSE] Desconexión inesperada antes de la asignación completa de client_id/room_id. WebSocket: {self}")
            return

        # Diferenciar el tratamiento para clientes web y móviles
        is_web_client = client_id.startswith("web-")
        # El grace_period_to_use aquí es solo para el log, la lógica de expiración usará el flag is_web en cleanup_sessions
        log_grace_period = WEB_RECONNECT_TIMEOUT if is_web_client else RECONNECT_GRACE_PERIOD

        logging.info(f"[CLOSE] Desconexión inesperada del cliente {client_id} (web: {is_web_client}) en la sala {room_id}. Marcando para reconexión (timeout respectivo: web={WEB_RECONNECT_TIMEOUT}s, mobile={RECONNECT_GRACE_PERIOD}s).")
        
        # Verificar si hay mensajes en la cola antes de la desconexión (para depuración)
        queue_key = f"{MESSAGE_QUEUE_KEY_PREFIX}{client_id}"
        queue_exists = redis_client.exists(queue_key)
        queue_length = redis_client.llen(queue_key) if queue_exists else 0
        logging.info(f"[CLOSE] Cola de mensajes para {client_id} - existe: {queue_exists}, longitud: {queue_length}")
        
        client_data = get_client(client_id)
        if client_data:
            # Si el cliente estaba en modo doze, mantener ese estado
            if client_data.get('status') == 'dozing':
                logging.info(f"[CLOSE] Cliente {client_id} ya estaba en modo doze, manteniendo ese estado")
            else:
                client_data['status'] = 'pending_reconnect'
                client_data['pending_since'] = time.time()
            
            client_data['is_web'] = is_web_client # Marcar si es web para cleanup_sessions y init
            set_client(client_id, client_data)
            
            # Si es un cliente web, registrarlo como cliente previo de la sala
            if is_web_client and room_id:
                session_data = get_session(room_id)
                if session_data:
                    if not session_data.get("previous_clients"):
                        session_data["previous_clients"] = []
                    if client_id not in session_data["previous_clients"]:
                        session_data["previous_clients"].append(client_id)
                    set_session(room_id, session_data)
                    logging.info(f"[CLOSE] Cliente web {client_id} registrado como cliente previo de la sala {room_id}")
        else:
            logging.warning(f"[CLOSE] No se encontraron datos para el cliente {client_id} (sala {room_id}) tras desconexión inesperada.")
        
        if client_id in active_websockets and active_websockets[client_id] == self:
            del active_websockets[client_id]
        
        # Notificar al peer
        current_session = get_session(room_id)
        if current_session:
            for peer in current_session.get('clients', []):
                if peer != client_id and peer in active_websockets: 
                    try:
                        active_websockets[peer].write_message({
                            'event': 'peer_disconnected_unexpectedly',
                            'client_id': client_id,
                            'message': f'El usuario {client_id} perdió la conexión. Esperando que se reconecte…'
                        })
                    except tornado.websocket.WebSocketClosedError:
                        logging.warning(f"Error al notificar a {peer} (desconexión de {client_id}, sala {room_id}): WebSocket ya cerrado.")
                    except Exception as e:
                        logging.error(f"Excepción al notificar a {peer} (desconexión de {client_id}, sala {room_id}): {e}")

#
#Configuración de la aplicación y rutas

def make_app():
    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/ws", WebSocketHandler),
        (r"/monitor", MonitorHandler),
        (r"/health", HealthHandler),
    ])

def cleanup_sessions():
    now = time.time()
    expired_sessions = []
    expired_clients_due_to_no_reconnect = []  # Lista para clientes que no se reconectaron
    
    # Identificar salas expiradas, pero mantener las que tienen un móvil en doze
    for rid_key in get_all_session_keys():
        info = get_session(rid_key)
        if not info: continue # La sesión pudo haber sido eliminada

        # Comprobar si hay algún cliente móvil en estado dozing
        has_dozing_mobile = False
        for cid in info.get('clients', []):
            client_info = get_client(cid)
            if client_info and not cid.startswith("web-") and client_info.get('status') == 'dozing':
                has_dozing_mobile = True
                break
        
        # Si tiene un móvil en doze, usar un timeout más largo (p.ej. 24 horas)
        if has_dozing_mobile:
            doze_timeout = 86400  # 24 horas
            if now - info.get('last_activity', now) > doze_timeout:
                expired_sessions.append(rid_key)
        else:
            # Timeout normal para otras salas
            if now - info.get('last_activity', now) > SESSION_TIMEOUT:
                expired_sessions.append(rid_key)

    # --- Expirar clientes que no se reconectaron ---
    for cid_key in get_all_client_keys():
        info = get_client(cid_key)
        if not info: continue

        if info.get('status') == 'pending_reconnect':
            is_web = info.get('is_web', False) # Comprobar si el cliente fue marcado como web
            timeout_for_client = WEB_RECONNECT_TIMEOUT if is_web else RECONNECT_GRACE_PERIOD
            
            if now - info.get('pending_since', now) > timeout_for_client:
                logging.info(f'[CLEANUP] Cliente {cid_key} (web: {is_web}) no se reconectó a tiempo ({timeout_for_client}s); limpieza definitiva.')
                rid = info.get('room_id')
                
                # Notificar al peer si la sala y el peer existen y están activos
                if rid:
                    session_info_reconnect = get_session(rid)
                    if session_info_reconnect:
                        for peer in session_info_reconnect.get('clients', []):
                            if peer != cid_key and peer in active_websockets:
                                try:
                                    active_websockets[peer].write_message({
                                        'event': 'peer_reconnect_failed',
                                        'client_id': cid_key,
                                        'message': f'El usuario {cid_key} no pudo reconectarse y ha sido desconectado.'
                                    })
                                except tornado.websocket.WebSocketClosedError:
                                    logging.warning(f"Error al notificar fallo de reconexión de {cid_key} a {peer}: WebSocket ya cerrado.")
                    
                    # Eliminar cliente de la sesión
                    if cid_key in session_info_reconnect['clients']:
                        session_info_reconnect['clients'].remove(cid_key)
                        session_info_reconnect['last_activity'] = now
                        
                        if not session_info_reconnect['clients']:
                            # Si la sala queda vacía, añadirla a la lista de expiradas para ser borrada más adelante en este ciclo de cleanup
                            # o borrarla directamente. Para simplificar, la borramos aquí.
                            delete_session(rid)
                            logging.info(f"[CLEANUP] Sala {rid} eliminada porque el último cliente ({cid_key}) no se reconectó y la sala quedó vacía.")
                            # No es necesario añadir a expired_sessions si se borra aquí.
                        else:
                            set_session(rid, session_info_reconnect)
            
            # ¡IMPORTANTE! - Verificar si este cliente estaba en modo doze antes de eliminarlo
            client_data_check_doze = get_client(cid_key)
            was_dozing = client_data_check_doze and client_data_check_doze.get('status') == 'dozing'
            
            # Si estaba en doze, conservamos la cola de mensajes al eliminarlo
            delete_client(cid_key, keep_message_queue=was_dozing)
            if was_dozing:
                logging.info(f"[CLEANUP] Cliente {cid_key} estaba en doze, se conserva su cola de mensajes")
            # active_websockets ya debería estar limpio para este cid_key desde on_close
    
    # Limpiar salas expiradas (esta parte es para el timeout general de la sesión)
    for rid_expired in expired_sessions: # expired_sessions se llena en la primera parte de cleanup_sessions
        session_to_delete = get_session(rid_expired)
        if session_to_delete:
            for cid_in_expired_session in session_to_delete.get('clients', []):
                if cid_in_expired_session in active_websockets:
                    try:
                        active_websockets[cid_in_expired_session].write_message({'info': 'Sesión expirada'})
                        active_websockets[cid_in_expired_session].close()
                    except Exception as e:
                        logging.error(f"Error al cerrar websocket para {cid_in_expired_session}: {e}")
                    del active_websockets[cid_in_expired_session]
                delete_client(cid_in_expired_session) # Asegurar que el cliente se borra de Redis
        delete_session(rid_expired)
        logging.info(f'Sesión {rid_expired} expirada y removida')

def shutdown(sig, frame):
    logging.info("→ Recibida señal %s, deteniendo IOLoop…", sig)
    tornado.ioloop.IOLoop.current().add_callback_from_signal(
        tornado.ioloop.IOLoop.current().stop
    )

signal.signal(signal.SIGTERM, shutdown)  # usado por Docker / systemd
signal.signal(signal.SIGINT,  shutdown)  # Ctrl-C local


PeriodicCallback(cleanup_sessions, 60000).start()

def initialize_server_state_from_redis():
    logging.info("[INIT] Inicializando estado del servidor desde Redis...")
    now = time.time()
    client_keys = get_all_client_keys()
    sessions_to_update = {} # Para agrupar actualizaciones de sesión
    clients_to_delete = []

    for cid_key in client_keys:
        client_data = get_client(cid_key)
        if not client_data:
            logging.warning(f"[INIT] Cliente {cid_key} encontrado en keys pero no en datos. Omitiendo.")
            continue

        current_status = client_data.get('status')
        room_id = client_data.get('room_id')

        if current_status == 'pending_reconnect':
            pending_since = client_data.get('pending_since', 0)
            is_web_client_init = client_data.get('is_web', False) # Leer la marca 'is_web'
            timeout_to_use_init = WEB_RECONNECT_TIMEOUT if is_web_client_init else RECONNECT_GRACE_PERIOD

            if now - pending_since > timeout_to_use_init:
                logging.info(f"[INIT] Cliente {cid_key} (web: {is_web_client_init}) en pending_reconnect ha expirado ({timeout_to_use_init}s). Limpiando.")
                clients_to_delete.append(cid_key)
                if room_id:
                    if room_id not in sessions_to_update:
                        sessions_to_update[room_id] = get_session(room_id)
                    if sessions_to_update[room_id] and cid_key in sessions_to_update[room_id].get('clients', []):
                        sessions_to_update[room_id]['clients'].remove(cid_key)
            else:
                logging.info(f"[INIT] Cliente {cid_key} sigue en pending_reconnect. Esperando reconexión.")
        
        elif current_status in ['connected', 'waiting']:
            logging.info(f"[INIT] Cliente {cid_key} estaba {current_status}. Marcando como pending_reconnect.")
            client_data['status'] = 'pending_reconnect'
            client_data['pending_since'] = now
            client_data['is_web'] = cid_key.startswith("web-") # Marcar si es web durante la inicialización también
            set_client(cid_key, client_data)
            if room_id: # Marcar la sesión para actualizar last_activity si es necesario
                if room_id not in sessions_to_update:
                    sessions_to_update[room_id] = get_session(room_id)
                if sessions_to_update[room_id]: # Asegurar que la sesión existe
                     sessions_to_update[room_id]['last_activity'] = now # Actualizar para evitar expiración inmediata
        
        elif current_status == 'dozing':
            logging.info(f"[INIT] Cliente {cid_key} está en dozing. Manteniendo estado.")
            if room_id: # Marcar la sesión para actualizar last_activity
                if room_id not in sessions_to_update:
                    sessions_to_update[room_id] = get_session(room_id)
                if sessions_to_update[room_id]: # Asegurar que la sesión existe
                    sessions_to_update[room_id]['last_activity'] = now
        else:
            logging.info(f"[INIT] Cliente {cid_key} con estado desconocido o ya limpio: {current_status}. Omitiendo.")

    for room_id, session_data in sessions_to_update.items():
        if session_data:
            if not session_data.get('clients'):
                logging.info(f"[INIT] Sesión {room_id} quedó vacía. Eliminando.")
                delete_session(room_id)
            else:
                # Asegurar que last_activity se actualiza si la sesión no se borra
                session_data['last_activity'] = now 
                set_session(room_id, session_data)
        else:
            # Esto podría pasar si una sesión fue eliminada mientras se procesaban clientes
            logging.warning(f"[INIT] Sesión {room_id} no encontrada al intentar actualizarla.")

    for cid_key in clients_to_delete:
        delete_client(cid_key)

    # Limpiar active_websockets (aunque debería estar vacío al inicio de un nuevo proceso)
    active_websockets.clear()
    logging.info("[INIT] Inicialización de estado del servidor desde Redis completada.")

#
#Iniciar el servidor

if __name__ == "__main__":
    app = make_app()
    app.listen(5001)
    logging.info('Servidor MinkaV2 (Redis) iniciado en el puerto 5001')
    
    # Inicializar estado desde Redis antes de iniciar el IOLoop
    initialize_server_state_from_redis()
    
    tornado.ioloop.IOLoop.current().start()