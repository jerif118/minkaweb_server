import json
import uuid
import paho.mqtt.client as mqtt
import asyncio
import threading
import tornado.ioloop
import tornado.web
import time

# Diccionario en memoria para controlar salas y clientes
salas = {}

# Crear cliente MQTT
broker = mqtt.Client() 
broker.connect("localhost", 1883)  # Cambia a 8080 con websockets si fuera necesario

# Suscribirse a eventos de creación y unión de sala
broker.subscribe("minka/pairing/create")
broker.subscribe("minka/pairing/join")

def crear_sala(client, data):
    room_id = str(uuid.uuid4())
    password = str(uuid.uuid4())[:6]
    salas[room_id] = {
        "clientes": [data["client_id"]],
        "password": password
    }
    # Responder al cliente con los datos de la sala
    client.publish(f"minka/client/{data['client_id']}", json.dumps({
        "room_created": True,
        "room_id": room_id,
        "password": password
    }))

def unir_a_sala(client, data):
    room_id = data.get("room_id")
    client_id = data.get("client_id")
    password = data.get("password")

    if room_id not in salas:
        client.publish(f"minka/client/{client_id}", json.dumps({
            "error": "Sala no encontrada"
        }))
        return

    if salas[room_id]["password"] != password:
        client.publish(f"minka/client/{client_id}", json.dumps({
            "error": "Contraseña incorrecta"
        }))
        return

    if len(salas[room_id]["clientes"]) >= 2:
        client.publish(f"minka/client/{client_id}", json.dumps({
            "error": "Sala llena"
        }))
        return

    salas[room_id]["clientes"].append(client_id)
    client.publish(f"minka/client/{client_id}", json.dumps({
        "event": "joined",
        "room_id": room_id
    }))

def on_message(client, userdata, msg):
    # Log incoming message for debugging
    print(f"[on_message] Received message on topic: {msg.topic}")
    print(f"[on_message] Raw payload: {msg.payload.decode()}")
    try:
        data = json.loads(msg.payload.decode())
        print(f"[on_message] Parsed data: {data}")
        if msg.topic == "minka/pairing/create":
            print("[crear_sala] Creating new room for client:", data.get("client_id"))
            crear_sala(client, data)
        elif msg.topic == "minka/pairing/join":
            print(f"[unir_a_sala] Client {data.get('client_id')} attempting to join room {data.get('room_id')}")
            unir_a_sala(client, data)
    except Exception as e:
        print(f"[on_message] Error processing message: {e}")

broker.on_message = on_message
broker.loop_start()   #  non‑blocking loop

class MonitorPage(tornado.web.RequestHandler):
    def get(self):
        # Render HTML table of active rooms and clients
        html = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Monitor</title>",
            "<style>body{font-family:Arial;margin:20px}table,th,td{border:1px solid #444;border-collapse:collapse;padding:6px}</style>",
            "</head><body><h1>Salas activas</h1><table><tr><th>Room ID</th><th>Password</th><th>Clientes</th></tr>"
        ]
        for room_id, info in salas.items():
            clients_list = ", ".join(info["clientes"])
            html.append(f"<tr><td>{room_id}</td><td>{info['password']}</td><td>{clients_list}</td></tr>")
        html.append("</table><h2>Clientes conectados</h2><table><tr><th>Client ID</th><th>Room ID</th></tr>")
        # Create a dictionary of clients and their rooms
        clients = {}
        for room_id, info in salas.items():
            for client_id in info["clientes"]:
                clients[client_id] = {"room_id": room_id}
        for client_id, cinfo in clients.items():
            html.append(f"<tr><td>{client_id}</td><td>{cinfo['room_id']}</td></tr>")
        html.append("</table></body></html>")
        self.write("".join(html))

def make_app():
    return tornado.web.Application([
        (r"/monitor",   MonitorPage),    # Página HTML con estado actual
    ])

def start_tornado():
    app = make_app() 
    app.listen(5001)
    print("Monitor HTTP  : http://localhost:5001/monitor")

threading.Thread(target=start_tornado, daemon=True).start()

# keep main thread alive
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    print("Stopping...")