#!/usr/bin/env python3
"""
Sistema de logs para Minka Server.

Este módulo proporciona funciones para configurar y obtener loggers para diferentes
componentes del servidor Minka, asegurando un registro consistente y detallado.
"""

import os
import logging
import logging.handlers
from datetime import datetime

# Directorio para almacenar logs
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# Formato común para todos los loggers
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'

# Niveles de log definidos
LOG_LEVELS = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}

def get_logger(name, log_level='INFO', log_to_file=True, log_to_console=True, max_bytes=10485760, backup_count=5):
    """
    Configura y devuelve un logger con el nombre especificado.
    
    Args:
        name (str): Nombre del logger
        log_level (str): Nivel de log (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_to_file (bool): Si True, registra logs en archivo
        log_to_console (bool): Si True, registra logs en consola
        max_bytes (int): Tamaño máximo del archivo de log antes de rotación
        backup_count (int): Número de archivos de backup a mantener
        
    Returns:
        logging.Logger: Logger configurado
    """
    logger = logging.getLogger(name)
    
    # Evitar añadir handlers múltiples veces
    if logger.handlers:
        return logger
        
    # Establecer nivel de log
    logger.setLevel(LOG_LEVELS.get(log_level, logging.INFO))
    
    # Crear formateador
    formatter = logging.Formatter(LOG_FORMAT)
    
    # Añadir handler para consola si se solicita
    if log_to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    # Añadir handler para archivo si se solicita
    if log_to_file:
        today = datetime.now().strftime('%Y-%m-%d')
        
        # Log general
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, f'minka_{today}.log'),
            maxBytes=max_bytes,
            backupCount=backup_count
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
        # Log de errores (nivel ERROR o superior)
        error_handler = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, f'minka_error_{today}.log'),
            maxBytes=max_bytes,
            backupCount=backup_count
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        logger.addHandler(error_handler)
    
    return logger

def get_app_logger(component='server'):
    """
    Obtiene un logger para componentes principales de la aplicación.
    
    Args:
        component (str): Nombre del componente (server, api, session, etc.)
        
    Returns:
        logging.Logger: Logger configurado para el componente
    """
    return get_logger(f'minka.{component}', 'INFO', True, True)

def get_test_logger(test_name='test'):
    """
    Obtiene un logger para pruebas.
    
    Args:
        test_name (str): Nombre del test
        
    Returns:
        logging.Logger: Logger configurado para pruebas
    """
    return get_logger(f'minka.test.{test_name}', 'INFO', True, True)

def get_client_logger(client_type, client_id):
    """
    Obtiene un logger para clientes específicos.
    
    Args:
        client_type (str): Tipo de cliente (web, mobile)
        client_id (str): ID del cliente
        
    Returns:
        logging.Logger: Logger configurado para el cliente
    """
    return get_logger(f'minka.client.{client_type}.{client_id}', 'INFO', True, True)

# Para uso directo en scripts que importan este módulo
app_logger = get_app_logger('main')
