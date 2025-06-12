#!/usr/bin/env python3
"""
Script de inicio para el servidor Minka WebSocket.

Este script sirve como punto de entrada principal para iniciar el servidor.
Maneja la configuración inicial, la inicialización del logger y el ciclo de vida del servidor.
"""

import asyncio
import logging
import sys
import os

# Asegurar que el directorio actual esté en el path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Importar la función principal desde main.py
from main import main
from logger_config import setup_logging

if __name__ == "__main__":
    # Configurar el logger antes de cualquier operación
    setup_logging()
    
    try:
        # Iniciar el servidor usando asyncio.run para gestionar el ciclo de vida
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Proceso interrumpido por el usuario (KeyboardInterrupt).")
    except Exception as e:
        logging.critical(f"Error fatal en el servidor: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logging.info("Servidor Minka detenido. Saliendo...")
        sys.exit(0)
