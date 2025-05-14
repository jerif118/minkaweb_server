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
from redis_Minka import redis_client
import jwt
from datetime import datetime, timedelta

# Importaciones para manejo de claves RSA y generación si no existen
import os
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

# Carga tus claves RSA, generando si no existen
private_path = "./path/to/private_key.pem"
public_path  = "./path/to/public_key.pem"

if not os.path.exists(private_path) or not os.path.exists(public_path):
    # Generar par de claves RSA
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    priv_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    )
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    # Guardar las claves
    os.makedirs(os.path.dirname(private_path), exist_ok=True)
    with open(private_path, "wb") as f:
        f.write(priv_pem)
    with open(public_path, "wb") as f:
        f.write(pub_pem)

# Leer las claves
with open(private_path, "rb") as f:
    PRIVATE_KEY = f.read()
with open(public_path, "rb") as f:
    PUBLIC_KEY = f.read()

logging.basicConfig(level=logging.INFO)

# Tiempo máximo de inactividad de una sala en segundos (ej. 5 minutos)
SESSION_TIMEOUT = 7_200

# Estructura de sessions: cada room_id → dict con keys: password, clients, last_activity
sessions = {}

# Diccionario para almacenar la información de los clientes conectados
clients = {}

client_cache = {}

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
        sessions_html = ""
        for key in redis_client.scan_iter("session:*"):
            if key.endswith(":clients"):
                continue
            rid = key.split(":",1)[1]
            info = redis_client.hgetall(key)
            clients = redis_client.lrange(f"session:{rid}:clients", 0, -1)
            sessions_html += f"<tr><td>{rid}</td><td>{info['password']}</td><td>{', '.join(clients)}</td></tr>"
        html += sessions_html
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
        clients_html = ""
        for key in redis_client.scan_iter("client:*"):
            cid = key.split(":",1)[1]
            info = redis_client.hgetall(key)
            clients_html += f"<tr><td>{cid}</td><td>{info.get('room_id','')}</td><td>{info.get('status','')}</td></tr>"

        html += clients_html
        html += """
          </table>
        </body>
        </html>
        """
        self.write(html)

#
# Helper para generar token JWT de cliente
#
def generate_client_token(room_id, client_id, expires_days=30):
    payload = {
        "room_id": room_id,
        "client_id": client_id,
        "exp": datetime.utcnow() + timedelta(days=expires_days)
    }
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")

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
        self.client_id = client_id
        client_cache[client_id] = self
        logging.info(f"[OPEN] client_id={client_id} action={action} room_id={room_id}")
        # Validar formato de client_id
        if client_id and not re.match(r'^[A-Za-z0-9_\-]{1,64}$', client_id):
            self.write_message({'error': 'client_id inválido'})
            self.close()
            return

        action = self.get_argument('action', None)

        if action == "create":
            if not client_id:
                self.write_message({'error': 'client_id es requerido'})
                self.close()
                return
            # Redis-only logic for room creation
            room_id = str(uuid.uuid4())
            # Generar token para este cliente
            client_token = generate_client_token(room_id, client_id)
            # Almacenar token del cliente en Redis
            redis_client.hset(f"session:{room_id}:clients_tokens", mapping={client_id: client_token})
            redis_client.hset(f"client:{client_id}", mapping={
                "status": "waiting",
                "room_id": room_id,
                "token": client_token
            })
            # Crear la lista de clientes y almacenar el cliente
            redis_client.rpush(f"session:{room_id}:clients", client_id)
            # Solo almacenar last_activity para la sala
            redis_client.hset(f"session:{room_id}", "last_activity", time.time())
            # Expiración automática de la sesión en Redis en 30 días
            redis_client.expire(f"session:{room_id}", 30 * 24 * 3600)
            self.client_id = client_id
            self.room_id = room_id
            # Enviar credenciales al cliente
            self.write_message({
                "room_created": True,
                "room_id": room_id,
                "token": client_token,
                "info": "Room created. Waiting for another user to join."
            })

        elif action == "join":
            room_id = self.get_argument('room_id', None)
            jwt_token = self.get_argument('token', None)

            if not client_id or not room_id or not jwt_token:
                self.write_message({'error': 'client_id, room_id y token son requeridos'})
                self.close()
                return
            if room_id and not re.match(r'^[0-9a-fA-F\-]{36}$', room_id):
                self.write_message({'error': 'room_id inválido'})
                self.close()
                return
            # Validar el token recibido (debe ser un token de cliente válido para la sala)
            try:
                decoded = jwt.decode(jwt_token, PUBLIC_KEY, algorithms=["RS256"])
                if decoded.get("room_id") == room_id and decoded.get("client_id") == client_id:
                    valid = True
                else:
                    valid = False
            except jwt.PyJWTError:
                valid = False
            # Validar que la sala existe
            session_exists = redis_client.exists(f"session:{room_id}")
            clients_list = redis_client.lrange(f"session:{room_id}:clients", 0, -1) if session_exists else []
            if valid and session_exists:
                cnt = len(clients_list)
                if cnt < 2:
                    # Añadir el cliente a la sala
                    redis_client.rpush(f"session:{room_id}:clients", client_id)
                    # Generar token para este cliente
                    client_token = generate_client_token(room_id, client_id)
                    # Almacenar token del cliente en Redis
                    redis_client.hset(f"session:{room_id}:clients_tokens", mapping={client_id: client_token})
                    redis_client.hset(f"client:{client_id}", mapping={
                        "status": "connected",
                        "room_id": room_id,
                        "token": client_token
                    })
                    # Notificar al cliente original
                    other_id = clients_list[0] if clients_list else None
                    if other_id:
                        redis_client.hset(f"client:{other_id}", "status", "connected")
                        ws_other = client_cache.get(other_id)
                        if ws_other:
                            ws_other.write_message({"event": "joined", "client": client_id})
                    self.client_id = client_id
                    self.room_id = room_id
                    # Actualizar última actividad
                    redis_client.hset(f"session:{room_id}", "last_activity", time.time())
                    # Enviar credenciales al cliente que se une
                    self.write_message({
                        "info": "Te has unido a la sala.",
                        "room_id": room_id,
                        "token": client_token
                    })
                else:
                    self.write_message({'error': 'Sala llena. Solo se permiten dos usuarios.'})
                    self.close()
                    return
            else:
                self.write_message({'error': 'Sala no existe o token incorrecto'})
                self.close()
        else:
            self.write_message({'error': 'Acción no válida'})
            self.close()
    
    def on_message(self, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            self.write_message({'error': 'JSON inválido'})
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
            return

        # Nuevo método de redis
        clients_list = redis_client.lrange(f"session:{self.room_id}:clients", 0, -1)
        if len(clients_list) == 2:
            other = clients_list[0] if clients_list[1] == self.client_id else clients_list[1]
            # Update last_activity on message send
            redis_client.hset(f"session:{self.room_id}", "last_activity", time.time())
            ws_other = client_cache.get(other)
            if ws_other:
                ws_other.write_message({
                    'sender_id': self.client_id,
                    'message': msg
                })
            logging.info(f'[BROADCAST] de {self.client_id} → {other}: {msg}')
        else:
            self.write_message({'info': 'Aún no emparejado.'})

    def on_close(self):
        client_id = getattr(self, 'client_id', None)
        room_id = getattr(self, 'room_id', None)
        if client_id:
            # Notificar al otro usuario y limpiar la sesión en Redis
            # Eliminamos hash de cliente y del cache
            client_cache.pop(client_id, None)
            redis_client.delete(f"client:{client_id}")
            # También eliminar su token de clients_tokens
            redis_client.hdel(f"session:{room_id}:clients_tokens", client_id)

            # Quitamos el cliente de la lista de la sala
            redis_client.lrem(f"session:{room_id}:clients", 0, client_id)
            remaining = redis_client.llen(f"session:{room_id}:clients")

            # Si queda un solo cliente, notificarle
            if remaining == 1:
                other_id = redis_client.lindex(f"session:{room_id}:clients", 0)
                ws_other = client_cache.get(other_id)
                if ws_other:
                    ws_other.write_message({'info': 'El otro usuario se ha desconectado.'})

            # Si ya no hay clientes, eliminamos la sala completa
            if remaining == 0:
                redis_client.delete(f"session:{room_id}", f"session:{room_id}:clients", f"session:{room_id}:clients_tokens")
                logging.info(f'Sesión {room_id} expirada y removida o cerrada por desconexión.')

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
    #expired = [rid for rid, info in sessions.items()
    #           if now - info.get('last_activity', now) > SESSION_TIMEOUT]
    """for rid in expired:
        for cid in sessions[rid]['clients']:
            if cid in clients:
                try:
                    clients[cid]['ws'].write_message({'info': 'Sesión expirada'})
                    clients[cid]['ws'].close()
                except:
                    pass
                del clients[cid]
        del sessions[rid]
        logging.info(f'Sesión {rid} expirada y removida')"""
    for key in redis_client.scan_iter("session:*"):
        if key.endswith(":clients"):
             continue
        last = float(redis_client.hget(key, "last_activity") or now)
        if now - last > SESSION_TIMEOUT:
            rid = key.split(":",1)[1]
            clients = redis_client.lrange(f"session:{rid}:clients", 0, -1)
            for cid in clients:
                ws = client_cache.get(cid)
                if ws:
                    ws.write_message({'info': 'Sesión expirada'})
                    ws.close()
            redis_client.delete(f"session:{rid}", f"session:{rid}:clients")
            logging.info(f'Sesión {rid} expirada y removida')
        

import signal, logging
import tornado.ioloop

def shutdown(sig, frame):
    logging.info("→ Recibida señal %s, deteniendo IOLoop…", sig)
    tornado.ioloop.IOLoop.current().add_callback_from_signal(
        tornado.ioloop.IOLoop.current().stop
    )

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