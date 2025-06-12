#!/usr/bin/env python3
"""
Script para iniciar el servidor Minka.
Ejecutar con: python run_server.py
"""

import asyncio
import logging
from main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Proceso interrumpido por el usuario (KeyboardInterrupt).")
    except Exception as e:
        logging.critical(f"Error fatal en el servidor: {e}")
    finally:
        logging.info("Servidor MinkaV2 detenido.")
