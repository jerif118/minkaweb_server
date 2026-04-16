import websocket
import json
import uuid
import sys
import _thread  # o "import threading" si prefieres

def on_message(ws, message):
    data = json.loads(message)
    print("Mensaje recibido:", data)

def on_error(ws, error):
    print("Error:", error)

def on_close(ws, close_status_code, close_msg):
    print("Conexión cerrada:", close_status_code, close_msg)

def on_open(ws):
    print("Conexión WebSocket abierta.")
    print("Cuando se te solicite, ingresa JSON (p.ej. {\"app\":\"X\",\"monto\":1000,\"hora\":\"12:00\"}) o texto (o 'exit').")

    def run():
        while True:
            user_input = input("\nIngresa JSON (p.ej. {\"app\":\"X\",\"monto\":1000,\"hora\":\"12:00\"}) o texto (o 'exit'): ")
            if user_input.lower() == "exit":
                ws.close()
                break

            # Intentar parsear JSON; si falla, tratarlo como texto simple
            try:
                message_content = json.loads(user_input)
            except json.JSONDecodeError:
                message_content = user_input

            # Construir payload con campo 'message'
            payload = {
                "message": message_content
            }

            # Enviar datos al servidor
            ws.send(json.dumps(payload))

    # Ejecutar la función run en un thread para no bloquear el evento principal
    _thread.start_new_thread(run, ())

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python simulate_client.py <room_id> <password>")
        sys.exit(1)
    
    room_id = sys.argv[1]
    password = sys.argv[2]
    # Generar un client_id único para el cliente simulado
    client_id = f"python-client-{uuid.uuid4()}"

    # Construir la URL de conexión con los parámetros necesarios
    ws_url = (
        "ws://localhost:5001/ws"
        f"?action=join"
        f"&client_id={client_id}"
        f"&room_id={room_id}"
        f"&room_password={password}"  # Asegúrate que este parámetro coincide con lo esperado
    )

    print("Conectando al servidor con URL:", ws_url)
    
    ws_app = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    
    ws_app.run_forever()