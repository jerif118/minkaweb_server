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


client_cache = {}

#
# Handlers Base para manejo de CORS y solicitudes OPTIONS
#
class BaseHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        origin = self.request.headers.get("Origin", "")
        # Allow only the requesting origin
        self.set_header("Access-Control-Allow-Origin", origin)
        self.set_header("Access-Control-Allow-Credentials", "true")
        self.set_header("Access-Control-Allow-Headers", "origin, x-requested-with, content-type, accept, authorization")
        self.set_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    
    def options(self, *args, **kwargs):
        # Preflight response
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
              <th>Pairing Token</th>
              <th>Clientes</th>
            </tr>
        """
        # Iterar sobre las salas y crear una fila por cada una
        sessions_html = ""
        for key in redis_client.scan_iter("session:*"):
            key = key.decode() if isinstance(key, bytes) else key
            if key.endswith(":clients"):
                continue
            rid = key.split(":",1)[1]
            rid = rid.decode() if isinstance(rid, bytes) else rid
            info = redis_client.hgetall(key)
            info = {k.decode() if isinstance(k, bytes) else k:
                    v.decode() if isinstance(v, bytes) else v
                    for k, v in info.items()}
            # Leer pairing_token temporal (puede no existir)
            token = redis_client.get(f"pairing:{rid}")
            token = token.decode() if token else ""
            clients_bytes = redis_client.lrange(f"session:{rid}:clients", 0, -1)
            clients = [(c.decode() if isinstance(c, bytes) else c) for c in clients_bytes]
            sessions_html += f"<tr><td>{rid}</td><td>{token}</td><td>{', '.join(clients)}</td></tr>"
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
            key = key.decode() if isinstance(key, bytes) else key
            cid = key.split(":",1)[1]
            cid = cid.decode() if isinstance(cid, bytes) else cid
            info = redis_client.hgetall(key)
            info = {k.decode() if isinstance(k, bytes) else k:
                    v.decode() if isinstance(v, bytes) else v
                    for k, v in info.items()}
            clients_html += f"<tr><td>{cid}</td><td>{info.get('room_id','')}</td><td>{info.get('status','')}</td></tr>"

        html += clients_html
        html += """
          </table>
        </body>
        </html>
        """
        self.write(html)


# Helper para generar token JWT de pairing (corto)
def generate_pairing_token(room_id, expires_minutes=3):
    payload = {
        "room_id": room_id,
        "exp": datetime.utcnow() + timedelta(minutes=expires_minutes),
        "purpose": "pairing"
    }
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")

# Helper para generar token JWT de cliente (largo)
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
        # Try to read long-lived session cookie
        raw = self.get_secure_cookie("minka_session")
        if raw:
            # Existing session connection (as before)
            try:
                jwt_token = raw.decode() if isinstance(raw, bytes) else raw
                decoded = jwt.decode(jwt_token, PUBLIC_KEY, algorithms=["RS256"])
                room_id = decoded.get("room_id")
                client_id = decoded.get("client_id")
                if not room_id or not client_id:
                    raise jwt.PyJWTError()
            except jwt.PyJWTError:
                self.close(code=4003, reason="Token de sesión inválido")
                return
            self.client_id = client_id
            self.room_id = room_id
            client_cache[client_id] = self
            # Garantizar presencia única en la lista de la sala
            redis_client.lrem(f"session:{room_id}:clients", 0, client_id)
            redis_client.rpush(f"session:{room_id}:clients", client_id)
            # Marcar estado online
            redis_client.hset(f"client:{client_id}", mapping={"room_id": room_id, "status": "online"})
            logging.info(f"[OPEN] (cookie) client_id={client_id} room_id={room_id}")
            # Puede agregar lógica adicional si se requiere para reconexión.
            return

        # If no cookie, attempt pairing with token
        # Read pairing_token and client_id
        pairing_token = self.get_argument('pairing_token', None)
        client_id = self.get_argument('client_id', None)

        # Decode pairing_token to extract room_id
        try:
            payload = jwt.decode(pairing_token, PUBLIC_KEY, algorithms=["RS256"])
            if payload.get("purpose") != "pairing":
                raise jwt.PyJWTError()
            room_id = payload["room_id"]
        except jwt.PyJWTError:
            self.close(code=4004, reason="Pairing token inválido")
            return

        self.client_id = client_id
        self.room_id = room_id
        client_cache[client_id] = self
        logging.info(f"[OPEN] (pairing) client_id={client_id} room_id={room_id}")
        # Validar formato de client_id
        if client_id and not re.match(r'^[A-Za-z0-9_\-]{1,64}$', client_id):
            self.write_message({'error': 'client_id inválido'})
            self.close()
            return
        # Si falta pairing_token o client_id, rechazar
        if not pairing_token or not client_id:
            self.write_message({'error': 'client_id y pairing_token requeridos'})
            self.close()
            return
        # Validar token y control de usos
        data = redis_client.hgetall(f"pairing:{room_id}")
        # Obtener token y normalizar
        raw_token = data.get("token") or data.get(b"token")
        if isinstance(raw_token, bytes):
            token_value = raw_token.decode()
        else:
            token_value = raw_token
        # Obtener usos y normalizar
        raw_uses = data.get("uses") or data.get(b"uses") or "0"
        if isinstance(raw_uses, bytes):
            raw_uses = raw_uses.decode()
        uses = int(raw_uses)
        # Validación de token y contador de usos
        if token_value is None or token_value != pairing_token or uses <= 0:
            self.close(code=4005, reason="Token inválido o expirado")
            return
        # Decrementar contador de usos
        remaining = redis_client.hincrby(f"pairing:{room_id}", "uses", -1)
        # Eliminar el hash de pairing cuando usos agoten
        if remaining <= 0:
            redis_client.delete(f"pairing:{room_id}")
        # Obtener primer cliente
        first_id = redis_client.lindex(f"session:{room_id}:clients", 0)
        if not first_id:
            self.write_message({'error': 'Sala no encontrada o primer cliente ausente'})
            self.close()
            return
        # Límite de 2 clientes y evitar duplicados
        existing = redis_client.lrange(f"session:{room_id}:clients", 0, -1)
        if client_id not in existing and len(existing) >= 2:
            self.close(code=4006, reason="Sala llena")
            return
        redis_client.lrem(f"session:{room_id}:clients", 0, client_id)
        # Añadir nuevo cliente
        redis_client.rpush(f"session:{room_id}:clients", client_id)
        # Marcar estado online
        redis_client.hset(f"client:{client_id}", mapping={"room_id": room_id, "status": "online"})
        # Generar long-lived tokens para ambos
        token1 = generate_client_token(room_id, first_id)
        token2 = generate_client_token(room_id, client_id)
        # Almacenar ambos tokens
        redis_client.hset(f"session:{room_id}:clients_tokens", mapping={first_id: token1, client_id: token2})
        # Notificar primer cliente
        ws_other = client_cache.get(first_id)
        if ws_other:
            ws_other.write_message({"event": "paired", "room_id": room_id, "token": token1})
        # Notificar a este cliente
        self.write_message({"event": "paired", "room_id": room_id, "token": token2})

    def on_message(self, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            self.write_message({'error': 'JSON inválido'})
            return

        msg = data.get('message')
        try:
            msg_serializado = json.dumps(msg)
        except Exception:
            self.write_message({'error': 'No pude serializar el mensaje'})
            return
        if len(msg_serializado) > 500:
            self.write_message({'error': 'Mensaje demasiado largo'})
            return

        # Actualizar actividad
        redis_client.hset(f"session:{self.room_id}", "last_activity", time.time())

        # Reenviar a todos menos al emisor
        clients_list = redis_client.lrange(f"session:{self.room_id}:clients", 0, -1)
        for cid in clients_list:
            if cid == self.client_id:
                continue
            ws_other = client_cache.get(cid)
            if ws_other:
                ws_other.write_message({'sender_id': self.client_id, 'message': msg})

        logging.info(f'[BROADCAST] de {self.client_id} → {len(clients_list)-1} clientes: {msg}')

    def on_close(self):
        client_id = getattr(self, 'client_id', None)
        room_id = getattr(self, 'room_id', None)
        if client_id:
            client_cache.pop(client_id, None)
            redis_client.hset(f"client:{client_id}", "status", "offline")
            redis_client.lrem(f"session:{room_id}:clients", 0, client_id)
            remaining = redis_client.llen(f"session:{room_id}:clients")

            if remaining == 1:
                other_id = redis_client.lindex(f"session:{room_id}:clients", 0)
                ws_other = client_cache.get(other_id)
                if ws_other:
                    ws_other.write_message({'info': 'El otro usuario se ha desconectado.'})

            if remaining == 0:
                redis_client.delete(f"session:{room_id}",
                                    f"session:{room_id}:clients",
                                    f"session:{room_id}:clients_tokens")
                logging.info(f'Sesión {room_id} expirada y removida o cerrada por desconexión.')
# Handler para crear sala y obtener pairing_token

class CreateRoomHandler(BaseHandler):
    def post(self):
        # Crear room_id y primer cliente
        data = json.loads(self.request.body.decode())
        client_id = data.get("client_id")
        if not client_id or not re.match(r'^[A-Za-z0-9_\-]{1,64}$', client_id):
            self.set_status(400)
            self.write({"error": "client_id inválido"})
            return

        # Nuevo: Buscar si el cliente ya tiene una sala asociada
        existing_room = redis_client.hget("client_rooms", client_id)
        if existing_room:
            # Verificar si la sesión existe en Redis
            if redis_client.exists(f"session:{existing_room}"):
                pairing_token = redis_client.hget(f"session:{existing_room}", "pairing_token")
                if pairing_token:
                    # Si pairing_token existe y la sesión es válida, responder con los valores existentes
                    self.write({"room_id": existing_room, "pairing_token": pairing_token})
                    return
            # Si la sesión expiró o pairing_token es falsy, limpiar los datos relacionados
            redis_client.delete(f"session:{existing_room}")
            redis_client.delete(f"session:{existing_room}:clients")
            redis_client.delete(f"session:{existing_room}:clients_tokens")
            redis_client.hdel("client_rooms", client_id)
            # Continuar para crear una nueva sala

        room_id = str(uuid.uuid4())
        # Agregar primer cliente a la lista
        redis_client.rpush(f"session:{room_id}:clients", client_id)
        # Registrar primer cliente para monitorización
        redis_client.hset(f"client:{client_id}", mapping={"room_id": room_id, "status": "waiting"})
        # Generar pairing_token (3 min)
        pairing_token = generate_pairing_token(room_id)

        # Crear el hash de la sesión SIN pairing_token (vive 30 d)
        redis_client.hset(f"session:{room_id}", mapping={
            "last_activity": time.time()
        })

        # Guardar el token y el contador de usos en una clave de hash con TTL y permitir 2 usos
        redis_client.hset(f"pairing:{room_id}", mapping={"token": pairing_token, "uses": 2})
        redis_client.expire(f"pairing:{room_id}", 3 * 60)
        # Expiración automática de la sesión en Redis en 30 días (para toda la sala)
        redis_client.expire(f"session:{room_id}:clients", 30 * 24 * 3600)
        # Registrar el mapeo client_id → room_id
        redis_client.hset("client_rooms", client_id, room_id)
        # Responder con room_id y pairing_token
        self.write({"room_id": room_id, "pairing_token": pairing_token})

# Handler para validar la sesión a través del JWT en la cookie
class ValidateSessionHandler(BaseHandler):
    def get(self):
        raw = self.get_secure_cookie("minka_session")
        if not raw:
            self.set_status(401)
            return self.write({"valid": False})
        token = raw.decode() if isinstance(raw, bytes) else raw
        try:
            payload = jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"])
            room_key = f"session:{payload['room_id']}"
            exists = bool(redis_client.exists(room_key))
            return self.write({
                "valid": exists,
                "client_id": payload["client_id"],
                "room_id": payload["room_id"]
            })
        except jwt.PyJWTError:
            self.set_status(401)
            return self.write({"valid": False})

# Nuevo handler para confirmar pairing y setear cookie larga
class ConfirmPairingHandler(BaseHandler):
    def post(self):
        data = json.loads(self.request.body.decode())
        token = data.get("token")
        if not token:
            self.set_status(400)
            return self.write({"error": "token requerido"})
        try:
            payload = jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"])
            client_id = payload.get("client_id")
            if not client_id:
                raise jwt.PyJWTError()
        except jwt.PyJWTError:
            self.set_status(400)
            return self.write({"error": "token inválido"})
        # Set the long-lived session cookie
        self.set_secure_cookie(
            "minka_session",
            token,
            expires_days=30,
            httponly=True,
            secure=True,
            samesite="Strict"
        )
        self.write({"ok": True})

#
# Configuración de la aplicación y rutas
#
def make_app():
    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/ws", WebSocketHandler),
        (r"/monitor", MonitorHandler),
        (r"/health", HealthHandler),
        (r"/create_room", CreateRoomHandler),
        (r"/api/validate-session", ValidateSessionHandler),
        (r"/confirm_pairing", ConfirmPairingHandler),
    ], cookie_secret="YOUR_RANDOM_SECRET")

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