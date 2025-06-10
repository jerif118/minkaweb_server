import requests
import json
import os
import unittest

# Archivo donde guardaremos el JWT para futuras ejecuciones
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".last_jwt_token")

# --- CONFIGURACIÓN (MODIFICA ESTO SEGÚN TU ENTORNO) ---
# Si tu servidor Tornado está en tu misma máquina:
SERVER_HOST = "127.0.0.1" # o "localhost"
SERVER_PORT = 5001

# CAMBIA A 'https' SI CONFIGURASTE SSL EN TORNADO, DE LO CONTRARIO USA 'http'
SERVER_SCHEME = "http"
# ---------------------------------------------------------

ENDPOINT_URL = f"{SERVER_SCHEME}://{SERVER_HOST}:{SERVER_PORT}/api/rooms/message"

# -------------------------------------------------------------------
# Utilidades para persistir / cargar el token JWT entre ejecuciones
# -------------------------------------------------------------------
def save_token(token: str) -> None:
    """Guarda el JWT en un archivo local para reutilizarlo."""
    if token:
        with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
            fh.write(token.strip())
        print(f"[INFO] Token JWT guardado en {TOKEN_FILE}")

def load_token() -> str | None:
    """Carga el JWT del archivo (si existe)."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return None

payload = {
    "client_id": "mobile-333fd0e2-5451-46b8-a666-7435eb7b109b",
    "room_id": "cf2415b1-2843-416b-aee3-f8cb59cf5ff7",
    "message": {
        "appName": "TestAppFromPython",
        "senderName": "Python Script",
        "text": "Hola desde el cliente de prueba Python!",
        "amount": 10.50,
        "date": 1678886400000,
        "id": "test-notif-id-python-001",
        "packageName": "com.example.pythontest",
        "info": "Información adicional opcional"
    }
}

# Si tenemos un JWT previo, añadirlo al payload
jwt_cached = load_token()
if jwt_cached:
    payload["jwt_token"] = jwt_cached
    print(f"[INFO] Usando JWT almacenado ({jwt_cached[:15]}…)")
else:
    print("[INFO] No se encontró JWT almacenado, se enviará sin token.")

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

            # -- Guardar token nuevo si viene en la respuesta --
            if isinstance(response_json, dict) and response_json.get("jwt_token"):
                save_token(response_json["jwt_token"])

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

# -------------------------------------------------------------------
# Tests básicos usando unittest
# -------------------------------------------------------------------
class APITestSuite(unittest.TestCase):
    def test_health(self):
        url = f"{SERVER_SCHEME}://{SERVER_HOST}:{SERVER_PORT}/health"
        r = requests.get(url, timeout=5)
        self.assertEqual(r.status_code, 200, "Health endpoint no devolvió 200")
        self.assertIn("status", r.json())

    def test_send_message(self):
        """POST al endpoint /api/rooms/message debería devolver 200 o 4xx controlado"""
        r = requests.post(ENDPOINT_URL, data=json.dumps(payload), headers=headers, timeout=10)
        self.assertIn(r.status_code, (200, 400, 403, 404))
        # Guarda token si vino
        try:
            data_json = r.json()
            if "jwt_token" in data_json:
                save_token(data_json["jwt_token"])
        except Exception:
            pass

if __name__ == "__main__":
    unittest.main(verbosity=2)