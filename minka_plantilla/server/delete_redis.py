import redis

# Conexión a Redis
r = redis.Redis(host='localhost', port=6379, db=0)

# Borrar todas las claves de la base de datos actual
r.flushall()

print("✅ Base de datos actual de Redis borrada.")