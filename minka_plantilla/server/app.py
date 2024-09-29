from flask import Flask, request, render_template_string, jsonify
from flask_socketio import SocketIO, emit
from cryptography.fernet import Fernet
import json
import uuid
import time
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret'
socket = SocketIO(app)

# Generación de claves y cifrado
key = Fernet.generate_key()
cipher = Fernet(key)

# Diccionario para almacenar información de los clientes
clients = {}

# Configuraciones de tiempo
WAIT_TIME = 90  # Tiempo de espera inicial en segundos (1.5 minutos)
TOKEN_EXPIRATION_TIME = 24 * 3600  # Expiración del token en 24 horas después de la conexión

@app.route('/')
def index():
    return render_template_string('Hola Minka, by ellanotequiere')

@app.route('/token', methods=['POST'])
def token():
    data = request.json
    client_id = data.get('client_id')
    
    if not client_id:
        return jsonify({'error': 'client_id is required'}), 400
    
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
        'sid': None,  # Se establecerá cuando el cliente se conecte por WebSocket
        'timestamp': timestamp,
        'status': 'waiting',
        'expiration': timestamp + WAIT_TIME
    }
    
    # Iniciar un hilo para esperar la conexión del cliente
    threading.Thread(target=wait_for_connection, args=(client_id,)).start()
    
    return jsonify({'encrypted_token': encrypted_token.decode('utf-8')})

def wait_for_connection(client_id):
    time.sleep(WAIT_TIME)
    
    # Verificar si el cliente aún está en espera
    if client_id in clients and clients[client_id]['status'] == 'waiting':
        # Eliminar el cliente si no se ha conectado dentro del tiempo de espera
        del clients[client_id]
        print(f'Token for {client_id} expired due to inactivity.')

@app.route('/check_token', methods=['POST'])
def check_token():
    data = request.json
    encrypted_token = data.get('token')
    
    if not encrypted_token:
        return jsonify({'error': 'Token is required'}), 400
    
    try:
        token_data = json.loads(cipher.decrypt(encrypted_token.encode('utf-8')).decode('utf-8'))
    except Exception as e:
        print(f"Token decryption error: {e}")
        return jsonify({'error': 'Invalid token'}), 400
    
    client_id = token_data.get('client_id')
    session_token = token_data.get('session_token')
    
    if client_id in clients and clients[client_id]['session_token'] == session_token:
        current_time = time.time()
        if current_time > clients[client_id]['expiration']:
            return jsonify({'valid': False, 'message': 'Token expired'}), 200
        else:
            return jsonify({'valid': True, 'message': 'Token is valid'}), 200
    else:
        return jsonify({'valid': False, 'message': 'Token or client_id is invalid'}), 400

@socket.on('connect')
def handle_connect(auth):
    if auth is None:
        emit('error', {'message': 'Authorization header is missing'})
        return

    encrypted_token = auth.get('Authorization')
    
    if not encrypted_token:
        emit('error', {'message': 'Token is required'})
        return

    try:
        # Descifrar el token para obtener los datos
        token_data = json.loads(cipher.decrypt(encrypted_token.encode('utf-8')).decode('utf-8'))
    except Exception as e:
        emit('error', {'message': 'Invalid token'})
        return

    client_id = token_data.get('client_id')
    session_token = token_data.get('session_token')

    # Verificar si el token ha expirado
    if time.time() > clients.get(client_id, {}).get('expiration', 0):
        emit('error', {'message': 'Token expired, please generate a new one'})
        return

    # Verificar que el token es el más reciente y válido
    if client_id in clients and clients[client_id]['session_token'] == session_token:
        # Actualizar la expiración a 24 horas desde ahora
        clients[client_id]['sid'] = request.sid
        clients[client_id]['status'] = 'connected'
        clients[client_id]['expiration'] = time.time() + TOKEN_EXPIRATION_TIME
        print(f'Client {client_id} connected successfully with SID: {request.sid} and token extended for 24 hours')
    else:
        emit('error', {'message': 'Invalid session token or client is not registered'})

@socket.on('message')
def handle_message(data):
    sender_id = data.get('sender_id')
    recipient_id = data.get('recipient_id')
    message = data.get('message')

    if recipient_id in clients and clients[recipient_id]['sid']:
        recipient_sid = clients[recipient_id]['sid']
        emit('receive_message', {'sender_id': sender_id, 'message': message}, to=recipient_sid)
        print(f'Message from {sender_id} to {recipient_id}: {message}')
    else:
        emit('error', {'message': f'Recipient {recipient_id} not found or not connected'}, to=request.sid)

@socket.on('disconnect')
def handle_disconnect():
    client_id = None
    for cid, info in clients.items():
        if info['sid'] == request.sid:
            client_id = cid
            break
    if client_id:
        clients[client_id]['sid'] = None
        print(f'Client {client_id} disconnected')

if __name__ == '__main__':
    socket.run(app, debug=True)
