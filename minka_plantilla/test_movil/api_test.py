import requests
import json

# --- CONFIGURACIÓN (MODIFICA ESTO SEGÚN TU ENTORNO) ---
# Si tu servidor Tornado está en tu misma máquina:
SERVER_HOST = "127.0.0.1" # o "localhost"
SERVER_PORT = 5001

# CAMBIA A 'https' SI CONFIGURASTE SSL EN TORNADO, DE LO CONTRARIO USA 'http'
SERVER_SCHEME = "http"
# ---------------------------------------------------------

ENDPOINT_URL = f"{SERVER_SCHEME}://{SERVER_HOST}:{SERVER_PORT}/api/rooms/message"

# Datos de ejemplo para el payload.
# Para que el mensaje se procese completamente y se intente reenviar,
# 'client_id' y 'room_id' deberían existir en una sesión activa en tu servidor.
# Si no existen, tu API debería devolver un error 403 o similar, lo cual también es una prueba útil.
payload = {
    "client_id": "mobile-333fd0e2-5451-46b8-a666-7435eb7b109b",  # Un ID de cliente de prueba
    "room_id": "cf2415b1-2843-416b-aee3-f8cb59cf5ff7",    # Reemplaza con un ID de sala real de tu servidor si quieres probar el reenvío
    "message": {                             # Esta es la estructura que envías desde Android (NotificationData)
        "appName": "TestAppFromPython",
        "senderName": "Python Script",
        "text": "Hola desde el cliente de prueba Python!",
        "amount": 10.50,
        "date": 1678886400000, # Ejemplo de timestamp
        "id": "test-notif-id-python-001",
        "packageName": "com.example.pythontest",
        "info": "Información adicional opcional" # Si tu NotificationData tiene este campo
    }
}

headers = {
    "Content-Type": "application/json"
}

def probar_envio_mensaje():
    print(f"Enviando petición POST a: {ENDPOINT_URL}")
    print(f"Payload que se enviará:\n{json.dumps(payload, indent=2)}")

    try:
        response = requests.post(ENDPOINT_URL, data=json.dumps(payload), headers=headers, timeout=10) # timeout de 10 segundos

        print("\n--- Respuesta del Servidor ---")
        print(f"Código de Estado: {response.status_code}")

        try:
            # Intentar parsear la respuesta como JSON
            response_json = response.json()
            print(f"Cuerpo de la Respuesta (JSON):\n{json.dumps(response_json, indent=2)}")
        except json.JSONDecodeError:
            # Si no es JSON, mostrar como texto
            print(f"Cuerpo de la Respuesta (Texto):\n{response.text}")

    except requests.exceptions.ConnectionError as e:
        print("\n--- ERROR DE CONEXIÓN ---")
        print(f"No se pudo conectar al servidor en {ENDPOINT_URL}.")
        print(f"Detalles: {e}")
        print("Verifica lo siguiente:")
        print(f"1. Que el servidor Tornado esté ejecutándose en {SERVER_HOST} en el puerto {SERVER_PORT}.")
        print(f"2. Que el esquema '{SERVER_SCHEME}' sea el correcto (http o https).")
        print(f"   Si es 'https', asegúrate de que SSL esté configurado en tu servidor Tornado.")
        print(f"3. Que no haya un firewall bloqueando la conexión.")
    except requests.exceptions.Timeout:
        print("\n--- ERROR DE TIMEOUT ---")
        print(f"La petición a {ENDPOINT_URL} excedió el tiempo de espera.")
    except Exception as e:
        print("\n--- OCURRIÓ UN ERROR INESPERADO ---")
        print(f"Error: {e}")

if __name__ == "__main__":
    print("NOTA: Para una prueba completa del flujo de mensajes dentro de una sala,")
    print("asegúrate de que 'client_id' y 'room_id' en el payload correspondan a una")
    print("sesión activa en el servidor, y que 'client_id' sea un participante de esa sala.")
    print("De lo contrario, podrías obtener una respuesta exitosa (200 OK) pero con un mensaje")
    print("del servidor indicando que el otro usuario no fue encontrado o no estás en una sala válida,")
    print("o un error 403/404 si la sala/cliente no existe.\n")
    
    # Para hacer una prueba más completa, podrías primero llamar a /api/rooms (POST) para crear una sala
    # y luego usar ese room_id aquí. Por ahora, esta prueba se enfoca en el endpoint de mensaje.

    probar_envio_mensaje()