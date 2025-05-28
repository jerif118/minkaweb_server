import tornado.ioloop
import tornado.web
import tornado.websocket
import tornado.httpserver
import re
from tornado.escape import xhtml_escape
from tornado.ioloop import PeriodicCallback
import json
import uuid
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Tiempo máximo de inactividad de una sala en segundos (ej. 5 minutos)
SESSION_TIMEOUT = 7_200

# Estructura de sessions: cada room_id → dict con keys: password, clients, last_activity
sessions = {}

# Diccionario para almacenar la información de los clientes conectados
clients = {}

#
# Handlers Base para manejo de CORS y solicitudes OPTIONS
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
        for room_id, info in sessions.items():
            clients_list = ", ".join([xhtml_escape(cid) for cid in info["clients"]])
            html += f"<tr><td>{xhtml_escape(room_id)}</td><td>{xhtml_escape(info['password'])}</td><td>{clients_list}</td></tr>"
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
        for client_id, cl_info in clients.items():
            room = xhtml_escape(cl_info.get('room_id', ''))
            status = xhtml_escape(cl_info.get('status', ''))
            html += f"<tr><td>{xhtml_escape(client_id)}</td><td>{room}</td><td>{status}</td></tr>"
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
    
    def open(self):
        client_id   = self.get_argument('client_id', None)
        action      = self.get_argument('action', None)
        room_id     = self.get_argument('room_id', None)
        room_passwd = self.get_argument('password', None)
        logging.info(f"[OPEN] Connection from origin: {self.request.headers.get('Origin')}")
        logging.info(f"[OPEN] client_id={client_id} action={action} room_id={room_id} password={room_passwd}")
        # Validar formato de client_id
        if client_id and not re.match(r'^[A-Za-z0-9_\-]{1,64}$', client_id):
            logging.warning(f"[OPEN] Invalid client_id format: {client_id}")
            self.write_message({'error': 'client_id inválido'})
            self.close()
            return

        action = self.get_argument('action', None)

        if action == "create":
            if not client_id:
                logging.warning("[OPEN] client_id is required for 'create' action.")
                self.write_message({'error': 'client_id es requerido'})
                self.close()
                return
            # dentro de WebSocketHandler.open(), rama action=="create"
            if client_id in clients:
                # Ya existe: devolver misma sala y no crear otra
                existing_room = clients[client_id]['room_id']
                password      = sessions[existing_room]['password']
                self.client_id = client_id
                self.room_id   = existing_room
                self.write_message({
                    "room_created": True,
                    "room_id": existing_room,
                    "password": password,
                    "info": "Ya tenías una sala creada. Esperando otro usuario."
                })
                return
            
            room_id = str(uuid.uuid4())
            room_password = str(uuid.uuid4())[:6]
            sessions[room_id] = {
                "password": room_password,
                "clients": [client_id],
                "last_activity": time.time()
            }
            clients[client_id] = {
                'ws': self,
                'status': 'waiting',
                'room_id': room_id
            }
            self.client_id = client_id
            self.room_id = room_id
            self.write_message({
                "room_created": True,
                "room_id": room_id,
                "password": room_password,
                "info": "Room created. Waiting for another user to join."
            })
            logging.info(f"[OPEN] Client {client_id} created room {room_id}")
        
        elif action == "join":
            room_id = self.get_argument('room_id', None)
            room_password = self.get_argument('password', None)

            if not client_id or not room_id or not room_password:
                logging.warning(f"[OPEN] Missing parameters for 'join' action: client_id={client_id}, room_id={room_id}, password={room_password}")
                self.write_message({'error': 'client_id, room_id y password son requeridos'})
                self.close()
                return
            
            if room_id and not re.match(r'^[0-9a-fA-F\-]{36}$', room_id):
                logging.warning(f"[OPEN] Invalid room_id format: {room_id}")
                self.write_message({'error': 'room_id inválido'})
                self.close()
                return
            if room_password and not re.match(r'^[A-Za-z0-9]{6}$', room_password):
                logging.warning(f"[OPEN] Invalid password format: {room_password}")
                self.write_message({'error': 'password inválido'})
                self.close()
                return

            if room_id in sessions and sessions[room_id]["password"] == room_password:
                if len(sessions[room_id]["clients"]) < 2:
                    sessions[room_id]["clients"].append(client_id)
                    clients[client_id] = {
                        'ws': self,
                        'status': 'connected',
                        'room_id': room_id
                    }
                    self.client_id = client_id
                    self.room_id = room_id
                    self.write_message({'info': 'Te has unido a la sala.'})
                    logging.info(f"[OPEN] Client {client_id} joined room {room_id}")
                    other_client_id = sessions[room_id]["clients"][0]
                    # Actualizar status del cliente original a 'connected'
                    clients[other_client_id]['status'] = 'connected'
                    if other_client_id != client_id:
                        clients[other_client_id]['ws'].write_message({"event": "joined","client": client_id})
                else:
                    logging.warning(f"[OPEN] Client {client_id} tried to join full room {room_id}")
                    self.write_message({'error': 'Sala llena. Solo se permiten dos usuarios.'})
                    self.close()
                    return
            else:
                logging.warning(f"[OPEN] Client {client_id} failed to join room {room_id}: Non-existent room or wrong password")
                self.write_message({'error': 'Sala no existe o contraseña incorrecta'})
                self.close()
        
        else:
            self.write_message({'error': 'Acción no válida'})
            self.close()
    
    def on_message(self, message):
        try:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logging.warning(f"[MSG_RECV] Invalid JSON from {getattr(self, 'client_id', 'Unknown')}: {message}")
                self.write_message({'error': 'JSON inválido'})
                return

            # ----- Handle client leave action -----
            if data.get('action') == 'leave':
                client_id = getattr(self, 'client_id', None)
                room_id   = getattr(self, 'room_id', None)
                logging.info(f"[LEAVE] Client {client_id} requested to leave room {room_id}")
                # Remove from global clients
                if client_id in clients:
                    del clients[
                        client_id]
                # Remove from session
                if room_id in sessions and client_id in sessions[room_id]['clients']:
                    sessions[room_id]['clients'].remove(client_id)
                    # Notify the other client if present
                    if sessions[room_id]['clients']:
                        other = sessions[room_id]['clients'][0]
                        if other in clients and clients[other]['ws']:
                            clients[other]['ws'].write_message({'info': 'El otro usuario se ha desconectado.'})
                    # Clean up session if empty
                    if not sessions[room_id]['clients']:
                        del sessions[room_id]
                        logging.info(f"[LEAVE] Room {room_id} deleted after client {client_id} left")
                # Acknowledge and close this WebSocket
                self.write_message({'info': 'Has salido de la sala.'})
                self.close(code=1000, reason='Client leave')
                return

            if not isinstance(data, dict):
                logging.warning(f"[MSG_RECV] Message is not a JSON object from {getattr(self, 'client_id', 'Unknown')}: {data}")
                self.write_message({'error': 'El mensaje debe ser un objeto JSON.'})
                return

            msg = data.get('message')
            if msg is None:
                logging.warning(f"[MSG_RECV] JSON object missing 'message' key from {getattr(self, 'client_id', 'Unknown')}: {data}")
                self.write_message({'error': 'El objeto JSON debe contener una clave "message".'})
                return

            # Serializamos a JSON para medir tamaño (y aceptar dicts)
            try:
                msg_serializado = json.dumps(msg)
            except Exception:
                logging.error(f"[MSG_RECV] Could not serialize message from {self.client_id}: {msg}", exc_info=True)
                self.write_message({'error': 'No pude serializar el mensaje'})
                return

            if len(msg_serializado) > 500: # Assuming this is a reasonable limit, not 1MB as per max_message_size
                logging.warning(f"[MSG_RECV] Message too long from {self.client_id}: {len(msg_serializado)} bytes")
                self.write_message({'error': 'Mensaje demasiado largo'})
                return

            # ¡Ya podemos hacer broadcast!
            if hasattr(self, 'room_id') and self.room_id in sessions and len(sessions[self.room_id]["clients"]) == 2:
                other = [cid for cid in sessions[self.room_id]["clients"] if cid != self.client_id][0]
                # Reenvía el objeto completo, no su str()
                clients[other]['ws'].write_message({
                    'sender_id': self.client_id,
                    'message'  : msg
                })
                logging.info(f'[MSG_SENT] Client: {self.client_id} to Room: {self.room_id} (Other: {other}): {msg_serializado}')
            elif hasattr(self, 'room_id') and self.room_id in sessions: # Paired but other client disconnected or not yet fully paired
                logging.info(f'[MSG_WAIT] Client: {self.client_id} in Room: {self.room_id} sent message but not paired: {msg_serializado}')
                self.write_message({'info': 'Aún no emparejado o el otro usuario se desconecto.'})
            else: # Should not happen if client is properly associated with a room
                logging.warning(f"[MSG_ERR] Client {getattr(self, 'client_id', 'Unknown')} in unknown room {getattr(self, 'room_id', 'None')} sent message: {msg_serializado}")
                self.write_message({'error': 'No estás en una sala válida.'})

        except Exception as e:
            client_id_for_log = getattr(self, 'client_id', 'Unknown')
            logging.error(f"Error processing message from {client_id_for_log}: {e}", exc_info=True)
            self.write_message({'error': 'Ocurrió un error procesando su mensaje.'})


    def on_close(self):
        client_id = getattr(self, 'client_id', None)
        room_id = getattr(self, 'room_id', None)
        if client_id:
            if client_id in clients:
                del clients[client_id]
            logging.info(f'Cliente {client_id} (Room: {room_id}) desconectado.')
            # Notificar al otro usuario y limpiar la sesión
            if room_id in sessions:
                if client_id in sessions[room_id]["clients"]:
                    sessions[room_id]["clients"].remove(client_id)
                    if len(sessions[room_id]["clients"]) == 1:
                        other_client_id = sessions[room_id]["clients"][0]
                        if other_client_id in clients and clients[other_client_id]['ws']:
                            clients[other_client_id]['ws'].write_message({'info': 'El otro usuario salió de la sala.'})
                # Eliminar la sala si queda vacía
                if not sessions[room_id]["clients"]:
                    del sessions[room_id]
                    logging.info(f'Room {room_id} eliminada por estar vacía.')

#
# Configuración de la aplicación y rutas
#
def make_app():
    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/ws", WebSocketHandler),
        (r"/monitor", MonitorHandler),
        (r"/health", HealthHandler),
    ])

def cleanup_sessions():
    now = time.time()
    expired = [rid for rid, info in sessions.items()
               if now - info.get('last_activity', now) > SESSION_TIMEOUT]
    for rid in expired:
        for cid in list(sessions[rid]['clients']): # Iterate over a copy for safe removal
            if cid in clients:
                try:
                    logging.info(f'Cliente {cid} en sala {rid} desconectado por inactividad.')
                    clients[cid]['ws'].write_message({'info': 'Sesión expirada por inactividad.'})
                    clients[cid]['ws'].close(code=1000, reason="Session Timeout")
                except Exception as e:
                    logging.error(f"Error closing WebSocket for client {cid} during cleanup: {e}")
                # Removal from clients dict will happen in on_close
        # If after attempting to disconnect clients, the room is empty, delete it
        # Note: on_close should handle removing clients from sessions[rid]['clients']
        # This check is more of a safeguard if on_close failed or was not called for some reason
        if not sessions[rid]['clients']:
             del sessions[rid]
             logging.info(f'Sesión {rid} expirada y removida')
        elif sessions[rid]['clients'] and all(c not in clients for c in sessions[rid]['clients']):
            # If clients are no longer in the global clients dict but still listed in session (stale)
            del sessions[rid]
            logging.info(f'Sesión {rid} (stale) expirada y removida')


import signal, logging
import tornado.ioloop

def shutdown(sig, frame):
    logging.info("→ Recibida señal %s, iniciando apagado del servidor...", sig)
    io_loop = tornado.ioloop.IOLoop.current()

    # Notificar y cerrar conexiones de clientes
    for client_id, client_info in list(clients.items()): # Iterate over a copy
        try:
            if client_info['ws'] and client_info['ws'].ws_connection:
                client_info['ws'].write_message({'info': 'Servidor reiniciando. Por favor, reconecte.'})
                client_info['ws'].close(code=1001, reason="Server Restarting")
                logging.info(f"Notificado y cerrado conexión para cliente {client_id}")
        except Exception as e:
            logging.error(f"Error notificando/cerrando cliente {client_id}: {e}")

    # Dar un pequeño tiempo para que los mensajes de cierre se envíen
    io_loop.add_timeout(time.time() + 1, lambda: io_loop.stop())
    logging.info("IOLoop se detendrá en breve.")


signal.signal(signal.SIGTERM, shutdown)  # usado por Docker / systemd
signal.signal(signal.SIGINT,  shutdown)  # Ctrl-C local

# Programar limpieza de sesiones cada minuto
PeriodicCallback(cleanup_sessions, 60000).start()

#
# Iniciar el servidor
#
if __name__ == "__main__":
    app = make_app()
    app.listen(5001)
    logging.info('Servidor iniciado en el puerto 5001')
    tornado.ioloop.IOLoop.current().start()