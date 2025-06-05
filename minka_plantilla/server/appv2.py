import tornado.websocket
import re, uuid, json, logging, time
from session import sessions, clients, RECONNECT_GRACE_PERIOD, SESSION_TIMEOUT
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

logging.basicConfig(level=logging.INFO)

# Tiempo máximo de inactividad de una sala en segundos (ej. 5 minutos)
# SESSION_TIMEOUT = 7_200

# RECONNECT_GRACE_PERIOD = 60  # Tiempo de gracia para reconexión en segundos

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
        self.intentional_disconnect = False
        client_id   = self.get_argument('client_id', None)
        action      = self.get_argument('action', None)
        room_id     = self.get_argument('room_id', None)
        room_passwd = self.get_argument('password', None)
        logging.info(f"[OPEN] client_id={client_id} action={action} room_id={room_id} password={room_passwd}")
        # Validar formato de client_id
        if client_id and not re.match(r'^[A-Za-z0-9_\-]{1,64}$', client_id):
            self.write_message({'error': 'client_id inválido'})
            self.close()
            return

        action = self.get_argument('action', None)

        if action == "create":
            if not client_id:
                self.write_message({'error': 'client_id es requerido'})
                self.close()
                return
                
            # Si es un cliente web, verificar si hay una sala con un móvil en doze
            if client_id.startswith("web-"):
                # Buscar salas con un solo cliente (móvil) en doze
                dozing_mobile_rooms = []
                for rid, info in sessions.items():
                    if len(info["clients"]) == 1:
                        mobile_id = info["clients"][0]
                        if not mobile_id.startswith("web-") and mobile_id in clients and clients[mobile_id].get('status') == 'dozing':
                            dozing_mobile_rooms.append((rid, info))
                
                # Si encontramos una sala con móvil en doze, conectar el web a esa sala
                if dozing_mobile_rooms:
                    # Usar la primera sala encontrada
                    room_id, room_info = dozing_mobile_rooms[0]
                    mobile_id = room_info["clients"][0]
                    logging.info(f"Web client {client_id} conectándose a sala con móvil {mobile_id} en doze")
                    
                    # Agregar cliente web a esta sala
                    sessions[room_id]["clients"].append(client_id)
                    clients[client_id] = {
                        'ws': self,
                        'status': 'connected',
                        'room_id': room_id
                    }
                    self.client_id = client_id
                    self.room_id = room_id
                    sessions[room_id]['last_activity'] = time.time()
                    
                    self.write_message({
                        "room_joined": True,
                        "room_id": room_id,
                        "password": sessions[room_id]["password"],
                        "info": "Te has conectado a una sala con un usuario móvil temporalmente inactivo."
                    })
                    return
            
            # Si el cliente ya existe, devolver misma sala
            if client_id in clients:
                # Ya existe: devolver misma sala y no crear otra
                existing_room = clients[client_id]['room_id']
                password      = sessions[existing_room]['password']
                self.client_id = client_id
                self.room_id   = existing_room
                self.write_message({
                    "room_created": True,
                    "room_id": existing_room,
                    "password": password,
                    "info": "Ya tenías una sala creada. Esperando otro usuario."
                })
                return
            
            # Crear nueva sala si no hay ninguna compatible
            room_id = str(uuid.uuid4())
            room_password = str(uuid.uuid4())[:6]
            sessions[room_id] = {
                "password": room_password,
                "clients": [client_id],
                "last_activity": time.time()
            }
            clients[client_id] = {
                'ws': self,
                'status': 'waiting',
                'room_id': room_id
            }
            self.client_id = client_id
            self.room_id = room_id
            self.write_message({
                "room_created": True,
                "room_id": room_id,
                "password": room_password,
                "info": "Room created. Waiting for another user to join."
            })
        
        elif action == "join":
            room_id = self.get_argument('room_id', None)
            room_password = self.get_argument('password', None)
                    # --- Re-conexión tras caída inesperada ---
            if client_id in clients and clients[client_id].get('status') == 'pending_reconnect':
                clients[client_id]['ws']     = self
                clients[client_id]['status'] = 'connected'
                clients[client_id].pop('pending_since', None)

                self.client_id = client_id
                self.room_id   = clients[client_id]['room_id']

                # Avisar al peer
                for other in sessions[self.room_id]["clients"]:
                    if other != client_id and other in clients and clients[other]['ws']:
                        clients[other]['ws'].write_message({'info': f'El usuario {client_id} se ha reconectado.'})
                return

            # Soporte de reconexión para un cliente existente (p.ej. web que se desconectó temporalmente)
            if client_id in sessions.get(room_id, {}).get("clients", []):
                # Si ya estaba en el room (p.ej. web reconectándose), reasignar ws y estado
                clients[client_id]['ws'] = self
                clients[client_id]['status'] = 'connected'
                self.client_id = client_id
                self.room_id = room_id
                # Notificar al peer que el usuario se ha reconectado
                other_ids = [cid for cid in sessions[room_id]["clients"] if cid != client_id]
                for other in other_ids:
                    if other in clients and clients[other]['ws']:
                        clients[other]['ws'].write_message({'info': f'El usuario {client_id} se ha reconectado.'})
                return

            # Re-conexión desde doze:
            if client_id in clients and clients[client_id].get('status') == 'dozing':
                # Restablecer ws y estado a 'connected'
                clients[client_id]['ws'] = self
                clients[client_id]['status'] = 'connected'
                self.client_id = client_id
                self.room_id = room_id
                # Enviar notificación al web cliente de que el móvil reingresó
                other_ids = [cid for cid in sessions[room_id]["clients"] if cid != client_id]
                if other_ids:
                    other = other_ids[0]
                    if other in clients and clients[other]['ws']:
                        clients[other]['ws'].write_message({'info': 'El otro usuario ha regresado desde doze.'})
                return

            if not client_id or not room_id or not room_password:
                self.write_message({'error': 'client_id, room_id y password son requeridos'})
                self.close()
                return
            
            if room_id and not re.match(r'^[0-9a-fA-F\-]{36}$', room_id):
                self.write_message({'error': 'room_id inválido'})
                self.close()
                return
            if room_password and not re.match(r'^[A-Za-z0-9]{6}$', room_password):
                self.write_message({'error': 'password inválido'})
                self.close()
                return

            if room_id in sessions and sessions[room_id]["password"] == room_password:
                # Verificar si es una reconexión de cliente web cuando había un móvil en doze
                if client_id not in sessions[room_id]["clients"]:
                    # Buscar si hay un cliente web anterior desconectado en esta sala
                    web_clients_in_room = [cid for cid in sessions[room_id]["clients"] if cid.startswith("web-")]
                    mobile_clients_in_room = [cid for cid in sessions[room_id]["clients"] if not cid.startswith("web-")]
                    
                    # Si hay un móvil en doze y el web se reconecta con nuevo ID, permitir la conexión
                    if len(mobile_clients_in_room) == 1 and len(web_clients_in_room) == 0:
                        mobile_client = mobile_clients_in_room[0]
                        if mobile_client in clients and clients[mobile_client].get('status') == 'dozing':
                            # Permitir que el nuevo cliente web se una
                            sessions[room_id]["clients"].append(client_id)
                            clients[client_id] = {
                                'ws': self,
                                'status': 'connected',
                                'room_id': room_id
                            }
                            self.client_id = client_id
                            self.room_id = room_id
                            sessions[room_id]['last_activity'] = time.time()
                            self.write_message({'info': 'Te has reconectado a la sala. El otro usuario está temporalmente inactivo.'})
                            return
                    
                    # Caso normal: agregar nuevo cliente si hay espacio
                    if len(sessions[room_id]["clients"]) < 2:
                        sessions[room_id]["clients"].append(client_id)
                        clients[client_id] = {
                            'ws': self,
                            'status': 'connected',
                            'room_id': room_id
                        }
                        self.client_id = client_id
                        self.room_id = room_id
                        self.write_message({'info': 'Te has unido a la sala.'})
                        other_client_id = [cid for cid in sessions[room_id]["clients"] if cid != client_id][0]
                        # Actualizar status del cliente original a 'connected' solo si no está en doze
                        if other_client_id in clients and clients[other_client_id].get('status') != 'dozing':
                            clients[other_client_id]['status'] = 'connected'
                            clients[other_client_id]['ws'].write_message({"event": "joined","client": client_id})
                    else:
                        self.write_message({'error': 'Sala llena. Solo se permiten dos usuarios.'})
                        self.close()
                        return
                else:
                    # Ya estaba en la sala pero no fue captado por el reatach anterior: rechazar duplicado
                    self.write_message({'error': 'Ya estás en la sala.'})
                    self.close()
                    return
            else:
                self.write_message({'error': 'Sala no existe o contraseña incorrecta'})
                self.close()
        
        else:
            self.write_message({'error': 'Acción no válida'})
            self.close()
    
    def on_message(self, message):
        logging.info(f"[DEBUG] on_message called with raw message: {message}")
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            self.write_message({'error': 'JSON inválido'})
            return
        # 1) Si llega {"action":"leave"}, notificamos al otro peer y limpiamos estructuras
        if data.get('action') == 'leave':
            self.intentional_disconnect = True
            reason = data.get('reason', '')
            logging.info(f"[DEBUG] leave action received from {self.client_id} in room {self.room_id} reason={reason}")
            # Si el cliente que hace leave es un cliente web, borrar sala incluso si el móvil está en doze
            if self.client_id.startswith("web-"):
                # Notificar al peer (si existe), independientemente de su estado
                peer_list = sessions.get(self.room_id, {}).get("clients", [])
                other_ids = [cid for cid in peer_list if cid != self.client_id]
                for peer in other_ids:
                    if peer in clients and clients[peer]['ws']:
                        clients[peer]['ws'].write_message({'info': 'El otro usuario se ha desconectado voluntariamente.'})
                # Eliminar cualquier cliente restante (con doze o conectado)
                for peer in other_ids:
                    if peer in clients:
                        del clients[peer]
                # Eliminar al cliente web
                if self.client_id in clients:
                    del clients[self.client_id]
                # Borrar la sala
                if self.room_id in sessions:
                    del sessions[self.room_id]
                return
            # Si el móvil entra en modo doze, marcamos estado y no notificamos al par
            if reason == "doze":
                # Marcar como dozing y mantener la sala
                if self.client_id in clients:
                    clients[self.client_id]['status'] = 'dozing'
                    clients[self.client_id]['ws'] = None
                # Actualizar última actividad de la sala
                if hasattr(self, 'room_id') and self.room_id in sessions:
                    sessions[self.room_id]['last_activity'] = time.time()
                return

            # Normal leave: notificar y remover tanto si el peer está doze como si está conectado
            if hasattr(self, 'room_id') and self.room_id in sessions:
                peer_list = sessions[self.room_id]["clients"]
                other_ids = [cid for cid in peer_list if cid != self.client_id]
                
                # Verificar si hay un cliente móvil en modo doze
                mobile_in_doze = False
                for peer in other_ids:
                    if peer in clients and not peer.startswith("web-") and clients[peer].get('status') == 'dozing':
                        mobile_in_doze = True
                        break
                
                # Si es un cliente web y hay un móvil en doze, manejar de forma especial
                if self.client_id.startswith("web-") and mobile_in_doze:
                    # Solo eliminar este cliente web, pero mantener la sala y el cliente móvil
                    if self.client_id in clients:
                        del clients[self.client_id]
                    # Remover solo el cliente web de la lista de clientes de la sala
                    if self.client_id in sessions[self.room_id]["clients"]:
                        sessions[self.room_id]["clients"].remove(self.client_id)
                    # Actualizar timestamp de actividad para evitar que expire
                    sessions[self.room_id]['last_activity'] = time.time()
                    return
                
                # Procesar normalmente para otros casos
                for peer in other_ids:
                    if peer in clients:
                        # Si está conectado, notificar
                        if clients[peer]['ws']:
                            clients[peer]['ws'].write_message({'info': 'El otro usuario se ha desconectado.'})
                # Remover al cliente que hace leave
                sessions[self.room_id]["clients"].remove(self.client_id)
                if self.client_id in clients:
                    del clients[self.client_id]
                # Limpiar lista de clientes en la sala
                sessions[self.room_id]["clients"] = [
                    cid for cid in sessions[self.room_id]["clients"] if cid in clients
                ]
                # Si la sala queda vacía, eliminarla
                if not sessions[self.room_id]["clients"]:
                    del sessions[self.room_id]
            return

        msg = data.get('message')

        # Serializamos a JSON para medir tamaño (y aceptar dicts)
        try:
            msg_serializado = json.dumps(msg)
        except Exception:
            self.write_message({'error': 'No pude serializar el mensaje'})
            return

        if len(msg_serializado) > 500:
            self.write_message({'error': 'Mensaje demasiado largo'})
            return

        # ¡Ya podemos hacer broadcast!
        if self.room_id in sessions and len(sessions[self.room_id]["clients"]) == 2:
            other = [cid for cid in sessions[self.room_id]["clients"] if cid != self.client_id][0]
            logging.info(f"[DEBUG] Broadcasting to {other}: {msg}")
            # Reenvía el objeto completo, no su str()
            clients[other]['ws'].write_message({
                'sender_id': self.client_id,
                'message'  : msg
            })
            logging.info(f'[BROADCAST] de {self.client_id} → {other}: {msg}')
        else:
            self.write_message({'info': 'Aún no emparejado.'})

    def on_close(self):
        # If this is a voluntary disconnect (handled in on_message with 'leave'), skip all cleanup here.
        if getattr(self, 'intentional_disconnect', False):
            return
        client_id = getattr(self, 'client_id', None)
        room_id = getattr(self, 'room_id', None)
        if not client_id or not room_id:
            return
        logging.info(f"[CLOSE] Desconexión de cliente {client_id} en sala {room_id}")
        # Verificar si hay un cliente móvil en modo doze en esta sala
        mobile_in_doze = False
        if room_id in sessions:
            for cid in sessions[room_id]["clients"]:
                if cid != client_id and cid in clients and not cid.startswith("web-") and clients[cid].get('status') == 'dozing':
                    mobile_in_doze = True
                    break
        # Si el cliente que cierra es un cliente web y hay un móvil en doze
        if client_id and client_id.startswith("web-") and mobile_in_doze:
            logging.info(f"Web client {client_id} desconectado pero hay móvil en doze. Preservando sala.")
            # Marcar el cliente web como desconectado pero mantener en la lista de clientes
            if client_id in clients:
                clients[client_id]['ws'] = None
                clients[client_id]['status'] = 'disconnected'
            # Actualizar timestamp para evitar expiración
            if room_id in sessions:
                sessions[room_id]['last_activity'] = time.time()
            return
        # Si el cliente que cierra es un cliente web y no hay móvil en doze, limpieza completa
        elif client_id and client_id.startswith("web-"):
            if room_id and room_id in sessions:
                peer_ids = [cid for cid in sessions[room_id]["clients"] if cid != client_id]
                # (No notify peers here; voluntary leave handled in on_message)
                for peer in peer_ids:
                    if peer in clients:
                        del clients[peer]
                if client_id in clients:
                    del clients[client_id]
                del sessions[room_id]
            return
        # Si estaba en estado 'dozing', no notificamos ni removemos la sala
        if client_id in clients and clients[client_id].get('status') == 'dozing':
            logging.info(f'Cliente {client_id} en dozing, cerrando socket sin remover sala.')
            clients[client_id]['ws'] = None
            return
        # Normal close: notificar y remover posibles peers en doze
        if room_id and room_id in sessions:
            peer_ids = [cid for cid in sessions[room_id]["clients"] if cid != client_id]
            for peer in peer_ids:
                if peer in clients:
                    # Si está conectado, notificar
                    if clients[peer]['ws']:
                        clients[peer]['ws'].write_message({'info': 'El otro usuario se ha desconectado.'})
            # Remover al cliente que cerró
            del clients[client_id]
            sessions[room_id]["clients"].remove(client_id)
            # Limpiar lista de clientes en la sala
            sessions[room_id]["clients"] = [
                cid for cid in sessions[room_id]["clients"] if cid in clients
            ]
            # Si la sala queda vacía, eliminarla
            if not sessions[room_id]["clients"]:
                del sessions[room_id]
            return
    def on_close(self):
        # Salir silenciosamente si el cierre fue voluntario (marcado en on_message)
        if getattr(self, 'intentional_disconnect', False):
            return
        
        client_id = getattr(self, 'client_id', None)
        room_id = getattr(self, 'room_id', None)
        if not client_id or not room_id:
            return
        logging.info(f"[CLOSE] Desconexion inexperada del cliente {client_id},room {room_id}"f"Reconectando en {RECONNECT_GRACE_PERIOD} segundos")
        
        if client_id in clients:
            clients[client_id]['status'] = 'pending_reconnect'
            clients[client_id]['ws'] = None  # Desconectar el WebSocket, pero mantener el client
            clients[client_id]['pending_since'] = time.time()
        
        for peer in sessions.get(room_id, {}).get('clients', []):
            if peer != client_id and peer in clients and clients[peer]['ws']:
                clients[peer]['ws'].write_message({'warning': 'El otro usuario perdió la conexión. Esperando que se reconecte…'})
#
#Configuración de la aplicación y rutas

def make_app():
    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/ws", WebSocketHandler),
        (r"/monitor", MonitorHandler),
        (r"/health", HealthHandler),
    ])

def cleanup_sessions():
    now = time.time()
    expired = []
    
    # Identificar salas expiradas, pero mantener las que tienen un móvil en doze
    for rid, info in sessions.items():
        # Comprobar si hay algún cliente móvil en estado dozing
        has_dozing_mobile = False
        for cid in info['clients']:
            if cid in clients and not cid.startswith("web-") and clients[cid].get('status') == 'dozing':
                has_dozing_mobile = True
                break
        
        # Si tiene un móvil en doze, usar un timeout más largo (p.ej. 24 horas)
        if has_dozing_mobile:
            doze_timeout = 86400  # 24 horas
            if now - info.get('last_activity', now) > doze_timeout:
                expired.append(rid)
        else:
            # Timeout normal para otras salas
            if now - info.get('last_activity', now) > SESSION_TIMEOUT:
                expired.append(rid)
        # --- Expirar clientes que no se reconectaron ---
    for cid, info in list(clients.items()):
        if info.get('status') == 'pending_reconnect' \
           and now - info.get('pending_since', now) > RECONNECT_GRACE_PERIOD:
            logging.info(f'Cliente {cid} no se reconectó a tiempo; limpieza definitiva.')
            rid = info['room_id']

            # Avisar al peer
            for peer in sessions.get(rid, {}).get('clients', []):
                if peer != cid and peer in clients and clients[peer]['ws']:
                    clients[peer]['ws'].write_message({'info': 'El otro usuario se desconectó definitivamente.'})

            # Eliminar cliente y sala si queda vacía
            sessions[rid]['clients'].remove(cid)
            del clients[cid]
            if not sessions[rid]['clients']:
                expired.append(rid)
    
    # Limpiar salas expiradas
    for rid in expired:
        for cid in sessions[rid]['clients']:
            if cid in clients:
                try:
                    if clients[cid]['ws']:
                        clients[cid]['ws'].write_message({'info': 'Sesión expirada'})
                        clients[cid]['ws'].close()
                except:
                    pass
                del clients[cid]
        del sessions[rid]
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


PeriodicCallback(cleanup_sessions, 60000).start()

#
#Iniciar el servidor

if __name__ == "__main__":
    app = make_app()
    app.listen(5001)
    logging.info('Servidor iniciado en el puerto 5001')
    tornado.ioloop.IOLoop.current().start()