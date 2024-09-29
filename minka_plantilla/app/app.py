import socketio
from flask import Flask, render_template, request, jsonify
from io import BytesIO
import qrcode
import base64
import uuid
import requests

app = Flask(__name__)
sio = socketio.Client()

global_params = {}
link = 'http://127.0.0.1:5000'
MAX_ATTEMPTS = 3

@app.route('/')
def index():
    # Generar el primer client_id y token al cargar la página
    return generate_token_and_connect()

def generate_token_and_connect(attempt_count=0):
    client_id = f'web-client-{uuid.uuid4()}'
    print(f"Generated client_id: {client_id}")

    try:
        response = requests.post(f'{link}/token', json={'client_id': client_id})
        
        if response.status_code == 200 and 'encrypted_token' in response.json():
            encrypted_token = response.json().get('encrypted_token')
            global_params[client_id] = encrypted_token
            print(f'Received encrypted token: {encrypted_token}')

            while attempt_count < MAX_ATTEMPTS:
                try:
                    if sio.connected:
                        sio.disconnect()
                    sio.connect(link, headers={'Authorization': encrypted_token})
                    print("Web client connected with new token")
                    break  # Conexión exitosa, salir del bucle
                except socketio.exceptions.ConnectionError as e:
                    print(f"Connection attempt {attempt_count + 1} failed: {e}")
                    attempt_count += 1
                except Exception as e:
                    print(f"Unexpected error during connection: {e}")
                    attempt_count += 1

            if attempt_count >= MAX_ATTEMPTS:
                print("Max attempts reached, stopping further connections.")
                return generate_qr_and_render(client_id, encrypted_token, 0)
            else:
                return generate_qr_and_render(client_id, encrypted_token, MAX_ATTEMPTS - attempt_count)
        else:
            print(f"Failed to get encrypted token, status code: {response.status_code}")
            return jsonify({'error': 'Failed to get encrypted token from server'}), 500

    except requests.exceptions.RequestException as e:
        print(f"HTTP request failed: {e}")
        return jsonify({'error': 'Failed to communicate with the token server'}), 500

def generate_qr_and_render(client_id, token, attempts_left):
    # Generar el código QR con el token cifrado
    qr = qrcode.QRCode()
    qr.add_data(token)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')

    buffered = BytesIO()
    img.save(buffered)
    img_str = base64.b64encode(buffered.getvalue()).decode()

    # Renderizar la página con la información correspondiente
    return render_template('index.html', qr_code=img_str, client_id=client_id, token=token, attempts_left=attempts_left)

@app.route('/retry', methods=['POST'])
def retry():
    client_id = f'web-client-{uuid.uuid4()}'
    print(f"Generated new client_id: {client_id}")

    try:
        response = requests.post(f'{link}/token', json={'client_id': client_id})
        
        if response.status_code == 200 and 'encrypted_token' in response.json():
            encrypted_token = response.json().get('encrypted_token')
            global_params[client_id] = encrypted_token
            print(f'Received new encrypted token: {encrypted_token}')

            # Generar el nuevo código QR con el token cifrado
            qr = qrcode.QRCode()
            qr.add_data(encrypted_token)
            qr.make(fit=True)
            img = qr.make_image(fill='black', back_color='white')

            buffered = BytesIO()
            img.save(buffered)
            img_str = base64.b64encode(buffered.getvalue()).decode()

            return jsonify({'qr_code': img_str, 'client_id': client_id}), 200
        else:
            print(f"Failed to get new token, status code: {response.status_code}")
            return jsonify({'error': 'Failed to get new token from server'}), 500

    except requests.exceptions.RequestException as e:
        print(f"HTTP request failed: {e}")
        return jsonify({'error': 'Failed to communicate with the token server'}), 500

@sio.event
def connect():
    print("Client connected to the WebSocket server")
    
@sio.on('mobile_connected')
def handle_mobile_connected(data):
    print(f"Mobile client connected with the same client_id: {data['client_id']}")
    # Aquí puedes iniciar la comunicación o actualizar la UI

@sio.on('message')
def handle_message(data):
    client_id = data.get('client_id')
    message = data.get('message')
    print(f'Received message for {client_id}: {message}')
    sio.emit('message_received', {'client_id': client_id, 'status': 'received'})

@sio.event
def disconnect():
    print("Disconnected from the WebSocket server")

if __name__ == '__main__':
    app.run(port=5500, debug=True)
