#!/usr/bin/env python3
import redis
import ssl  # Necesario para configurar la verificación de certificados

def cleanup_sessions():
    # Conexión a Upstash Redis ignorando la verificación del certificado
    redis_url = "rediss://default:AWyrAAIjcDFiOTZiMmM0ODYxNjg0NDkyYWNmYWQ5OTE3ZGYzYTg5NXAxMA@vocal-thrush-27819.upstash.io:6379"
    
    # Configuración para ignorar la verificación del certificado
    redis_client = redis.from_url(
        redis_url,
        decode_responses=True,
        ssl_cert_reqs=ssl.CERT_NONE  # Esto deshabilita la verificación del certificado
    )
    
    # Confirmar la acción
    confirm = input("¿Estás seguro de que quieres borrar TODOS los datos de Redis? (sí/no): ")
    if confirm.lower() not in ['sí', 'si', 'yes', 'y']:
        print("Operación cancelada.")
        return
    
    # Elimina todos los datos
    redis_client.flushdb()
    print("Se han eliminado todas las salas y datos de Redis.")

if __name__ == "__main__":
    cleanup_sessions()