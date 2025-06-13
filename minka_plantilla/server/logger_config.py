"""
Configuración del sistema de logging para el servidor Minka.

Este módulo proporciona funciones para configurar loggers con formatos
consistentes y múltiples destinos (consola, archivos).
"""

import logging
import logging.handlers
import os
import sys
from datetime import datetime

# Importar configuraciones de logger
from config import (
    LOG_LEVEL, LOG_DIR, 
    LOG_TO_FILE, LOG_TO_CONSOLE, 
    LOG_MAX_BYTES, LOG_BACKUP_COUNT
)

def setup_logging():
    """
    Configura el sistema de logging para la aplicación.
    
    Establece un formateador estándar y añade handlers para la consola
    y archivos (general y de errores) basado en la configuración importada.
    Evita añadir múltiples handlers si el logger ya ha sido configurado.
    """
    logger = logging.getLogger()
    
    # Evitar múltiples handlers si se llama varias veces
    if logger.hasHandlers():
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
    
    # Determinar el nivel de log basado en la configuración
    level_name = LOG_LEVEL.upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    
    # Crear formateador con timestamp, nivel, módulo y mensaje
    log_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - [%(module)s:%(funcName)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Configurar salida a consola si está habilitada
    if LOG_TO_CONSOLE:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(log_formatter)
        logger.addHandler(console_handler)
    
    # Configurar salida a archivos si está habilitada
    if LOG_TO_FILE:
        # Crear directorio de logs si no existe
        if not os.path.exists(LOG_DIR):
            try:
                os.makedirs(LOG_DIR)
            except OSError as e:
                print(f"Error creando directorio de logs {LOG_DIR}: {e}")
                return
        
        try:
            # Log general
            general_log_path = os.path.join(LOG_DIR, 'minka_server.log')
            file_handler = logging.handlers.RotatingFileHandler(
                general_log_path,
                maxBytes=LOG_MAX_BYTES, 
                backupCount=LOG_BACKUP_COUNT,
                encoding='utf-8'
            )
            file_handler.setFormatter(log_formatter)
            logger.addHandler(file_handler)
            
            # Log de errores (solo nivel ERROR o superior)
            error_log_path = os.path.join(LOG_DIR, 'minka_server_error.log')
            error_file_handler = logging.handlers.RotatingFileHandler(
                error_log_path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding='utf-8'
            )
            error_file_handler.setFormatter(log_formatter)
            error_file_handler.setLevel(logging.ERROR)
            logger.addHandler(error_file_handler)
            
            # Log para cada día
            today = datetime.now().strftime('%Y-%m-%d')
            daily_log_path = os.path.join(LOG_DIR, f'minka_server_{today}.log')
            daily_handler = logging.FileHandler(
                daily_log_path,
                encoding='utf-8'
            )
            daily_handler.setFormatter(log_formatter)
            logger.addHandler(daily_handler)
            
            logging.info(f"Logs configurados en: {LOG_DIR}")
        except Exception as e:
            print(f"Error configurando handlers de archivo para logs: {e}")
            if LOG_TO_CONSOLE:
                logging.error(f"No se pudieron configurar los handlers de archivo: {e}", exc_info=True)
    
    logging.info(f"Sistema de logging inicializado con nivel {level_name}")
    return logger

# También podemos exponer una función para obtener loggers específicos para módulos
def get_module_logger(module_name: str) -> logging.Logger:
    """Return a logger for a specific module."""
    return logging.getLogger(module_name)


def get_app_logger(component: str = "server") -> logging.Logger:
    """Return a logger for a high level application component."""
    return get_module_logger(f"minka.{component}")


def get_test_logger(test_name: str = "test") -> logging.Logger:
    """Return a logger for test runs."""
    return get_module_logger(f"minka.test.{test_name}")


def get_client_logger(client_type: str, client_id: str) -> logging.Logger:
    """Return a logger for a specific client."""
    return get_module_logger(f"minka.client.{client_type}.{client_id}")

if __name__ == '__main__':
    # Ejemplo de cómo usarlo y probarlo
    # Esto normalmente se llamaría una vez al inicio de la aplicación.
    
    # Para probar, simular que config.py tiene estos valores:
    class MockConfig:
        LOG_LEVEL = "DEBUG"
        LOG_DIR = "test_logs"
        LOG_TO_FILE = True
        LOG_TO_CONSOLE = True
        LOG_MAX_BYTES = 1024 * 1024 # 1MB
        LOG_BACKUP_COUNT = 3

    # "Monkey patch" temporal de los valores de config para la prueba
    # En una app real, estos vendrían de tu archivo config.py
    import sys
    mock_config_module = MockConfig()
    # Esto es solo para el bloque if __name__ == '__main__':
    # No afectará a los imports normales de 'config' en otros módulos.
    original_config = sys.modules.get('config')
    sys.modules['config'] = mock_config_module
    
    # Re-importar las variables específicas para que tomen los valores mock
    from config import LOG_LEVEL, LOG_DIR, LOG_TO_FILE, LOG_TO_CONSOLE, LOG_MAX_BYTES, LOG_BACKUP_COUNT

    if os.path.exists(LOG_DIR):
        import shutil
        shutil.rmtree(LOG_DIR) # Limpiar directorio de logs de prueba

    setup_logging()
    
    logging.debug("Este es un mensaje de debug.")
    logging.info("Este es un mensaje de info.")
    logging.warning("Este es un mensaje de warning.")
    logging.error("Este es un mensaje de error.")
    logging.critical("Este es un mensaje crítico.")

    print(f"Logs de prueba deberían estar en consola y en el directorio '{LOG_DIR}'.")

    # Restaurar el módulo config original si existía
    if original_config:
        sys.modules['config'] = original_config
    else:
        del sys.modules['config']