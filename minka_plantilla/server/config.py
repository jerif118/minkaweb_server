#!/usr/bin/env python3
"""
Configuración para despliegue en producción del servidor Minka.
"""

import os
import logging

# Obtener configuración del entorno
def get_env_var(var_name, default_value):
    """Obtiene una variable de entorno o devuelve un valor por defecto."""
    return os.environ.get(var_name, default_value)

# Configuración del servidor
SERVER_PORT = int(get_env_var('MINKA_PORT', '5001'))
SERVER_HOST = get_env_var('MINKA_HOST', 'localhost')
ENVIRONMENT = get_env_var('MINKA_ENV', 'development')  # 'development' o 'production'

# Tiempo de espera y timeouts
SESSION_TIMEOUT = int(get_env_var('MINKA_SESSION_TIMEOUT', str(3 * 24 * 60 * 60)))  # 3 días por defecto
RECONNECT_GRACE_PERIOD = int(get_env_var('MINKA_RECONNECT_GRACE_PERIOD', '60'))  # 1 minuto por defecto
WEB_RECONNECT_TIMEOUT = int(get_env_var('MINKA_WEB_RECONNECT_TIMEOUT', str(3 * 24 * 60 * 60)))  # 3 días por defecto
DOZE_TIMEOUT = int(get_env_var('MINKA_DOZE_TIMEOUT', str(24 * 60 * 60)))  # 24 horas por defecto
MESSAGE_TTL_SECONDS = int(get_env_var('MINKA_MESSAGE_TTL', str(7 * 24 * 60 * 60)))  # 7 días por defecto
MAX_QUEUE_LENGTH = int(get_env_var('MINKA_MAX_QUEUE_LENGTH', '100'))  # Máximo de mensajes por cola

# Configuración de Redis
REDIS_HOST = get_env_var('REDIS_HOST', 'localhost')
REDIS_PORT = int(get_env_var('REDIS_PORT', '6379'))
REDIS_DB = int(get_env_var('REDIS_DB', '0'))
REDIS_PASSWORD = get_env_var('REDIS_PASSWORD', None)

# Configuración de JWT
JWT_SECRET_KEY = get_env_var('MINKA_JWT_SECRET', 'minkaweb_secret_key')  # ¡Cambiar en producción!
JWT_ALGORITHM = get_env_var('MINKA_JWT_ALGORITHM', 'HS256')
JWT_EXPIRATION_DELTA_SECONDS = int(get_env_var('MINKA_JWT_EXPIRATION', str(WEB_RECONNECT_TIMEOUT)))

# Configuración de logs
LOG_LEVEL = get_env_var('MINKA_LOG_LEVEL', 'INFO')
LOG_TO_FILE = get_env_var('MINKA_LOG_TO_FILE', 'true').lower() == 'true'
LOG_TO_CONSOLE = get_env_var('MINKA_LOG_TO_CONSOLE', 'true').lower() == 'true'
LOG_DIR = get_env_var('MINKA_LOG_DIR', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs'))
LOG_MAX_BYTES = int(get_env_var('MINKA_LOG_MAX_BYTES', str(10 * 1024 * 1024)))  # 10 MB por defecto
LOG_BACKUP_COUNT = int(get_env_var('MINKA_LOG_BACKUP_COUNT', '5'))

# Prefijos para Redis
SESSION_KEY_PREFIX = get_env_var('MINKA_SESSION_PREFIX', 'session:')
CLIENT_KEY_PREFIX = get_env_var('MINKA_CLIENT_PREFIX', 'client:')
MESSAGE_QUEUE_KEY_PREFIX = get_env_var('MINKA_MQ_PREFIX', 'mq:')
JWT_BLACKLIST_KEY_PREFIX = get_env_var('MINKA_JWT_BLACKLIST_PREFIX', 'jwt_blacklist:')

# Función para mostrar la configuración actual
def print_config():
    """Imprime la configuración actual para diagnóstico."""
    config_vars = {
        "SERVER_PORT": SERVER_PORT,
        "SERVER_HOST": SERVER_HOST,
        "ENVIRONMENT": ENVIRONMENT,
        "SESSION_TIMEOUT": SESSION_TIMEOUT,
        "RECONNECT_GRACE_PERIOD": RECONNECT_GRACE_PERIOD,
        "WEB_RECONNECT_TIMEOUT": WEB_RECONNECT_TIMEOUT,
        "DOZE_TIMEOUT": DOZE_TIMEOUT,
        "MESSAGE_TTL_SECONDS": MESSAGE_TTL_SECONDS,
        "MAX_QUEUE_LENGTH": MAX_QUEUE_LENGTH,
        "REDIS_HOST": REDIS_HOST,
        "REDIS_PORT": REDIS_PORT,
        "REDIS_DB": REDIS_DB,
        "JWT_ALGORITHM": JWT_ALGORITHM,
        "JWT_EXPIRATION_DELTA_SECONDS": JWT_EXPIRATION_DELTA_SECONDS,
        "LOG_LEVEL": LOG_LEVEL,
        "LOG_TO_FILE": LOG_TO_FILE,
        "LOG_TO_CONSOLE": LOG_TO_CONSOLE,
        "LOG_DIR": LOG_DIR,
    }
    
    print("\n=== CONFIGURACIÓN DEL SERVIDOR MINKA ===")
    print(f"ENTORNO: {ENVIRONMENT}")
    for key, value in config_vars.items():
        if 'PASSWORD' not in key and 'SECRET' not in key:  # No mostrar información sensible
            print(f"{key}: {value}")
    print("======================================\n")

if __name__ == "__main__":
    print_config()
