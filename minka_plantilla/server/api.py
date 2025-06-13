"""
DEPRECATED: Este archivo contiene endpoints REST heredados que ya no están en uso.

El servidor principal (`main.py`) utiliza WebSockets y Redis para toda la funcionalidad.
Los endpoints REST en este archivo usan variables globales que están vacías y 
causarán errores 403 porque no están sincronizados con el estado en Redis.

Para nueva funcionalidad o depuración, utilice:
- /ws para conexiones WebSocket
- /health para verificar el estado del servidor
- /monitor para ver el estado de sesiones y clientes activos

Este archivo se mantiene para referencia histórica pero NO debe ser usado
en producción.
"""

import tornado.web, re, json, uuid, time, logging
from tornado.escape import xhtml_escape
from session import sessions, clients, SESSION_TIMEOUT
# import tornado.ioloop
# import tornado.web
# import re
# from tornado.escape import xhtml_escape
from tornado.ioloop import PeriodicCallback
# import json
# import uuid
# import time
# import logging
# from handlers.websocket_handler import WebSocketHandler

# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# # Tiempo máximo de inactividad de una sala en segundos (ej. 5 minutos)
# SESSION_TIMEOUT = 7_200

# # Estructura de sessions: cada room_id → dict con keys: password, clients, last_activity
# sessions = {}

# # Diccionario para almacenar la información de los clientes conectados
# clients = {}

# #
# # Handlers Base para manejo de CORS y solicitudes OPTIONS
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


# --- Puente Doze → WebSocket ---
class SendMessageHandler(BaseHandler):
    """
    Recibe mensajes de un cliente vía HTTP (p.ej. móvil en Doze) y los
    reenvía al peer conectado por WebSocket.
    Espera JSON: { "client_id": ..., "room_id": ..., "message": ... }
    """
    def post(self):
        try:
            data = json.loads(self.request.body)
            client_id = data.get('client_id')
            room_id   = data.get('room_id')
            message   = data.get('message')

            if not client_id or not room_id or message is None:
                self.set_status(400)
                self.write({"error": "client_id, room_id y message son requeridos"})
                logging.warning(f"[API][SEND] Faltan parámetros client_id={client_id} room_id={room_id}")
                return

            if room_id not in sessions or client_id not in sessions[room_id]["clients"]:
                self.set_status(403)
                self.write({"error": "No estás en una sala válida"})
                logging.warning(f"[API][SEND] Cliente {client_id} no está en la sala {room_id}")
                return

            # Limitar tamaño del payload (como en WebSocket)
            try:
                msg_serializado = json.dumps(message)
            except Exception:
                self.set_status(400)
                self.write({"error": "No pude serializar el mensaje"})
                logging.error(f"[API][SEND] No se pudo serializar mensaje de {client_id}: {message}")
                return

            if len(msg_serializado) > 500:
                self.set_status(400)
                self.write({"error": "Mensaje demasiado largo"})
                return

            # Reenviar al peer conectado
            delivered = False
            for peer in sessions[room_id]["clients"]:
                if peer != client_id and peer in clients and clients[peer].get('ws'):
                    clients[peer]['ws'].write_message({
                        'sender_id': client_id,
                        'message'  : message
                    })
                    delivered = True

            if delivered:
                self.write({"info": "Mensaje enviado al peer por WebSocket"})
            else:
                self.write({"info": "El peer no está conectado; mensaje no entregado"})
        except Exception as e:
            logging.error(f"[API][SEND] Error: {e}", exc_info=True)
            self.set_status(500)
            self.write({"error": "Error interno del servidor"})


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
        for room_id, info in sessions.items():
            clients_list = ", ".join([xhtml_escape(cid) for cid in info["clients"]])
            html += f"<tr><td>{xhtml_escape(room_id)}</td><td>{xhtml_escape(info['password'])}</td><td>{clients_list}</td></tr>"
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
        for client_id, cl_info in clients.items():
            room = xhtml_escape(cl_info.get('room_id', ''))
            status = xhtml_escape(cl_info.get('status', ''))
            html += f"<tr><td>{xhtml_escape(client_id)}</td><td>{room}</td><td>{status}</td></tr>"
        html += """
          </table>
        </body>
        </html>
        """
        self.write(html)

#
# Handler del WebSocket para emparejamiento y comunicación entre dos usuarios
#

#
# Configuración de la aplicación y rutas
#
# def make_app():
#     return tornado.web.Application([
#         (r"/", MainHandler),
#         (r"/ws", WebSocketHandler),
#         (r"/monitor", MonitorHandler),
#         (r"/health", HealthHandler),
#         (r"/api/rooms/message", SendMessageHandler),  #  ← nuevo endpoint
#     ])

# Variables globales para sesiones y clientes

class CreateRoomHandler(BaseHandler):
    def post(self):
        try:
            data = json.loads(self.request.body)
            client_id = data.get('client_id')
            if not client_id:
                self.set_status(400)
                self.write({"error": "client_id es requerido"})
                logging.warning("[API][CREATE] client_id es requerido")
                return
            
            # Validar formato de client_id
            if not re.match(r'^[A-Za-z0-9_\-]{1,64}$', client_id):
                self.set_status(400)
                self.write({"error": "client_id inválido"})
                logging.warning(f"[API][CREATE] client_id inválido: {client_id}")
                return
            
            if client_id in clients:
                room_id = clients[client_id]['room_id']
                password = sessions[room_id]['password']
                self.write({
                    "room_created": True,
                    "room_id": room_id,
                    "password": password,
                    "info": "Ya tenías una sala creada. Esperando otro usuario."
                })
                logging.info(f"[API][CREATE] Client {client_id} ya tenía sala {room_id}")
                return
            
            room_id = str(uuid.uuid4())
            room_password = str(uuid.uuid4())[:6]
            sessions[room_id] = {
                "password": room_password,
                "clients": [client_id],
                "last_activity": time.time()
            }
            clients[client_id] = {
                'status': 'waiting',
                'room_id': room_id
            }
            
            self.write({
                "room_created": True,
                "room_id": room_id,
                "password": room_password,
                "info": "Room created. Waiting for another user to join."
            })
            logging.info(f"[API][CREATE] Client {client_id} creó sala {room_id}")

        except Exception as e:
            logging.error(f"[API][CREATE] Error: {e}", exc_info=True)
            self.set_status(500)
            self.write({"error": "Error interno del servidor"})


class JoinRoomHandler(BaseHandler):
    def post(self):
        try:
            data = json.loads(self.request.body)
            client_id = data.get('client_id')
            room_id = data.get('room_id')
            password = data.get('password')

            if not client_id or not room_id or not password:
                self.set_status(400)
                self.write({"error": "client_id, room_id y password son requeridos"})
                logging.warning(f"[API][JOIN] Faltan parámetros client_id={client_id} room_id={room_id} password={password}")
                return

            # Validar formato
            if not re.match(r'^[A-Za-z0-9_\-]{1,64}$', client_id):
                self.set_status(400)
                self.write({"error": "client_id inválido"})
                logging.warning(f"[API][JOIN] client_id inválido: {client_id}")
                return
            if not re.match(r'^[0-9a-fA-F\-]{36}$', room_id):
                self.set_status(400)
                self.write({"error": "room_id inválido"})
                logging.warning(f"[API][JOIN] room_id inválido: {room_id}")
                return
            if not re.match(r'^[A-Za-z0-9]{6}$', password):
                self.set_status(400)
                self.write({"error": "password inválido"})
                logging.warning(f"[API][JOIN] password inválido: {password}")
                return

            if room_id not in sessions:
                self.set_status(404)
                self.write({"error": "Sala no existe"})
                logging.warning(f"[API][JOIN] Sala no existe: {room_id}")
                return
            
            if sessions[room_id]["password"] != password:
                self.set_status(403)
                self.write({"error": "Contraseña incorrecta"})
                logging.warning(f"[API][JOIN] Contraseña incorrecta para sala {room_id}")
                return
            
            if len(sessions[room_id]["clients"]) >= 2:
                self.set_status(403)
                self.write({"error": "Sala llena. Solo se permiten dos usuarios."})
                logging.warning(f"[API][JOIN] Sala llena {room_id} cliente: {client_id}")
                return
            
            sessions[room_id]["clients"].append(client_id)
            clients[client_id] = {
                'status': 'connected',
                'room_id': room_id
            }
            
            self.write({"info": "Te has unido a la sala."})
            logging.info(f"[API][JOIN] Client {client_id} se unió a la sala {room_id}")

        except Exception as e:
            logging.error(f"[API][JOIN] Error: {e}", exc_info=True)
            self.set_status(500)
            self.write({"error": "Error interno del servidor"})



class LeaveRoomHandler(BaseHandler):
    def post(self):
        try:
            data = json.loads(self.request.body)
            client_id = data.get('client_id')
            room_id = data.get('room_id')
            if not client_id or not room_id:
                self.set_status(400)
                self.write({"error": "client_id y room_id son requeridos"})
                logging.warning(f"[API][LEAVE] Parámetros faltantes: client_id={client_id}, room_id={room_id}")
                return
            
            if client_id in clients:
                del clients[client_id]
            
            if room_id in sessions and client_id in sessions[room_id]["clients"]:
                sessions[room_id]["clients"].remove(client_id)
                if len(sessions[room_id]["clients"]) == 1:
                    other_client_id = sessions[room_id]["clients"][0]
                    logging.info(f"[API][LEAVE] Cliente {client_id} salió de la sala {room_id}, notificando a {other_client_id}")
                if not sessions[room_id]["clients"]:
                    del sessions[room_id]
                    logging.info(f"[API][LEAVE] Sala {room_id} eliminada porque está vacía después que salió {client_id}")
            
            self.write({"info": "Has salido de la sala."})
            logging.info(f"[API][LEAVE] Cliente {client_id} salió de la sala {room_id}")

        except Exception as e:
            logging.error(f"[API][LEAVE] Error: {e}", exc_info=True)
            self.set_status(500)
            self.write({"error": "Error interno del servidor"})



class SendMessageHandler(BaseHandler):
    def post(self):
        try:
            data = json.loads(self.request.body)
            client_id = data.get('client_id')
            room_id = data.get('room_id')
            message = data.get('message')

            if not client_id or not room_id or message is None:
                self.set_status(400)
                self.write({"error": "client_id, room_id y message son requeridos"})
                logging.warning(f"[API][SEND] Parámetros faltantes: client_id={client_id}, room_id={room_id}, message={message}")
                return

            if room_id not in sessions or client_id not in sessions[room_id]["clients"]:
                self.set_status(403)
                self.write({"error": "No estás en una sala válida"})
                logging.warning(f"[API][SEND] Cliente {client_id} no está en la sala {room_id}")
                return

            try:
                msg_serializado = json.dumps(message)
            except Exception:
                self.set_status(400)
                self.write({"error": "No pude serializar el mensaje"})
                logging.error(f"[API][SEND] No se pudo serializar el mensaje del cliente {client_id}: {message}", exc_info=True)
                return
            
            if len(msg_serializado) > 500:
                self.set_status(400)
                self.write({"error": "Mensaje demasiado largo"})
                logging.warning(f"[API][SEND] Mensaje demasiado largo del cliente {client_id}: {len(msg_serializado)} bytes")
                return
            
            # Simular broadcast entre los 2 clientes
            other_clients = [cid for cid in sessions[room_id]["clients"] if cid != client_id]
            if other_clients:
                other = other_clients[0]
                logging.info(f"[API][SEND] Client {client_id} envió mensaje a Room {room_id} (Other: {other}): {msg_serializado}")
                # Aquí podrías guardar mensajes o gestionar una cola para que el otro cliente los consulte
            else:
                logging.info(f"[API][SEND] Client {client_id} envió mensaje pero no está emparejado en Room {room_id}: {msg_serializado}")
                self.write({"info": "Aún no emparejado o el otro usuario se desconectó."})
                return

            self.write({"info": "Mensaje recibido."})

        except Exception as e:
            logging.error(f"[API][SEND] Error: {e}", exc_info=True)
            self.set_status(500)
            self.write({"error": "Error interno del servidor"})


class ListRoomsHandler(BaseHandler):
    def get(self):
        try:
            rooms_list = []
            for room_id, session in sessions.items():
                rooms_list.append({
                    "room_id": room_id,
                    "clients": session["clients"],
                    "last_activity": session["last_activity"]
                })
            self.write({"rooms": rooms_list})
            logging.info(f"[API][LIST] Listando {len(rooms_list)} salas activas")
        except Exception as e:
            logging.error(f"[API][LIST] Error: {e}", exc_info=True)
            self.set_status(500)
            self.write({"error": "Error interno del servidor"})







def cleanup_sessions():
    now = time.time()
    expired = [rid for rid, info in sessions.items()
               if now - info.get('last_activity', now) > SESSION_TIMEOUT]
    for rid in expired:
        for cid in list(sessions[rid]['clients']): # Iterate over a copy for safe removal
            if cid in clients:
                try:
                    logging.info(f'Cliente {cid} en sala {rid} desconectado por inactividad.')
                    clients[cid]['ws'].write_message({'info': 'Sesión expirada por inactividad.'})
                    clients[cid]['ws'].close(code=1000, reason="Session Timeout")
                except Exception as e:
                    logging.error(f"Error closing WebSocket for client {cid} during cleanup: {e}")
                # Removal from clients dict will happen in on_close
        # If after attempting to disconnect clients, the room is empty, delete it
        # Note: on_close should handle removing clients from sessions[rid]['clients']
        # This check is more of a safeguard if on_close failed or was not called for some reason
        if not sessions[rid]['clients']:
             del sessions[rid]
             logging.info(f'Sesión {rid} expirada y removida')
        elif sessions[rid]['clients'] and all(c not in clients for c in sessions[rid]['clients']):
            # If clients are no longer in the global clients dict but still listed in session (stale)
            del sessions[rid]
            logging.info(f'Sesión {rid} (stale) expirada y removida')


import signal, logging
import tornado.ioloop

def shutdown(sig, frame):
    logging.info("→ Recibida señal %s, iniciando apagado del servidor...", sig)
    io_loop = tornado.ioloop.IOLoop.current()

    # Notificar y cerrar conexiones de clientes
    for client_id, client_info in list(clients.items()): # Iterate over a copy
        try:
            if client_info['ws'] and client_info['ws'].ws_connection:
                client_info['ws'].write_message({'info': 'Servidor reiniciando. Por favor, reconecte.'})
                client_info['ws'].close(code=1001, reason="Server Restarting")
                logging.info(f"Notificado y cerrado conexión para cliente {client_id}")
        except Exception as e:
            logging.error(f"Error notificando/cerrando cliente {client_id}: {e}")

    # Dar un pequeño tiempo para que los mensajes de cierre se envíen
    io_loop.add_timeout(time.time() + 1, lambda: io_loop.stop())
    logging.info("IOLoop se detendrá en breve.")


signal.signal(signal.SIGTERM, shutdown)  # usado por Docker / systemd
signal.signal(signal.SIGINT,  shutdown)  # Ctrl-C local

# Programar limpieza de sesiones cada minuto
PeriodicCallback(cleanup_sessions, 60000).start()

#
# Iniciar el servidor
#
# if __name__ == "__main__":
#     app = make_app()
#     app.listen(5001)
#     logging.info('Servidor iniciado en el puerto 5001')
#     tornado.ioloop.IOLoop.current().start()