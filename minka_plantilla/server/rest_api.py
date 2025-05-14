import tornado.web
import tornado.ioloop
import json
import time
import logging

# Importa las estructuras de estado del WebSocket
# Asume que tu archivo de WS se llama `websocket_server.py` y define `sessions` y `clients`
from appv2 import sessions, clients

logging.basicConfig(level=logging.INFO)

# --------------------------------------------------
# BaseHandler para CORS y OPTIONS
# --------------------------------------------------
class BaseHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "origin, x-requested-with, content-type, accept, authorization")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()


# --------------------------------------------------
# GET /api/sessions
# Lista todas las sesiones activas
# --------------------------------------------------
class SessionsListHandler(BaseHandler):
    async def get(self):
        result = []
        for room_id, info in sessions.items():
            result.append({
                "room_id": room_id,
                "password": info.get("password"),
                "clients": info.get("clients", []),
                "last_activity": info.get("last_activity")
            })
        self.write({"sessions": result})


# --------------------------------------------------
# GET /api/sessions/{room_id}
# Detalle de una sesión concreta
# --------------------------------------------------
class SessionDetailHandler(BaseHandler):
    async def get(self, room_id):
        info = sessions.get(room_id)
        if not info:
            self.set_status(404)
            self.write({"error": "Session not found"})
            return
        self.write({
            "room_id": room_id,
            "password": info.get("password"),
            "clients": info.get("clients", []),
            "last_activity": info.get("last_activity")
        })


# --------------------------------------------------
# POST /api/event
# Recibe evento desde el móvil y lo broadcast a WS
# Body: { "room_id": "...", "message": {...} }
# --------------------------------------------------
class EventHandler(BaseHandler):
    async def post(self):
        try:
            body = json.loads(self.request.body)
            room_id = body["room_id"]
            message = body["message"]
        except (json.JSONDecodeError, KeyError):
            self.set_status(400)
            self.write({"error": "Invalid payload, requiere room_id y message"})
            return

        if room_id not in sessions:
            self.set_status(404)
            self.write({"error": "Session not found"})
            return

        # Actualiza timestamp de actividad
        sessions[room_id]["last_activity"] = time.time()

        # Reenvía a cada cliente conectado
        for client_id in sessions[room_id]["clients"]:
            ws_entry = clients.get(client_id)
            if not ws_entry:
                continue
            ws = ws_entry.get("ws")
            if not ws:
                continue
            try:
                ws.write_message({
                    "sender": "api",
                    "message": message
                })
                logging.info(f"[API→WS] enviado a {client_id} en {room_id}")
            except Exception:
                logging.exception(f"Error enviando WS a {client_id}")

        self.write({"ok": True})


# --------------------------------------------------
# Arranque del servidor REST
# --------------------------------------------------
if __name__ == "__main__":
    app = tornado.web.Application([
        (r"/api/sessions", SessionsListHandler),
        (r"/api/sessions/([^/]+)", SessionDetailHandler),
        (r"/api/event", EventHandler),
    ], 
    debug=True)

    port = 5002
    app.listen(port)
    logging.info(f"REST API escuchando en el puerto {port}")
    tornado.ioloop.IOLoop.current().start()
