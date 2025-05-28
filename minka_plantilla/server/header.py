# run.py
import tornado.httpserver
import tornado.netutil
import tornado.ioloop
import tornado.web

from api import api_handlers
from ws_service import ws_handlers

if __name__ == "__main__":
    # 1) Abrir socket en el puerto 5001
    sockets = tornado.netutil.bind_sockets(5001)

    # 2) Crear dos HTTPServer, cada uno con su Application
    server_api = tornado.httpserver.HTTPServer(tornado.web.Application(api_handlers))
    server_ws  = tornado.httpserver.HTTPServer(tornado.web.Application(ws_handlers))

    # 3) Asociar ambos al mismo socket
    server_api.add_sockets(sockets)
    server_ws .add_sockets(sockets)

    # 4) Arrancar el loop
    logging.info("Servidor combinado escuchando en :5001")
    tornado.ioloop.IOLoop.current().start()