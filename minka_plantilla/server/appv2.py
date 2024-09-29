import tornado.ioloop
import tornado.web
import tornado.websocket
from cryptography.fernet import Fernet
import json
import uuid
import time
import logging
logging.basicConfig(level=logging.INFO)

# Generación de claves y cifrado
key = Fernet.generate_key()
cipher = Fernet(key)

# Diccionario para almacenar información de los clientes
clients = {}

# Configuraciones de tiempo
WAIT_TIME = 90  # Tiempo de espera inicial en segundos (1.5 minutos)
TOKEN_EXPIRATION_TIME = 24 * 3600  # Expiración del token en 24 horas después de la conexión

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.write('Hola Minka, by ellanotequiere')

class TokenHandler(tornado.web.RequestHandler):
    def post(self):
        data = json.loads(self.request.body)
        client_id = data.get('client_id')

        if not client_id:
            self.set_status(400)
            self.write({'error': 'client_id is required'})
            return

        session_token = str(uuid.uuid4())
        timestamp = time.time()

        # Estructura de datos del token
        token_data = {
            'client_id': client_id,
            'session_token': session_token,
            'timestamp': timestamp
        }

        # Cifrar el token_data
        encrypted_token = cipher.encrypt(json.dumps(token_data).encode('utf-8'))

        # Almacenar el cliente y su token de sesión
        clients[client_id] = {
            'session_token': session_token,
            'ws': None,  # Se establecerá cuando el cliente se conecte por WebSocket
            'timestamp': timestamp,
            'status': 'waiting',
            'expiration': timestamp + WAIT_TIME
        }

        # Programar la expiración del token
        schedule_token_expiration(client_id)

        self.write({'encrypted_token': encrypted_token.decode('utf-8')})

def schedule_token_expiration(client_id):
    def expire_token():
        if client_id in clients and clients[client_id]['status'] == 'waiting':
            del clients[client_id]
            print(f'Token for {client_id} expired due to inactivity.')

    tornado.ioloop.IOLoop.current().call_later(WAIT_TIME, expire_token)

class CheckTokenHandler(tornado.web.RequestHandler):
    def post(self):
        data = json.loads(self.request.body)
        encrypted_token = data.get('token')

        if not encrypted_token:
            self.set_status(400)
            self.write({'error': 'Token is required'})
            return

        try:
            token_data = json.loads(cipher.decrypt(encrypted_token.encode('utf-8')).decode('utf-8'))
        except Exception as e:
            print(f"Token decryption error: {e}")
            self.set_status(400)
            self.write({'error': 'Invalid token'})
            return

        client_id = token_data.get('client_id')
        session_token = token_data.get('session_token')

        if client_id in clients and clients[client_id]['session_token'] == session_token:
            current_time = time.time()
            if current_time > clients[client_id]['expiration']:
                self.write({'valid': False, 'message': 'Token expired'})
            else:
                self.write({'valid': True, 'message': 'Token is valid'})
        else:
            self.set_status(400)
            self.write({'valid': False, 'message': 'Token or client_id is invalid'})

class SessionTokenHandler(BaseHandler):
    def post(self):
        data = json.loads(self.request.body)
        token = data.get('token')

        # Validar el token proporcionado (el token del código QR)
        if not token or token not in sessions:
            self.set_status(400)
            self.write({'error': 'Invalid token'})
            return

        # Generar el token de sesión persistente
        session_token = str(uuid.uuid4())
        sessions[token]['session_token'] = session_token
        sessions[token]['session_token_expiration'] = time.time() + SESSION_EXPIRATION_TIME

        # Enviar el token de sesión al frontend
        self.write({'session_token': session_token})


class WebSocketHandler(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True  # Implementa lógica de seguridad en producción
    
    def set_default_headers(self):
        # Establecer cabeceras CORS
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "origin, x-requested-with, content-type, accept, authorization")
        self.set_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()


    def open(self):
        encrypted_token = self.get_argument('token', None)

        if not encrypted_token:
            self.write_message({'error': 'Token is required'})
            self.close()
            return

        try:
            token_data = json.loads(cipher.decrypt(encrypted_token.encode('utf-8')).decode('utf-8'))
        except Exception as e:
            self.write_message({'error': 'Invalid token'})
            self.close()
            return

        client_id = token_data.get('client_id')
        session_token = token_data.get('session_token')

        if time.time() > clients.get(client_id, {}).get('expiration', 0):
            self.write_message({'error': 'Token expired, please generate a new one'})
            self.close()
            return

        if client_id in clients and clients[client_id]['session_token'] == session_token:
            clients[client_id]['ws'] = self
            clients[client_id]['status'] = 'connected'
            clients[client_id]['expiration'] = time.time() + TOKEN_EXPIRATION_TIME
            print(f'Client {client_id} connected successfully')
            self.client_id = client_id
        else:
            self.write_message({'error': 'Invalid session token or client is not registered'})
            self.close()

    def on_message(self, message):
        data = json.loads(message)
        sender_id = data.get('sender_id')
        recipient_id = data.get('recipient_id')
        message_text = data.get('message')

        if recipient_id in clients and clients[recipient_id]['ws']:
            recipient_ws = clients[recipient_id]['ws']
            recipient_ws.write_message({'sender_id': sender_id, 'message': message_text})
            print(f'Message from {sender_id} to {recipient_id}: {message_text}')
        else:
            self.write_message({'error': f'Recipient {recipient_id} not found or not connected'})

    def on_close(self):
        client_id = getattr(self, 'client_id', None)
        if client_id and client_id in clients:
            clients[client_id]['ws'] = None
            print(f'Client {client_id} disconnected')

class CheckLinkedHandler(tornado.web.RequestHandler):
    def post(self):
        data = json.loads(self.request.body)
        client_id = data.get('client_id')

        # Verificar si el cliente está en el diccionario de clientes y su estado es 'connected'
        if client_id in clients and clients[client_id]['status'] == 'connected':
            self.write({'is_linked': True})
        else:
            self.write({'is_linked': False})
class BaseHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        # Permitir solicitudes desde cualquier origen (ajusta esto para producción)
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "origin, x-requested-with, content-type, accept, authorization")
        self.set_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    
    def options(self, *args, **kwargs):
        # Manejar solicitudes OPTIONS
        self.set_status(204)
        self.finish()
class ReconnectHandler(BaseHandler):
    def post(self):
        data = json.loads(self.request.body)
        session_token = data.get('session_token')

        # Buscar la sesión asociada al token
        for session in sessions.values():
            if session.get('session_token') == session_token:
                # Verificar si el token ha expirado
                if time.time() > session.get('session_token_expiration', 0):
                    self.set_status(401)
                    self.write({'error': 'Session token expired'})
                    return

                # Reconectar la sesión
                self.write({'status': 'reconnected', 'messages': session.get('messages', [])})
                return

        self.set_status(401)
        self.write({'error': 'Invalid session token'})



def make_app():
    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/token", TokenHandler),
        (r"/check_token", CheckTokenHandler),
        (r"/check_linkend",CheckLinkedHandler),
        (r"/ws", WebSocketHandler),
        
    ])

if __name__ == "__main__":
    app = make_app()
    app.listen(5000)
    print('Server started on port 5000')
    tornado.ioloop.IOLoop.current().start()
