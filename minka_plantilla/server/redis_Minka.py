import redis

try:
    redis_client = redis.Redis(
        host='192.168.1.4',
        port=6379,
        db=0,
        decode_responses=True
    )
except redis.ConnectionError as e:
    print(f"Error de conexión a Redis: {e}")