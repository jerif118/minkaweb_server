import logging
import tornado.ioloop
import tornado.web
import paho.mqtt.client as mqtt

sessions = {}
clients = {}

# MQTT integration for receiving messages from mobile app
def on_mqtt_connect(client, userdata, flags, rc):
    if rc == 0:
        logging.info(f"[MQTT] Conectado al broker, suscribiendo topics")
        client.subscribe("notificaciones/#")
    else:
        logging.error(f"[MQTT] Error al conectar al broker, rc={rc}")

def on_mqtt_message(client, userdata, msg):
    try:
        payload = msg.payload.decode('utf-8', errors='ignore')
        topic = msg.topic
        logging.info(f"[MQTT] Mensaje recibido topic={topic}: {payload}")
        # Extraer room_id
        room_id = topic.split("/")[-1]
        if room_id in sessions:
            for cid in sessions[room_id]["clients"]:
                ws = clients[cid]['ws']
                try:
                    ws.write_message(payload)
                except Exception as e:
                    logging.error(f"[MQTT] Error al enviar a ws {cid}: {e}")
    except Exception as e:
        logging.error(f"[MQTT] Error procesando mensaje: {e}")

# Setup MQTT client
mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_message = on_mqtt_message
mqtt_client.connect("localhost", 1883, 60)
mqtt_client.loop_start()

def make_app():
    # Placeholder for Tornado application setup
    return tornado.web.Application([])

if __name__ == "__main__":
    app = make_app()
    app.listen(5001)
    tornado.ioloop.IOLoop.current().start()

#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import signal

import paho.mqtt.client as mqtt
import websockets

# Configure logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

# MQTT settings (adjust if needed)
MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "notificaciones/#")
MQTT_KEEPALIVE = 60

# WebSocket settings for bridge endpoint
WS_BRIDGE_URL = os.environ.get("WS_BRIDGE_URL", "ws://localhost:5001/bridge")

# Async queue to pass messages from MQTT to WebSocket
queue = asyncio.Queue()

# MQTT callbacks
def on_mqtt_connect(client, userdata, flags, rc):
    if rc == 0:
        logging.info(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
        client.subscribe(MQTT_TOPIC)
        logging.info(f"Subscribed to topic '{MQTT_TOPIC}'")
    else:
        logging.error(f"Failed to connect to MQTT broker, return code {rc}")

def on_mqtt_message(client, userdata, msg):
    payload = msg.payload.decode('utf-8', errors='ignore')
    logging.info(f"MQTT message received: topic={msg.topic} message={payload}")
    asyncio.run_coroutine_threadsafe(queue.put((msg.topic, payload)), asyncio.get_event_loop())

# Setup MQTT client
def setup_mqtt():
    client = mqtt.Client()
    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message
    client.connect(MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE)
    client.loop_start()
    return client

# WebSocket sender coroutine
async def websocket_sender():
    while True:
        try:
            async with websockets.connect(WS_BRIDGE_URL) as ws:
                logging.info(f"Connected to WebSocket bridge at {WS_BRIDGE_URL}")
                while True:
                    topic, message = await queue.get()
                    payload = json.dumps({"topic": topic, "message": message})
                    await ws.send(payload)
                    logging.info(f"Forwarded to WS: {payload}")
        except Exception as e:
            logging.warning(f"WebSocket bridge disconnected: {e}")
            await asyncio.sleep(5)  # wait before retry

# Graceful shutdown handler
def shutdown(loop, mqtt_client):
    logging.info("Shutting down bridge...")
    mqtt_client.loop_stop()
    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.stop()

# Main entry point
def main():
    mqtt_client = setup_mqtt()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda sig=sig: shutdown(loop, mqtt_client))
    try:
        loop.run_until_complete(websocket_sender())
    except asyncio.CancelledError:
        pass
    finally:
        logging.info("Bridge stopped.")

if __name__ == "__main__":
    main()