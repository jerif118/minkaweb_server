# server.py
import logging, signal, time, tornado.ioloop
from tornado.ioloop import PeriodicCallback
import tornado.web

from session import sessions, clients, SESSION_TIMEOUT
from appv2   import WebSocketHandler
from api  import (
    MainHandler, HealthHandler, MonitorHandler,
    SendMessageHandler, CreateRoomHandler,
    JoinRoomHandler, LeaveRoomHandler, ListRoomsHandler
)

logging.basicConfig(level=logging.INFO)

def cleanup_sessions():
    now = time.time()
    expired = [rid for rid, info in sessions.items()
               if now - info.get('last_activity', now) > SESSION_TIMEOUT]
    for rid in expired:
        for cid in sessions[rid]['clients'][:]:
            if cid in clients:
                ws = clients[cid].get('ws')
                try:
                    if ws: ws.close(code=1000, reason="Session Timeout")
                finally:
                    clients.pop(cid, None)
        sessions.pop(rid, None)
        logging.info(f"Sesión {rid} expirada y removida")

def make_app():
    return tornado.web.Application([
        (r"/",                  MainHandler),
        (r"/health",            HealthHandler),
        (r"/monitor",           MonitorHandler),
        (r"/ws",                WebSocketHandler),

        # --- REST API ---
        (r"/api/rooms",             CreateRoomHandler),
        (r"/api/rooms/join",        JoinRoomHandler),
        (r"/api/rooms/leave",       LeaveRoomHandler),
        (r"/api/rooms/message",     SendMessageHandler),
        (r"/api/rooms_all",         ListRoomsHandler),
    ])

def shutdown(sig, frame):
    logging.info("→ Señal %s recibida, apagando…", sig)
    tornado.ioloop.IOLoop.current().add_callback_from_signal(
        tornado.ioloop.IOLoop.current().stop
    )

if __name__ == "__main__":
    # Ctrl-C o Docker stop ⇒ cierre limpio
    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    PeriodicCallback(cleanup_sessions, 60_000).start()  # cada minuto
    make_app().listen(5001)  # PUERTO ÚNICO
    logging.info("★ Servidor Minka API+WS escuchando en http://0.0.0.0:5001")
    tornado.ioloop.IOLoop.current().start()