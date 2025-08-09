import tornado.web
import tornado.ioloop
import asyncio
import logging
import signal
import time

from tornado.ioloop import PeriodicCallback

# Importar desde los módulos correctos
from session import init_redis_client, close_redis_client
from utils import crear_token_jwt, verificar_token_jwt
from handlers.websocket_handler import WebSocketHandler, active_websockets
from session_manager import initialize_server_state_from_redis, cleanup_sessions

# Importar configuraciones
from config import (
    SERVER_PORT, SERVER_HOST, LOG_LEVEL
)

# Importar handlers HTTP de forma consistente
from handlers.http_handlers import MainHandler, HealthHandler, MonitorHandler

# Importar configuración de logger
from logger_config import setup_logging

async def main():
    """Función principal que inicia el servidor Tornado."""
    # CORREGIDO: Nombre correcto de la función setup_logging
    setup_logging()
    
    try:
        # Inicializar conexión a Redis
        await init_redis_client()
    except Exception as e:
        logging.critical(f"[MAIN] No se pudo inicializar Redis al arrancar: {e}. Saliendo.")
        return  # Salir si Redis no está disponible

    # Definir rutas de la aplicación
    app = tornado.web.Application([
        (r"/", MainHandler),
        (r"/ws", WebSocketHandler),
        (r"/monitor", MonitorHandler),
        (r"/health", HealthHandler),
        (r"/api/status", HealthHandler),  # Alias para health check
    ], debug=(LOG_LEVEL == 'DEBUG'))
    
    # Iniciar el servidor
    app.listen(SERVER_PORT, SERVER_HOST)
    logging.info(f'Servidor MinkaV2 (Async Redis) iniciado en http://{SERVER_HOST}:{SERVER_PORT}')
    
    # Inicializar estado desde Redis
    await initialize_server_state_from_redis()
    
    # Iniciar tareas periódicas
    cleanup_task = PeriodicCallback(cleanup_sessions, 43200000)  # 60 segundos
    cleanup_task.start()
    
    # Configurar manejo de cierre elegante
    shutdown_event = asyncio.Event()
    
    async def graceful_shutdown():
        """Realiza un cierre controlado del servidor."""
        logging.info("Iniciando cierre elegante...")
        cleanup_task.stop()
        
        # Cerrar websockets activos
        for client_id, ws in list(active_websockets.items()):
            try:
                ws.close(code=1001, reason="Servidor apagándose")
            except Exception as e:
                logging.error(f"Error al cerrar websocket {client_id}: {e}")
            finally:
                if client_id in active_websockets:
                    del active_websockets[client_id]
        
        # Dar tiempo para que los cierres se procesen
        await asyncio.sleep(1)
        
        # Cerrar conexión a Redis
        await close_redis_client()
        
        # Detener el bucle de eventos de Tornado
        tornado.ioloop.IOLoop.current().stop()
        shutdown_event.set()
        logging.info("Cierre elegante completado.")

    def sig_handler(sig, frame):
        """Manejador de señales del sistema."""
        logging.warning(f"Recibida señal {sig}. Planificando cierre elegante.")
        # Es importante usar add_callback_from_signal para operaciones async desde un signal handler
        tornado.ioloop.IOLoop.current().add_callback_from_signal(
            lambda: asyncio.create_task(graceful_shutdown())
        )

    # Registrar manejadores de señales
    signal.signal(signal.SIGTERM, sig_handler)
    signal.signal(signal.SIGINT, sig_handler)

    # Esperar al evento de shutdown para finalizar main()
    await shutdown_event.wait()

if __name__ == "__main__":
    try:
        # Usar asyncio.run para gestionar el punto de entrada principal
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Proceso interrumpido por el usuario (KeyboardInterrupt).")
    except Exception as e:
        logging.critical(f"Error fatal en el servidor: {e}")
    finally:
        logging.info("Servidor MinkaV2 detenido.")
