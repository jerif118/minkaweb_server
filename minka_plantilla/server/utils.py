# Utilidades para Minka Server - funciones compartidas entre diferentes componentes

import uuid
import re
import os
import time
import jwt
import logging
import hashlib
import inspect
import secrets
import string
from typing import Union, Optional, Dict, Any

# Importar configuraciones necesarias
from config import (
    JWT_SECRET_KEY, JWT_ALGORITHM, JWT_EXPIRATION_DELTA_SECONDS
)

# ------- GENERACIÓN DE IDENTIFICADORES Y VALIDACIONES -------

def generar_identificador_unico(prefijo="id"):
    """
    Genera un identificador único universal (UUID v4) con un prefijo opcional.

    Args:
        prefijo (str, optional): Un prefijo para el identificador. 
                                 Por defecto es "id".

    Returns:
        str: Un identificador único con el formato 'prefijo-uuid'.
    """
    return f"{prefijo}-{uuid.uuid4().hex}"

def validar_formato_uuid(identificador):
    """
    Valida si un string sigue el formato de un UUID v4 hexadecimal (32 caracteres).

    Args:
        identificador (str): El string a validar.

    Returns:
        bool: True si el formato es válido, False en caso contrario.
    """
    if not isinstance(identificador, str):
        return False
    # UUID v4 sin guiones es una secuencia de 32 caracteres hexadecimales
    patron_uuid = re.compile(r'^[0-9a-fA-F]{32}$')
    return bool(patron_uuid.match(identificador))

def generar_password_sala(longitud=6):
    """
    Genera una contraseña simple para una sala, compuesta de letras mayúsculas y números.

    Args:
        longitud (int, optional): La longitud de la contraseña. Por defecto es 6.

    Returns:
        str: Una contraseña generada aleatoriamente.
    """
    if longitud <= 0:
        raise ValueError("La longitud debe ser un entero positivo")

    caracteres = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(caracteres) for _ in range(longitud))


# ------- FUNCIONES RELACIONADAS CON JWT -------

def crear_token_jwt(client_id, room_id, jti=None):
    """
    Crea un token JWT para un cliente y una sala específicos.

    Args:
        client_id (str): El identificador del cliente.
        room_id (str): El identificador de la sala.
        jti (str, optional): Identificador único para el token. Si no se provee,
                             se genera uno nuevo.

    Returns:
        str: El token JWT codificado.
    """
    payload = {
        'client_id': client_id,
        'room_id': room_id,
        'exp': time.time() + JWT_EXPIRATION_DELTA_SECONDS,
        'iat': time.time(),
        'jti': jti or uuid.uuid4().hex # Identificador único del token
    }
    try:
        token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
        return token
    except Exception as e:
        # En una aplicación real, loguear este error
        print(f"Error al crear JWT: {e}")
        return None

async def verificar_token_jwt_async(token, jti_blacklisted_checker=None):
    """
    Versión asíncrona de verificar_token_jwt que permite usar comprobadores de JTI asíncronos.

    Args:
        token (str): El token JWT a verificar.
        jti_blacklisted_checker (async function, optional): Una función asíncrona que toma un JTI (str)
                                                     y devuelve True si está en la lista negra,
                                                     False en caso contrario.

    Returns:
        dict or None: El payload decodificado si el token es válido y no está en lista negra,
                      None en caso contrario.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        
        # Verificar si el JTI está en la lista negra, si se proporciona un verificador
        if jti_blacklisted_checker:
            jti = payload.get('jti')
            if not jti:
                # Considerar esto un error o una política de seguridad: token sin JTI
                pass  # Por ahora, permitir si no hay JTI y no hay checker
            elif await jti_blacklisted_checker(jti):  # Await aquí para la función asíncrona
                return None  # Token revocado
                
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError as e:
        return None
    except Exception as e:
        logging.error(f"Error inesperado al verificar JWT: {e}")
        return None

def verificar_token_jwt(token, jti_blacklisted_checker=None):
    """
    Verifica la validez de un token JWT.
    
    NOTA: Esta función ahora detecta si el verificador es asíncrono y devuelve
    None directamente. Para soporte asíncrono, use verificar_token_jwt_async.

    Args:
        token (str): El token JWT a verificar.
        jti_blacklisted_checker (function, optional): Una función que toma un JTI (str)
                                                     y devuelve True si está en la lista negra,
                                                     False en caso contrario.

    Returns:
        dict or None: El payload decodificado si el token es válido y no está en lista negra,
                      None en caso contrario.
    """
    # Comprobar si el verificador es asíncrono
    if jti_blacklisted_checker and inspect.iscoroutinefunction(jti_blacklisted_checker):
        logging.warning("verificar_token_jwt recibió un verificador asíncrono pero esta función es síncrona. Use verificar_token_jwt_async en su lugar.")
        return None
        
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        
        # Verificar si el JTI está en la lista negra, si se proporciona un verificador
        if jti_blacklisted_checker:
            jti = payload.get('jti')
            if not jti:
                # Considerar esto un error o una política de seguridad: token sin JTI
                pass  # Por ahora, permitir si no hay JTI y no hay checker
            elif jti_blacklisted_checker(jti):
                return None  # Token revocado
                
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError as e:
        return None
    except Exception as e:
        logging.error(f"Error inesperado al verificar JWT: {e}")
        return None


# ------- FUNCIONES DE SEGURIDAD Y HASH -------

def hashear_password(password, salt=None):
    """
    Genera un hash de una contraseña usando SHA256 y un salt.

    Args:
        password (str): La contraseña a hashear.
        salt (str, optional): Un salt para el hasheo. Si no se provee, se genera uno.

    Returns:
        tuple: (hash_hex, salt_hex) El hash de la contraseña y el salt utilizado, ambos en formato hexadecimal.
    """
    if salt is None:
        salt_bytes = os.urandom(16) # Genera un salt aleatorio de 16 bytes
    else:
        try:
            salt_bytes = bytes.fromhex(salt) # Asume que el salt de entrada es hexadecimal
        except ValueError:
            # Si el salt no es hexadecimal, usarlo directamente como bytes (codificado en UTF-8)
            # Esto es menos ideal que un salt binario aleatorio, pero permite flexibilidad.
            salt_bytes = salt.encode('utf-8')

    # Combinar salt y password
    # Asegurarse de que la contraseña también esté en bytes
    password_bytes = password.encode('utf-8')
    
    # Iteraciones para hacer el hash más resistente (opcional, pero recomendado)
    # Python `hashlib.pbkdf2_hmac` es mejor para esto, pero para mantenerlo simple:
    hashed = hashlib.sha256(salt_bytes + password_bytes).digest()
    # Podrías añadir iteraciones aquí si es necesario, ej. bucle de hash(salt + hash)
    
    return hashed.hex(), salt_bytes.hex()

def verificar_password_hasheado(password_ingresada, hash_guardado_hex, salt_hex):
    """
    Verifica una contraseña ingresada contra un hash y salt guardados.

    Args:
        password_ingresada (str): La contraseña que el usuario ha ingresado.
        hash_guardado_hex (str): El hash de la contraseña guardada (en hexadecimal).
        salt_hex (str): El salt utilizado para generar el hash guardado (en hexadecimal).

    Returns:
        bool: True si la contraseña es correcta, False en caso contrario.
    """
    nuevo_hash_hex, _ = hashear_password(password_ingresada, salt_hex)
    return nuevo_hash_hex == hash_guardado_hex


# Solo ejecutar el código de prueba si este archivo se ejecuta directamente
if __name__ == '__main__':
    print("--- Probando Generadores ---")
    client_id = generar_identificador_unico("cliente")
    room_id = generar_identificador_unico("sala")
    print(f"ID Cliente: {client_id}")
    print(f"ID Sala: {room_id}")
    print(f"Validar formato UUID de room_id (sin prefijo): {validar_formato_uuid(room_id.split('-')[1])}")
    print(f"Validar formato UUID de '12345': {validar_formato_uuid('12345')}")

    password_sala = generar_password_sala()
    print(f"Password Sala (6 chars): {password_sala}")
    password_sala_larga = generar_password_sala(10)
    print(f"Password Sala (10 chars): {password_sala_larga}")

    print("\n--- Probando JWT ---")
    # Simular una función de lista negra de JTI
    jti_blacklist = set()
    def es_jti_revocado(jti):
        return jti in jti_blacklist

    # Crear token
    token_jwt = crear_token_jwt(client_id, room_id)
    if token_jwt:
        print(f"Token JWT generado: {token_jwt[:50]}...")
        
        # Verificar token válido
        payload_verificado = verificar_token_jwt(token_jwt, es_jti_revocado)
        if payload_verificado:
            print(f"Token JWT verificado exitosamente. Payload: {payload_verificado}")
            jti_del_token = payload_verificado.get('jti')
            
            # Simular revocación del token añadiendo su JTI a la lista negra
            if jti_del_token:
                print(f"Añadiendo JTI {jti_del_token} a la lista negra.")
                jti_blacklist.add(jti_del_token)
                payload_revocado = verificar_token_jwt(token_jwt, es_jti_revocado)
                if payload_revocado is None:
                    print("Token JWT ahora es inválido (revocado por JTI). Correcto.")
                else:
                    print("ERROR: Token JWT debería ser inválido tras revocación de JTI.")
        else:
            print("Error al verificar token JWT válido.")
            
        # Verificar token expirado (simulado)
        token_expirado_payload = {
            'client_id': client_id, 'room_id': room_id,
            'exp': time.time() - 3600, # Expiró hace 1 hora
            'iat': time.time() - 3700,
            'jti': uuid.uuid4().hex
        }
        token_expirado = jwt.encode(token_expirado_payload, JWT_SECRET_KEY, JWT_ALGORITHM)
        payload_exp = verificar_token_jwt(token_expirado, es_jti_revocado)
        if payload_exp is None:
            print("Token JWT expirado verificado correctamente (devuelve None).")
        else:
            print("Error: Token JWT expirado no fue detectado.")
    else:
        print("Error al generar token JWT.")

    print("\n--- Probando Hasheo de Contraseñas ---")
    mi_password = "P@$$wOrd123!"
    
    # Generar hash y salt
    hash_hex, salt_hex = hashear_password(mi_password)
    print(f"Password: {mi_password}")
    print(f"Salt (hex): {salt_hex}")
    print(f"Hash (hex): {hash_hex}")
    
    # Verificar contraseña correcta
    if verificar_password_hasheado(mi_password, hash_hex, salt_hex):
        print("Verificación de password correcta: ÉXITO")
    else:
        print("Verificación de password correcta: FALLO")
        
    # Verificar contraseña incorrecta
    if not verificar_password_hasheado("incorrecta", hash_hex, salt_hex):
        print("Verificación de password incorrecta: ÉXITO (devuelve False como se esperaba)")
    else:
        print("Verificación de password incorrecta: FALLO")

    # Prueba con salt proporcionado
    otro_password = "otroPasswordSeguro789"
    salt_fijo = os.urandom(16).hex() # Un salt fijo para la prueba
    hash1, _ = hashear_password(otro_password, salt_fijo)
    hash2, _ = hashear_password(otro_password, salt_fijo)
    if hash1 == hash2:
        print(f"Hashes con salt fijo son consistentes: ÉXITO")
    else:
        print(f"Hashes con salt fijo son inconsistentes: FALLO")
    if verificar_password_hasheado(otro_password, hash1, salt_fijo):
        print(f"Verificación con salt fijo: ÉXITO")
    else:
        print(f"Verificación con salt fijo: FALLO")