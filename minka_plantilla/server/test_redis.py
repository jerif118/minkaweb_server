import redis

# Cambia la IP por la IP local de tu laptop que tiene el contenedor Redis
r = redis.Redis(host='192.168.1.4', port=6379, decode_responses=True)

try:
    # Comando PING para probar conexión
    response = r.ping()
    if response:
        print("✅ Conexión exitosa a Redis")

        # Prueba de escritura y lectura
        r.set("clave_prueba", "Hola Redis desde Python")
        valor = r.get("clave_prueba")
        print("🟢 Valor leído:", valor)
    else:
        print("❌ No se pudo conectar")

except Exception as e:
    print("❌ Error al conectar:", e)
