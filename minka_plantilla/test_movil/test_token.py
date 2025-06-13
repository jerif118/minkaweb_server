import asyncio
import websockets
import json
import uuid
import logging
import time
import sys
import random
import re
import os

# Asegurar acceso a los utilitarios de logging del servidor
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))
from logger_config import setup_logging, get_module_logger

# Configurar el logger de forma consistente con el servidor
setup_logging()
logger = get_module_logger("minka_test")

# URL del servidor WebSocket
SERVER_URL = "ws://localhost:5001/ws"  # Ajustar según corresponda

# Clase para representar un cliente (web o movil)
class MinkaClient:
    def __init__(self, client_type, client_id=None):
        """
        Inicializa un cliente Minka.
        client_type: 'web' o 'mobile'
        client_id: opcional, si no se proporciona se genera automáticamente
        """
        self.client_type = client_type
        self.client_id = client_id or f"{client_type}-{uuid.uuid4().hex[:8]}"
        self.websocket = None
        self.room_id = None
        self.room_password = None
        self.jwt_token = None
        self.connected = False
        self.messages_received = []
        self.paired_client_id = None
    
    async def create_room(self):
        """Crea una sala nueva como cliente web"""
        if self.client_type != "web":
            logger.error("Solo los clientes web pueden crear salas")
            return False
        
        logger.info(f"Cliente {self.client_id} creando sala...")
        try:
            url = f"{SERVER_URL}?client_id={self.client_id}&action=create"
            self.websocket = await websockets.connect(url)
            self.connected = True
            
            # Esperar la respuesta con room_id y password
            response = await self.websocket.recv()
            data = json.loads(response)
            
            if 'room_id' in data and 'password' in data:
                self.room_id = data['room_id']
                self.room_password = data['password']
                logger.info(f"Sala creada: {self.room_id}, Contraseña: {self.room_password}")
                return True
            else:
                if 'error' in data:
                    logger.error(f"Error al crear sala: {data['error']}")
                return False
                
        except Exception as e:
            logger.error(f"Error al crear sala: {e}")
            self.connected = False
            return False
    
    async def join_room(self, room_id, room_password):
        """Se une a una sala existente"""
        logger.info(f"Cliente {self.client_id} uniendose a sala {room_id}...")
        try:
            url = f"{SERVER_URL}?client_id={self.client_id}&action=join&room_id={room_id}&room_password={room_password}"
            self.websocket = await websockets.connect(url)
            self.connected = True
            self.room_id = room_id
            self.room_password = room_password
            
            # Esperar la respuesta con el token JWT
            response = await self.websocket.recv()
            data = json.loads(response)
            
            if 'jwt_token' in data:
                self.jwt_token = data['jwt_token']
                token_preview = self.jwt_token[:20] + "..." if self.jwt_token else "None"
                logger.info(f"Cliente {self.client_id} obtuvo token JWT: {token_preview}")
                return True
            else:
                if 'error' in data:
                    logger.error(f"Error al unirse a sala: {data['error']}")
                return False
                
        except Exception as e:
            logger.error(f"Error al unirse a sala: {e}")
            self.connected = False
            return False
    
    async def reconnect_with_token(self, use_invalid_token=False):
        """Reconecta usando el token JWT"""
        if not self.jwt_token and not use_invalid_token:
            logger.error(f"Cliente {self.client_id} no tiene token JWT para reconectar")
            return False
        
        token = "invalid_token_123" if use_invalid_token else self.jwt_token
        token_preview = token[:20] + "..." if token else "None"
        
        try:
            if self.websocket and self.connected:
                await self.websocket.close()
                self.connected = False
            
            logger.info(f"Cliente {self.client_id} reconectando con token: {token_preview}")
            url = f"{SERVER_URL}?client_id={self.client_id}&jwt_token={token}"
            self.websocket = await websockets.connect(url)
            
            # Esperar respuesta
            response = await self.websocket.recv()
            data = json.loads(response)
            
            if 'event' in data and data['event'] == 'reconnected':
                logger.info(f"Cliente {self.client_id} reconectado exitosamente")
                self.connected = True
                return True
            else:
                if 'error' in data:
                    logger.error(f"Error al reconectar: {data['error']}")
                return False
        
        except Exception as e:
            logger.error(f"Error al reconectar: {e}")
            self.connected = False
            return False
    
    async def reconnect_without_token(self):
        """Intenta reconectar sin token (deberia fallar)"""
        try:
            if self.websocket and self.connected:
                await self.websocket.close()
                self.connected = False
            
            logger.info(f"Cliente {self.client_id} intentando reconectar SIN token...")
            url = f"{SERVER_URL}?client_id={self.client_id}"
            self.websocket = await websockets.connect(url)
            
            # Esperar respuesta (deberia ser un error)
            response = await self.websocket.recv()
            data = json.loads(response)
            logger.info(f"Respuesta a reconexion sin token: {data}")
            
            if 'error' in data:
                logger.info(f"Servidor rechazo reconexion sin token como esperado: {data['error']}")
                return False
            else:
                logger.warning("Servidor permitio reconexion sin token (inesperado)")
                self.connected = True
                return True
        
        except Exception as e:
            logger.error(f"Error al intentar reconectar sin token: {e}")
            self.connected = False
            return False
    
    async def send_message(self, message_content):
        """Envia un mensaje a la sala"""
        if not self.connected or not self.websocket:
            logger.error(f"Cliente {self.client_id} no está conectado")
            return False
        
        try:
            message = {"message": message_content}
            message_json = json.dumps(message)
            logger.debug(f"Enviando: {message_json}")
            await self.websocket.send(message_json)
            logger.info(f"Cliente {self.client_id} envio mensaje: {message_content}")
            return True
        except Exception as e:
            logger.error(f"Error al enviar mensaje: {e}")
            self.connected = False
            return False
    
    async def clear_delivery_confirmations(self, timeout=0.5):
        """Limpia los mensajes de confirmacion de entrega pendientes"""
        try:
            while True:
                response = await asyncio.wait_for(self.websocket.recv(), timeout)
                data = json.loads(response)
                
                # Registrar todos los mensajes
                self.messages_received.append(data)
                
                # Si es un mensaje de confirmacion de entrega, continuar limpiando
                if 'event' in data and data['event'] in ['message_delivered', 'message_queued']:
                    logger.debug(f"Descartando confirmacion: {data['event']}")
                    continue
                else:
                    # Si es un mensaje real, devolverlo
                    logger.debug(f"Encontrado mensaje no-confirmacion: {data}")
                    return data
        except asyncio.TimeoutError:
            # No hay más mensajes, lo cual es esperado
            logger.debug("No hay más mensajes de confirmacion pendientes")
            return None
        except Exception as e:
            logger.error(f"Error al limpiar confirmaciones: {e}")
            return None
    
    async def receive_message(self, timeout=5, ignore_confirmations=True):
        """
        Recibe un mensaje, con timeout.
        Si ignore_confirmations=True, ignora los mensajes de confirmacion de entrega.
        """
        if not self.connected or not self.websocket:
            logger.error(f"Cliente {self.client_id} no está conectado")
            return None
        
        try:
            # Primero limpiar cualquier confirmacion pendiente
            if ignore_confirmations:
                pre_message = await self.clear_delivery_confirmations(timeout=0.5)
                if pre_message and ('message' in pre_message or not pre_message.get('event') in ['message_delivered', 'message_queued']):
                    logger.info(f"Cliente {self.client_id} recibio: {pre_message}")
                    return pre_message
            
            # Esperar un mensaje real
            start_time = time.time()
            remaining_timeout = max(0.1, timeout - (time.time() - start_time))
            
            while remaining_timeout > 0:
                response = await asyncio.wait_for(self.websocket.recv(), remaining_timeout)
                data = json.loads(response)
                logger.info(f"Cliente {self.client_id} recibio: {data}")
                self.messages_received.append(data)
                
                # Si estamos ignorando confirmaciones y esto es una confirmacion, continuar esperando
                if ignore_confirmations and 'event' in data and data['event'] in ['message_delivered', 'message_queued']:
                    logger.debug(f"Ignorando confirmacion de entrega: {data['event']}")
                    remaining_timeout = max(0.1, timeout - (time.time() - start_time))
                    continue
                
                return data
                
            logger.warning(f"Timeout esperando mensaje para {self.client_id}")
            return None  # Timeout sin recibir mensaje válido
            
        except asyncio.TimeoutError:
            logger.warning(f"Timeout esperando mensaje para {self.client_id}")
            return None
        except Exception as e:
            logger.error(f"Error al recibir mensaje: {e}")
            self.connected = False
            return None
    
    async def close(self):
        """Cierra la conexion websocket"""
        if self.websocket:
            try:
                await self.websocket.close()
                logger.info(f"Cliente {self.client_id} cerro conexion")
            except:
                pass
        self.connected = False

# NUEVO: Clase para gestionar los resultados de las pruebas
class TestResults:
    def __init__(self):
        self.total_tests = 0
        self.passed_tests = 0
        self.failed_tests = []
    
    def add_success(self, test_name):
        self.total_tests += 1
        self.passed_tests += 1
        logger.info(f"✅ PRUEBA EXITOSA: {test_name}")
    
    def add_failure(self, test_name, error_message):
        self.total_tests += 1
        self.failed_tests.append((test_name, error_message))
        logger.error(f"❌ PRUEBA FALLIDA: {test_name} - {error_message}")
    
    def print_summary(self):
        success_rate = (self.passed_tests / self.total_tests) * 100 if self.total_tests > 0 else 0
        
        logger.info("\n==================================")
        logger.info(f"RESUMEN DE PRUEBAS: {self.passed_tests}/{self.total_tests} exitosas ({success_rate:.1f}%)")
        
        if self.failed_tests:
            logger.info("\nPruebas fallidas:")
            for i, (test_name, error) in enumerate(self.failed_tests, 1):
                logger.info(f"{i}. {test_name}: {error}")
        
        logger.info("==================================")
        
        return self.passed_tests == self.total_tests

# Funcion auxiliar para imprimir divisores en el log para mejor legibilidad
def log_section(title):
    logger.info("\n=== " + title + " ===")

async def run_test():
    """Ejecuta el flujo de prueba completo"""
    results = TestResults()
    web_client = None
    mobile_client = None
    
    try:
        # 1. Crear cliente web
        log_section("INICIANDO PRUEBA - CREACIoN DE SALA")
        web_client = MinkaClient("web")
        success = await web_client.create_room()
        if not success:
            results.add_failure("Creacion de sala", "No se pudo crear sala")
            return results
        
        # Dormir para simular tiempo real
        await asyncio.sleep(1)
        
        # 2. Crear cliente movil y unirlo a la sala
        log_section("UNIoN DE CLIENTE MoVIL A LA SALA")
        mobile_client = MinkaClient("mobile")
        success = await mobile_client.join_room(web_client.room_id, web_client.room_password)
        if not success:
            results.add_failure("Union a sala", "Movil no pudo unirse a la sala")
            return results
        
        # 3. El web client deberia recibir un token JWT cuando el movil se une
        logger.info("Esperando token JWT para cliente web...")
        jwt_notification = await web_client.receive_message(timeout=5)
        if not jwt_notification or not jwt_notification.get('jwt_token'):
            results.add_failure("Recepcion de JWT", "Web client no recibio token JWT")
            return results
        
        web_client.jwt_token = jwt_notification.get('jwt_token')
        token_preview = web_client.jwt_token[:20] + "..." if web_client.jwt_token else "None"
        logger.info(f"Web client recibio token JWT: {token_preview}")
        
        # Procesar todos los eventos pendientes antes de comenzar el intercambio de mensajes
        log_section("PROCESANDO EVENTOS PENDIENTES")
        logger.info("Esperando y procesando eventos pendientes...")
        await asyncio.sleep(2)  # Esperar más tiempo para asegurar que todos los eventos lleguen
        
        # Procesar cualquier mensaje pendiente en el cliente web
        try:
            while True:
                # Esperar con un timeout corto para no bloquear demasiado
                extra_event = await asyncio.wait_for(web_client.websocket.recv(), 0.5)
                extra_data = json.loads(extra_event)
                logger.info(f"Procesando evento adicional: {extra_data}")
                web_client.messages_received.append(extra_data)
                # Si es un evento 'joined', lo registramos especificamente
                if extra_data.get('event') == 'joined':
                    logger.info(f"Recibido evento 'joined' del peer: {extra_data.get('peer_id')}")
        except asyncio.TimeoutError:
            # Timeout esperado cuando no hay más mensajes
            logger.info("No hay más eventos pendientes, continuando con la prueba")
        except Exception as e:
            logger.warning(f"Error al procesar eventos adicionales: {e}")
        
        # 4. Intercambio de mensajes más robusto
        log_section("PRUEBA DE INTERCAMBIO DE MENSAJES INICIAL")
        
        # Serie de mensajes para probar la comunicacion bidireccional
        test_messages = [
            ("web", "Mensaje 1 desde web"),
            ("mobile", "Mensaje 1 desde movil"),
            ("web", "Mensaje 2 desde web"),
            ("mobile", "Mensaje 2 desde movil"),
            ("web", "Mensaje 3 desde web")
        ]
        
        exchange_success = True
        exchange_error = ""
        
        for sender, message in test_messages:
            if sender == "web":
                logger.info(f"Web envia: {message}")
                await web_client.send_message(message)
                
                response = await mobile_client.receive_message(timeout=5)
                if not response or response.get('message') != message:
                    exchange_success = False
                    exchange_error = f"Movil no recibio correctamente el mensaje: {message}"
                    logger.error(exchange_error)
                    break
                logger.info(f"Movil recibio correctamente: {message}")
            else:  # mobile
                logger.info(f"Movil envia: {message}")
                await mobile_client.send_message(message)
                
                # MEJORADO: Implementar logica más robusta para verificar la recepcion correcta del mensaje
                received_correct_message = False
                max_attempts = 3
                
                for attempt in range(max_attempts):
                    logger.info(f"Web esperando mensaje (intento {attempt+1}/{max_attempts})...")
                    response = await web_client.receive_message(timeout=5, ignore_confirmations=True)
                    if not response:
                        logger.error(f"Web no recibio respuesta en el intento {attempt+1}")
                        continue
                    
                    # Verificar si es el mensaje que esperamos
                    if 'message' in response and response.get('message') == message:
                        received_correct_message = True
                        logger.info(f"Web recibio correctamente: {message} (intento {attempt+1})")
                        break
                    else:
                        # Si recibimos otro tipo de evento, lo registramos pero seguimos esperando
                        logger.warning(f"Recibido mensaje inesperado en intento {attempt+1}: {response}")
                        await asyncio.sleep(0.5)
                
                if not received_correct_message:
                    exchange_success = False
                    exchange_error = f"Web no recibio correctamente el mensaje: {message} despues de {max_attempts} intentos"
                    logger.error(exchange_error)
                    break
        
        if exchange_success:
            results.add_success("Intercambio de mensajes inicial")
        else:
            results.add_failure("Intercambio de mensajes inicial", exchange_error)
            # Continuamos con las pruebas aunque esta haya fallado
        
        # 5. Probar reconexion del cliente web con token válido
        log_section("PRUEBA DE RECONEXIoN WEB CON TOKEN VÁLIDO")
        await web_client.close()
        success = await web_client.reconnect_with_token()
        if not success:
            results.add_failure("Reconexion web con token válido", "Reconexion fallo")
        else:
            # Verificar comunicacion despues de reconexion web
            logger.info("Verificando comunicacion despues de reconexion web...")
            
            # Limpiar posibles mensajes pendientes
            await asyncio.sleep(1)
            await web_client.clear_delivery_confirmations()
            await mobile_client.clear_delivery_confirmations()
            
            # Web -> Mobile
            test_message = "Mensaje despues de reconexion web"
            logger.info(f"Web reconectada envia: {test_message}")
            await web_client.send_message(test_message)
            
            response = await mobile_client.receive_message(timeout=5, ignore_confirmations=True)
            
            if response and response.get('message') == test_message:
                logger.info(f"Movil recibio mensaje despues de reconexion web")
                
                # Mobile -> Web
                test_message_back = "Respuesta a web reconectado"
                logger.info(f"Movil responde a web reconectada: {test_message_back}")
                await mobile_client.send_message(test_message_back)
                
                response_back = await web_client.receive_message(timeout=5, ignore_confirmations=True)
                
                if response_back and response_back.get('message') == test_message_back:
                    logger.info(f"Web reconectada recibio respuesta del movil")
                    results.add_success("Comunicacion bidireccional despues de reconexion web")
                else:
                    logger.error(f"Web reconectada no recibio mensaje de movil")
                    results.add_failure("Comunicacion bidireccional despues de reconexion web", 
                                        "Web reconectado no recibio mensaje de movil")
            else:
                logger.error(f"Movil no recibio mensaje despues de reconexion web")
                results.add_failure("Comunicacion despues de reconexion web", 
                                    "Movil no recibio mensaje despues de reconexion web")
        
        # 6. Probar reconexion del cliente movil con token válido
        log_section("PRUEBA DE RECONEXIoN MoVIL CON TOKEN VÁLIDO")
        await mobile_client.close()
        success = await mobile_client.reconnect_with_token()
        if not success:
            results.add_failure("Reconexion movil con token válido", "Reconexion fallo")
        else:
            # Verificar comunicacion despues de reconexion movil
            logger.info("Verificando comunicacion despues de reconexion movil...")
            
            # Limpiar posibles mensajes pendientes
            await asyncio.sleep(1)
            await web_client.clear_delivery_confirmations()
            await mobile_client.clear_delivery_confirmations()
            
            # Mobile -> Web
            test_message = "Mensaje despues de reconexion movil"
            logger.info(f"Movil reconectado envia: {test_message}")
            await mobile_client.send_message(test_message)
            
            response = await web_client.receive_message(timeout=5, ignore_confirmations=True)
            
            if response and response.get('message') == test_message:
                logger.info(f"Web recibio mensaje despues de reconexion movil")
                
                # Web -> Mobile
                test_message_back = "Respuesta a movil reconectado"
                logger.info(f"Web responde a movil reconectado: {test_message_back}")
                await web_client.send_message(test_message_back)
                
                response_back = await mobile_client.receive_message(timeout=5, ignore_confirmations=True)
                
                if response_back and response_back.get('message') == test_message_back:
                    logger.info(f"Movil reconectado recibio respuesta de web")
                    results.add_success("Comunicacion bidireccional despues de reconexion movil")
                else:
                    logger.error(f"Movil reconectado no recibio mensaje de web")
                    results.add_failure("Comunicacion bidireccional despues de reconexion movil", 
                                        "Movil reconectado no recibio mensaje de web")
            else:
                logger.error(f"Web no recibio mensaje despues de reconexion movil")
                results.add_failure("Comunicacion despues de reconexion movil", 
                                    "Web no recibio mensaje despues de reconexion movil")
        
        # 7. Probar reconexion con token inválido
        log_section("PRUEBA DE RECONEXIoN CON TOKEN INVÁLIDO")
        await web_client.close()
        success = await web_client.reconnect_with_token(use_invalid_token=True)
        if success:
            logger.error("La reconexion con token inválido fue aceptada (¡deberia fallar!)")
            results.add_failure("Reconexion con token inválido", 
                                "Reconexion con token inválido fue aceptada (deberia fallar)")
        else:
            logger.info("Reconexion con token inválido rechazada correctamente")
            results.add_success("Reconexion con token inválido")
            
            # Reconectar para continuar
            logger.info("Reconectando web con token válido para continuar pruebas...")
            success = await web_client.reconnect_with_token()
            if not success:
                logger.error("No se pudo reconectar despues de prueba con token inválido")
                results.add_failure("Reconexion despues de prueba con token inválido", 
                                    "No se pudo reconectar despues de prueba con token inválido")
                return results
            else:
                logger.info("Reconexion exitosa despues de prueba con token inválido")
        
        # 8. Probar reconexion sin token
        log_section("PRUEBA DE RECONEXIoN SIN TOKEN")
        await mobile_client.close()
        success = await mobile_client.reconnect_without_token()
        if success:
            logger.error("La reconexion sin token fue aceptada (¡deberia fallar!)")
            results.add_failure("Reconexion sin token", 
                                "Reconexion sin token fue aceptada (deberia fallar)")
        else:
            logger.info("Reconexion sin token rechazada correctamente")
            results.add_success("Reconexion sin token")
            
            # Reconectar para finalizar limpiamente
            logger.info("Reconectando movil con token válido para continuar pruebas...")
            success = await mobile_client.reconnect_with_token()
            if not success:
                logger.error("No se pudo reconectar al final de las pruebas")
                results.add_failure("Reconexion final", 
                                    "No se pudo reconectar al final de las pruebas")
                return results
            else:
                logger.info("Reconexion final exitosa")
        
        # 9. Realizar múltiples reconexiones consecutivas (prueba de estres)
        log_section("PRUEBA DE MÚLTIPLES RECONEXIONES CONSECUTIVAS")
        reconnect_success = True
        reconnect_error = ""
        
        for i in range(3):  # 3 reconexiones consecutivas
            logger.info(f"--- Ronda de reconexion #{i+1} ---")
            
            logger.info(f"Cerrando y reconectando cliente web (ronda {i+1})...")
            await web_client.close()
            success = await web_client.reconnect_with_token()
            if not success:
                reconnect_success = False
                reconnect_error = f"Fallo la reconexion web #{i+1}"
                logger.error(reconnect_error)
                break
            logger.info(f"Reconexion web #{i+1} exitosa")
            
            logger.info(f"Cerrando y reconectando cliente movil (ronda {i+1})...")
            await mobile_client.close()
            success = await mobile_client.reconnect_with_token()
            if not success:
                reconnect_success = False
                reconnect_error = f"Fallo la reconexion movil #{i+1}"
                logger.error(reconnect_error)
                break
            logger.info(f"Reconexion movil #{i+1} exitosa")
            
            # Verificar comunicacion
            test_message = f"Prueba despues de reconexion múltiple {i+1}"
            logger.info(f"Web envia mensaje tras reconexion múltiple: {test_message}")
            await web_client.send_message(test_message)
            
            logger.info("Movil esperando mensaje tras reconexion múltiple...")
            response = await mobile_client.receive_message(timeout=5, ignore_confirmations=True)
            if not response or response.get('message') != test_message:
                reconnect_success = False
                reconnect_error = f"Fallo la comunicacion despues de reconexion múltiple {i+1}"
                logger.error(reconnect_error)
                logger.error(f"Esperaba: '{test_message}', Recibio: {response}")
                break
            logger.info(f"Movil recibio mensaje correctamente tras reconexion múltiple #{i+1}")
            
            # Esperar un poco entre rondas
            await asyncio.sleep(0.5)
        
        if reconnect_success:
            logger.info("Todas las reconexiones múltiples fueron exitosas")
            results.add_success("Múltiples reconexiones consecutivas")
        else:
            results.add_failure("Múltiples reconexiones consecutivas", reconnect_error)
        
        # 10. Cerrar las conexiones
        log_section("FINALIZANDO PRUEBAS")
        logger.info("Cerrando conexiones...")
        await web_client.close()
        await mobile_client.close()
        
        # Mostrar resumen de resultados
        return results
    
    except Exception as e:
        logger.error(f"Error inesperado durante la prueba: {e}")
        import traceback
        traceback.print_exc()
        results.add_failure("Error general", str(e))
        
        # Cerrar conexiones en caso de error
        if web_client:
            await web_client.close()
        if mobile_client:
            await mobile_client.close()
        
        return results


if __name__ == "__main__":
    try:
        logger.info("\n=== INICIANDO PRUEBAS DE RECONEXIoN MINKA ===")
        
        results = asyncio.run(run_test())
        results.print_summary()
        
        # Salir con codigo 0 si todas las pruebas pasaron, 1 si alguna fallo
        sys.exit(0 if results.passed_tests == results.total_tests else 1)
    except KeyboardInterrupt:
        logger.info("Prueba interrumpida por el usuario")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error en la prueba: {e}")
        sys.exit(1)
