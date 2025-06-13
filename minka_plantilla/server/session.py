import asyncio # Importar asyncio
import redis.asyncio as aioredis # Usar redis-py asyncio
import redis.exceptions as redis # Para excepciones
from datetime import datetime, timedelta, timezone
import uuid
import json
import logging
import time

# Importar funciones JWT desde utils.py
from utils import crear_token_jwt, verificar_token_jwt_async

# Importar configuraciones de config.py
from config import (
    REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASSWORD,
    SESSION_KEY_PREFIX as CFG_SESSION_KEY_PREFIX,
    CLIENT_KEY_PREFIX as CFG_CLIENT_KEY_PREFIX,
    JWT_BLACKLIST_KEY_PREFIX as CFG_JWT_JTI_BLACKLIST_PREFIX,
    MESSAGE_QUEUE_KEY_PREFIX as CFG_MESSAGE_QUEUE_KEY_PREFIX,
    SESSION_TIMEOUT as CFG_SESSION_TIMEOUT,
    RECONNECT_GRACE_PERIOD as CFG_RECONNECT_GRACE_PERIOD,
    DOZE_TIMEOUT as CFG_DOZE_TIMEOUT,
    WEB_RECONNECT_TIMEOUT as CFG_WEB_RECONNECT_TIMEOUT,
    MESSAGE_TTL_SECONDS as CFG_MESSAGE_TTL_SECONDS,
    JWT_SECRET_KEY as CFG_JWT_SECRET_KEY,
    JWT_ALGORITHM as CFG_JWT_ALGORITHM,
    JWT_EXPIRATION_DELTA_SECONDS as CFG_JWT_EXPIRATION_DELTA_SECONDS,
    MAX_QUEUE_LENGTH # Añadido para usarlo en add_message_to_queue
)

# Variable global para el cliente Redis asíncrono
redis_client = None

async def init_redis_client():
    """Inicializa la conexión global del cliente Redis asíncrono."""
    global redis_client
    if redis_client is None:
        redis_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
        if REDIS_PASSWORD:
            redis_url = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
        
        logging.info(f"[REDIS_INIT] Conectando a Redis en {REDIS_HOST}:{REDIS_PORT}, DB: {REDIS_DB}")
        try:
            redis_client = await aioredis.from_url(redis_url, decode_responses=True)
            await redis_client.ping()
            logging.info("[REDIS_INIT] Conexión a Redis establecida y verificada (ping exitoso).")
        except (redis.RedisError, ConnectionRefusedError) as e: # Capturar ConnectionRefusedError también
            logging.error(f"[REDIS_INIT] No se pudo conectar a Redis: {e}")
            redis_client = None # Asegurar que redis_client es None si falla la conexión
            raise # Relanzar para que el llamador maneje la falla de conexión crítica
    return redis_client

# Prefijos para keys de redis (usando los de config.py)
SESSION_KEY_PREFIX = CFG_SESSION_KEY_PREFIX
CLIENT_KEY_PREFIX = CFG_CLIENT_KEY_PREFIX
JWT_JTI_BLACKLIST_PREFIX = CFG_JWT_JTI_BLACKLIST_PREFIX
MESSAGE_QUEUE_KEY_PREFIX = CFG_MESSAGE_QUEUE_KEY_PREFIX

# Tiempos y TTLs (usando los de config.py)
SESSION_TIMEOUT = CFG_SESSION_TIMEOUT
RECONNECT_GRACE_PERIOD = CFG_RECONNECT_GRACE_PERIOD
DOZE_TIMEOUT = CFG_DOZE_TIMEOUT
WEB_RECONNECT_TIMEOUT = CFG_WEB_RECONNECT_TIMEOUT
MESSAGE_TTL_SECONDS = CFG_MESSAGE_TTL_SECONDS

# JWT Config (usando los de config.py)
JWT_SECRET_KEY = CFG_JWT_SECRET_KEY
JWT_ALGORITHM = CFG_JWT_ALGORITHM
JWT_EXPIRATION_DELTA_SECONDS = CFG_JWT_EXPIRATION_DELTA_SECONDS

# active_websockets se maneja en `handlers.websocket_handler`, no aquí.

# Funciones para obtener/establecer sesiones
async def get_session(room_id):
    rc = await init_redis_client() # Asegurar que el cliente esté inicializado
    if not rc: return None # No se pudo conectar a Redis
    if not room_id:
        return None
    session_json = await rc.get(f"{SESSION_KEY_PREFIX}{room_id}")
    if session_json:
        try:
            return json.loads(session_json)
        except json.JSONDecodeError:
            logging.error(f"Error al decodificar JSON para sesión {room_id}")
            return None
    return None

async def set_session(room_id, data):
    rc = await init_redis_client()
    if not rc: return False
    if not room_id:
        return False
    try:
        return await rc.set(f"{SESSION_KEY_PREFIX}{room_id}", json.dumps(data))
    except redis.RedisError as e:
        logging.error(f"Error de Redis en set_session para {room_id}: {e}")
        return False

async def delete_session(room_id):
    rc = await init_redis_client()
    if not rc: return False
    if not room_id:
        return False
    try:
        return await rc.delete(f"{SESSION_KEY_PREFIX}{room_id}") > 0
    except redis.RedisError as e:
        logging.error(f"Error de Redis en delete_session para {room_id}: {e}")
        return False

async def get_all_session_keys():
    rc = await init_redis_client()
    if not rc: return []
    try:
        keys = await rc.keys(f"{SESSION_KEY_PREFIX}*")
        # Extraer la parte del ID de la clave completa
        return [key.replace(SESSION_KEY_PREFIX, "", 1) for key in keys]
    except redis.RedisError as e:
        logging.error(f"Error de Redis en get_all_session_keys: {e}")
        return []

# Funciones para obtener/establecer clientes
async def get_client(client_id):
    rc = await init_redis_client()
    if not rc: return None
    if not client_id:
        return None
    client_json = await rc.get(f"{CLIENT_KEY_PREFIX}{client_id}")
    if client_json:
        try:
            return json.loads(client_json)
        except json.JSONDecodeError:
            logging.error(f"Error al decodificar JSON para cliente {client_id}")
            return None
    return None

async def set_client(client_id, data):
    rc = await init_redis_client()
    if not rc: return False
    if not client_id:
        return False
    try:
        return await rc.set(f"{CLIENT_KEY_PREFIX}{client_id}", json.dumps(data))
    except redis.RedisError as e:
        logging.error(f"Error de Redis en set_client para {client_id}: {e}")
        return False

async def delete_client(client_id, keep_message_queue=False):
    rc = await init_redis_client()
    if not rc: return False
    if not client_id:
        return False
    try:
        result = await rc.delete(f"{CLIENT_KEY_PREFIX}{client_id}") > 0
        if not keep_message_queue:
            await rc.delete(f"{MESSAGE_QUEUE_KEY_PREFIX}{client_id}")
        return result
    except redis.RedisError as e:
        logging.error(f"Error de Redis en delete_client para {client_id}: {e}")
        return False

async def get_all_client_keys():
    rc = await init_redis_client()
    if not rc: return []
    try:
        keys = await rc.keys(f"{CLIENT_KEY_PREFIX}*")
        return [key.replace(CLIENT_KEY_PREFIX, "", 1) for key in keys]
    except redis.RedisError as e:
        logging.error(f"Error de Redis en get_all_client_keys: {e}")
        return []

# Gestión de JTIs para JWT
async def add_jti_to_blacklist(jti, ttl_seconds):
    rc = await init_redis_client()
    if not rc: return False
    if not jti:
        return False
    try:
        return await rc.setex(f"{JWT_JTI_BLACKLIST_PREFIX}{jti}", ttl_seconds, "1")
    except redis.RedisError as e:
        logging.error(f"Error de Redis en add_jti_to_blacklist para JTI {jti}: {e}")
        return False

async def is_jti_blacklisted(jti):
    rc = await init_redis_client()
    if not rc: return True # Si Redis no está, considerar inválido por seguridad
    if not jti:
        return True 
    try:
        return await rc.exists(f"{JWT_JTI_BLACKLIST_PREFIX}{jti}") == 1
    except redis.RedisError as e:
        logging.error(f"Error de Redis en is_jti_blacklisted para JTI {jti}: {e}")
        return True # Fallar seguro

async def is_valid_jti(jti):
    rc = await init_redis_client()
    if not rc: return False # Si Redis no está, considerar inválido
    return jti and not await is_jti_blacklisted(jti)

# Funciones JWT - Ahora usando las de utils.py
def generate_jwt(client_id, room_id):
    """
    Crea un token JWT para un cliente y sala específicos.
    Esta función es un wrapper de crear_token_jwt de utils.py para mantener compatibilidad.
    """
    return crear_token_jwt(client_id, room_id)

async def verify_jwt(token):
    """
    Verifica un token JWT y su JTI.
    Esta función es un wrapper de verificar_token_jwt_async de utils.py para mantener compatibilidad.
    """
    if not token:
        return None
    
    async def check_jti_blacklisted(jti):
        return await is_jti_blacklisted(jti)
    
    return await verificar_token_jwt_async(token, check_jti_blacklisted)

# Funciones para manejar la cola de mensajes
async def add_message_to_queue(client_id, message):
    rc = await init_redis_client()
    if not rc: return False
    if not client_id or not message:
        return False

    client_data = await get_client(client_id)
    if not client_data:
        # Crear un registro básico del cliente si no existe
        is_mobile = not client_id.startswith('web-')
        logging.info(f"[QUEUE] Creando registro temporal para cliente {client_id} (móvil: {is_mobile}) para encolar mensaje")
        client_data = {
            'client_id': client_id,
            'status': 'pending',
            'created_at': time.time(),
            'is_web': not is_mobile,
            'is_mobile': is_mobile
        }
        await set_client(client_id, client_data)
    
    is_dozing = client_data.get('status') == 'dozing'
    is_mobile = client_data.get('is_mobile', not client_id.startswith('web-'))

    message_id = f"{time.time()}-{uuid.uuid4().hex[:8]}"
    message_with_meta = {
        'id': message_id,
        'data': message,
        'timestamp': time.time(),
        'attempts': 0,
        'sender_id': message.get('sender_id', 'unknown') if isinstance(message, dict) else 'unknown',
        'is_doze_message': is_dozing,
        'for_mobile': is_mobile
    }

    try:
        message_json = json.dumps(message_with_meta)
    except Exception as e:
        logging.error(f"Error al serializar mensaje para {client_id}: {e}")
        return False
        
    queue_key = f"{MESSAGE_QUEUE_KEY_PREFIX}{client_id}"
    
    try:
        current_length = await rc.llen(queue_key)
        if current_length >= MAX_QUEUE_LENGTH:
            logging.warning(f"Cola para {client_id} llena ({MAX_QUEUE_LENGTH}). No se añadió {message_id}.")
            return False

        result = await rc.rpush(queue_key, message_json)
        
        current_ttl = await rc.ttl(queue_key)
        if current_ttl == -1:
             await rc.expire(queue_key, MESSAGE_TTL_SECONDS)
            
        logging.info(f"Mensaje encolado para {client_id} (móvil: {is_mobile}, dozing: {is_dozing}). Cola: {await rc.llen(queue_key)}. TTL: {MESSAGE_TTL_SECONDS}s.")
        return result > 0
    except redis.RedisError as e:
        logging.error(f"Error de Redis en add_message_to_queue para {client_id}: {e}")
        return False

async def get_pending_messages(client_id, delete_queue=True):
    rc = await init_redis_client()
    if not rc: return []
    if not client_id:
        return []

    queue_key = f"{MESSAGE_QUEUE_KEY_PREFIX}{client_id}"
    try:
        if not await rc.exists(queue_key):
            return []
            
        queue_length = await rc.llen(queue_key)
        if queue_length == 0:
            return []
        logging.info(f"Recuperando {queue_length} mensajes pendientes para {client_id}")
        
        messages_json = await rc.lrange(queue_key, 0, -1)
        
        messages = []
        deserialize_errors = 0
        
        for msg_json in messages_json:
            try:
                msg = json.loads(msg_json)
                if 'data' in msg:
                    messages.append(msg['data'])
                else:
                    messages.append(msg)
            except json.JSONDecodeError:
                deserialize_errors += 1
                logging.error(f"Error al deserializar mensaje de la cola para {client_id}")
        
        if delete_queue and deserialize_errors == 0:
            await rc.delete(queue_key)
            logging.info(f"Cola de mensajes eliminada para {client_id} después de recuperar {len(messages)} mensajes")
        elif deserialize_errors > 0:
            logging.warning(f"No se eliminó la cola para {client_id} debido a {deserialize_errors} errores de deserialización")
        elif not delete_queue:
            logging.info(f"Conservando cola de mensajes para {client_id} con {len(messages)} mensajes")
            await rc.expire(queue_key, MESSAGE_TTL_SECONDS)
        
        return messages
    except redis.RedisError as e:
        logging.error(f"Error de Redis en get_pending_messages para {client_id}: {e}")
        return []

async def close_redis_client():
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None
        logging.info("[REDIS_CLOSE] Conexión a Redis cerrada.")
