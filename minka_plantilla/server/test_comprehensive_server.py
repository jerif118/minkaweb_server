#!/usr/bin/env python3
"""
Script de pruebas exhaustivas para el servidor Minka WebSocket V2
Simula clientes web y móviles, prueba todos los flujos críticos
"""

import asyncio
import websockets
import json
import time
import uuid
import logging
import sys
import signal
from datetime import datetime
import requests
from urllib.parse import urlencode

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s'
)
logger = logging.getLogger('TestSuite')

class TestClient:
    """Cliente de prueba que simula conexiones WebSocket"""
    
    def __init__(self, client_type="web", client_id=None):
        self.client_type = client_type  # "web" o "mobile"
        self.client_id = client_id or f"{client_type}-{uuid.uuid4().hex[:8]}"
        self.websocket = None
        self.jwt_token = None
        self.room_id = None
        self.room_password = None
        self.connected = False
        self.messages_received = []
        self.server_url = "ws://localhost:5001/ws"
        
    async def connect(self, action=None, **params):
        """Conectar al servidor WebSocket"""
        try:
            # Construir URL con parámetros
            url_params = {}
            if self.jwt_token:
                url_params['jwt'] = self.jwt_token
            if action:
                url_params['action'] = action
            
            # Agregar parámetros adicionales
            url_params.update(params)
            
            # Construir URL final
            if url_params:
                url = f"{self.server_url}?{urlencode(url_params)}"
            else:
                url = self.server_url
                
            logger.info(f"[{self.client_id}] Conectando a {url}")
            
            # Configurar headers si es necesario
            headers = {}
            if self.jwt_token and not url_params.get('jwt'):
                headers['Sec-WebSocket-Protocol'] = self.jwt_token
                
            self.websocket = await websockets.connect(url, extra_headers=headers)
            self.connected = True
            logger.info(f"[{self.client_id}] Conectado exitosamente")
            
            # Iniciar task para recibir mensajes
            asyncio.create_task(self._receive_messages())
            
        except Exception as e:
            logger.error(f"[{self.client_id}] Error de conexión: {e}")
            self.connected = False
            raise
            
    async def _receive_messages(self):
        """Task para recibir mensajes del servidor"""
        try:
            async for message in self.websocket:
                data = json.loads(message)
                self.messages_received.append(data)
                logger.info(f"[{self.client_id}] Recibido: {data}")
                
                # Procesar eventos importantes
                if data.get('event') == 'room_created':
                    self.room_id = data.get('room_id')
                    self.room_password = data.get('room_password')
                    self.jwt_token = data.get('jwt_token')
                    # Actualizar client_id si el servidor nos asignó uno nuevo
                    if data.get('client_id'):
                        self.client_id = data.get('client_id')
                    
                elif data.get('event') == 'joined_room':
                    self.room_id = data.get('room_id')
                    self.jwt_token = data.get('jwt_token')
                    # Actualizar client_id si el servidor nos asignó uno nuevo
                    if data.get('client_id'):
                        self.client_id = data.get('client_id')
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"[{self.client_id}] Conexión cerrada")
            self.connected = False
        except Exception as e:
            logger.error(f"[{self.client_id}] Error recibiendo mensajes: {e}")
            self.connected = False
            
    async def send_message(self, message):
        """Enviar mensaje al servidor"""
        if not self.connected:
            raise Exception("No conectado")
            
        try:
            await self.websocket.send(json.dumps(message))
            logger.info(f"[{self.client_id}] Enviado: {message}")
        except Exception as e:
            logger.error(f"[{self.client_id}] Error enviando mensaje: {e}")
            raise
            
    async def disconnect(self, reason="normal"):
        """Desconectar del servidor"""
        if self.connected and self.websocket:
            try:
                if reason == "doze":
                    await self.send_message({"action": "leave", "reason": "doze"})
                elif reason == "leave":
                    await self.send_message({
                        "action": "leave", 
                        "reason": "user_disconnect",
                        "jwt_token": self.jwt_token
                    })
                else:
                    await self.websocket.close()
                    
                self.connected = False
                logger.info(f"[{self.client_id}] Desconectado (razón: {reason})")
            except Exception as e:
                logger.error(f"[{self.client_id}] Error al desconectar: {e}")
                
    def get_last_message(self):
        """Obtener el último mensaje recibido"""
        return self.messages_received[-1] if self.messages_received else None
        
    def clear_messages(self):
        """Limpiar lista de mensajes recibidos"""
        self.messages_received = []

class TestSuite:
    """Suite de pruebas exhaustivas"""
    
    def __init__(self):
        self.server_url = "http://localhost:5001"
        self.results = []
        self.failed_tests = []
        
    def log_test_result(self, test_name, success, details=""):
        """Registrar resultado de prueba"""
        status = "✅ PASS" if success else "❌ FAIL"
        logger.info(f"{status} - {test_name}: {details}")
        
        self.results.append({
            'test': test_name,
            'success': success,
            'details': details,
            'timestamp': datetime.now()
        })
        
        if not success:
            self.failed_tests.append(test_name)
            
    async def test_server_health(self):
        """Probar el endpoint de salud del servidor"""
        try:
            response = requests.get(f"{self.server_url}/health", timeout=5)
            success = response.status_code == 200
            details = f"Status: {response.status_code}, Response: {response.json()}"
            self.log_test_result("Server Health Check", success, details)
            return success
        except Exception as e:
            self.log_test_result("Server Health Check", False, f"Error: {e}")
            return False
            
    async def test_room_creation_web(self):
        """Probar creación de sala por cliente web"""
        client = TestClient("web")
        try:
            await client.connect(action="create")
            await asyncio.sleep(1)  # Esperar respuesta
            
            last_msg = client.get_last_message()
            success = (last_msg and 
                      last_msg.get('event') == 'room_created' and
                      last_msg.get('room_id') and
                      last_msg.get('room_password') and
                      last_msg.get('jwt_token'))
                      
            details = f"Room ID: {client.room_id}, Password: {client.room_password}"
            self.log_test_result("Web Room Creation", success, details)
            
            await client.disconnect("leave")
            return success, client
            
        except Exception as e:
            self.log_test_result("Web Room Creation", False, f"Error: {e}")
            return False, None
            
    async def test_room_join_web(self):
        """Probar unirse a sala por cliente web"""
        # Primero crear una sala
        creator = TestClient("web")
        await creator.connect(action="create")
        await asyncio.sleep(1)
        
        if not creator.room_id:
            self.log_test_result("Web Room Join - Setup", False, "No se pudo crear sala")
            return False
            
        # Ahora unirse a la sala
        joiner = TestClient("web")
        try:
            await joiner.connect(
                action="join", 
                room_id=creator.room_id, 
                room_password=creator.room_password
            )
            await asyncio.sleep(1)
            
            last_msg = joiner.get_last_message()
            success = (last_msg and 
                      last_msg.get('event') == 'joined_room' and
                      last_msg.get('room_id') == creator.room_id and
                      last_msg.get('jwt_token'))
                      
            details = f"Joined room: {joiner.room_id}"
            self.log_test_result("Web Room Join", success, details)
            
            await creator.disconnect("leave")
            await joiner.disconnect("leave")
            return success
            
        except Exception as e:
            self.log_test_result("Web Room Join", False, f"Error: {e}")
            await creator.disconnect("leave")
            return False
            
    async def test_message_broadcast(self):
        """Probar transmisión de mensajes entre clientes"""
        creator = TestClient("web")
        joiner = TestClient("web")
        
        try:
            # Crear sala y unirse
            await creator.connect(action="create")
            await asyncio.sleep(1)
            
            await joiner.connect(
                action="join",
                room_id=creator.room_id,
                room_password=creator.room_password
            )
            await asyncio.sleep(1)
            
            # Limpiar mensajes previos
            creator.clear_messages()
            joiner.clear_messages()
            
            # Enviar mensaje del creador al que se unió
            test_message = {"type": "test", "content": "Hello from creator", "timestamp": time.time()}
            await creator.send_message({"message": test_message})
            await asyncio.sleep(1)
            
            # Verificar que el joiner recibió el mensaje
            joiner_msg = joiner.get_last_message()
            success = (joiner_msg and 
                      joiner_msg.get('sender_id') == creator.client_id and
                      joiner_msg.get('message') == test_message)
                      
            details = f"Message delivered: {success}"
            self.log_test_result("Message Broadcast", success, details)
            
            await creator.disconnect("leave")
            await joiner.disconnect("leave")
            return success
            
        except Exception as e:
            self.log_test_result("Message Broadcast", False, f"Error: {e}")
            return False
            
    async def test_jwt_reconnection_web(self):
        """Probar reconexión web usando JWT"""
        client = TestClient("web")
        
        try:
            # Crear sala
            await client.connect(action="create")
            await asyncio.sleep(1)
            
            original_jwt = client.jwt_token
            original_room = client.room_id
            
            if not original_jwt:
                self.log_test_result("JWT Reconnection - Setup", False, "No JWT received")
                return False
                
            # Desconectar inesperadamente (simular caída de red)
            await client.websocket.close()
            await asyncio.sleep(1)
            
            # Reconectar usando JWT
            client.connected = False
            client.websocket = None
            await client.connect()  # JWT ya está en client.jwt_token
            await asyncio.sleep(1)
            
            last_msg = client.get_last_message()
            success = (last_msg and 
                      last_msg.get('event') == 'reconnected' and
                      last_msg.get('room_id') == original_room)
                      
            details = f"Reconnected to room: {original_room}"
            self.log_test_result("JWT Reconnection", success, details)
            
            await client.disconnect("leave")
            return success
            
        except Exception as e:
            self.log_test_result("JWT Reconnection", False, f"Error: {e}")
            return False
            
    async def test_mobile_doze_mode(self):
        """Probar modo doze para cliente móvil"""
        web_client = TestClient("web")
        mobile_client = TestClient("mobile")
        
        try:
            # Cliente web crea sala
            await web_client.connect(action="create")
            await asyncio.sleep(1)
            
            # Cliente móvil se une
            await mobile_client.connect(
                action="join",
                room_id=web_client.room_id,
                room_password=web_client.room_password,
                client_id=mobile_client.client_id
            )
            await asyncio.sleep(1)
            
            # Cliente móvil entra en modo doze
            await mobile_client.disconnect("doze")
            await asyncio.sleep(1)
            
            # Cliente web envía mensajes mientras móvil está en doze
            web_client.clear_messages()
            test_messages = [
                {"type": "test1", "content": "Message while dozing 1"},
                {"type": "test2", "content": "Message while dozing 2"}
            ]
            
            for msg in test_messages:
                await web_client.send_message({"message": msg})
                await asyncio.sleep(0.5)
                
            # Verificar que web recibió confirmación de encolado
            web_last_msg = web_client.get_last_message()
            queued_success = web_last_msg and web_last_msg.get('event') == 'message_queued'
            
            # Cliente móvil reconecta
            await mobile_client.connect(
                action="join",
                room_id=web_client.room_id,
                room_password=web_client.room_password,
                client_id=mobile_client.client_id
            )
            await asyncio.sleep(2)  # Tiempo para que lleguen mensajes pendientes
            
            # Verificar que móvil recibió mensajes pendientes
            mobile_msgs = [msg for msg in mobile_client.messages_received 
                          if msg.get('message', {}).get('type', '').startswith('test')]
            
            success = queued_success and len(mobile_msgs) >= len(test_messages)
            details = f"Queued messages delivered: {len(mobile_msgs)}/{len(test_messages)}"
            self.log_test_result("Mobile Doze Mode", success, details)
            
            await web_client.disconnect("leave")
            await mobile_client.disconnect("leave")
            return success
            
        except Exception as e:
            self.log_test_result("Mobile Doze Mode", False, f"Error: {e}")
            return False
            
    async def test_jwt_blacklisting(self):
        """Probar blacklisting de JWT al hacer leave"""
        client = TestClient("web")
        
        try:
            # Crear sala
            await client.connect(action="create")
            await asyncio.sleep(1)
            
            original_jwt = client.jwt_token
            
            if not original_jwt:
                self.log_test_result("JWT Blacklisting - Setup", False, "No JWT received")
                return False
                
            # Hacer leave explícito (debería blacklistear JWT)
            await client.disconnect("leave")
            await asyncio.sleep(1)
            
            # Intentar reconectar con el mismo JWT
            client.connected = False
            client.websocket = None
            
            try:
                await client.connect()
                await asyncio.sleep(1)
                
                # Debería fallar o recibir error de token inválido
                last_msg = client.get_last_message()
                blacklist_works = (last_msg and 
                                 (last_msg.get('error') or 
                                  last_msg.get('code') in ['INVALID_TOKEN', 'INVALID_TOKEN_SESSION']))
                
                if client.connected:
                    await client.disconnect()
                    
                details = f"JWT properly blacklisted: {blacklist_works}"
                self.log_test_result("JWT Blacklisting", blacklist_works, details)
                return blacklist_works
                
            except websockets.exceptions.ConnectionClosedError:
                # Conexión cerrada inmediatamente = JWT blacklisted correctamente
                self.log_test_result("JWT Blacklisting", True, "Connection rejected (JWT blacklisted)")
                return True
                
        except Exception as e:
            self.log_test_result("JWT Blacklisting", False, f"Error: {e}")
            return False
            
    async def test_invalid_actions(self):
        """Probar manejo de acciones inválidas y errores"""
        tests = [
            ("Invalid JSON", "invalid json"),
            ("No action", {}),
            ("Invalid action", {"action": "invalid_action"}),
            ("Missing room_id for join", {"action": "join", "room_password": "123456"}),
            ("Invalid room format", {"action": "join", "room_id": "invalid", "room_password": "123456"}),
            ("Wrong password", {"action": "join", "room_id": "a" * 32, "room_password": "wrong"})
        ]
        
        all_passed = True
        
        for test_name, test_data in tests:
            client = TestClient("web")
            error_handled = False
            
            try:
                if test_name == "Invalid JSON":
                    # Para JSON inválido, necesitamos conectar primero
                    await client.connect()
                    await asyncio.sleep(0.1)  # Pequeña pausa para asegurar conexión
                    await client.websocket.send(test_data)  # Enviar string directamente
                elif test_data.get("action") == "join":
                    await client.connect(**test_data)
                elif test_name in ["No action", "Invalid action"]:
                    # Para estos casos, conectar sin parámetros y luego enviar data inválida
                    await client.connect()
                    await asyncio.sleep(0.2)  # Pausa más larga para asegurar conexión estable
                    
                    # Verificar que estamos conectados antes de enviar
                    if not client.connected:
                        error_handled = True
                        self.log_test_result(f"Invalid Action: {test_name}", True, 
                                           "Connection closed immediately (proper behavior)")
                        continue
                    
                    try:
                        await client.send_message(test_data)
                        await asyncio.sleep(0.5)  # Dar más tiempo para la respuesta
                    except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosed):
                        # La conexión se cerró durante el envío = error manejado correctamente
                        error_handled = True
                        self.log_test_result(f"Invalid Action: {test_name}", True, 
                                           "Connection properly closed during send")
                        continue
                    except Exception as e:
                        # Si hay una excepción de "No conectado", también es válido
                        if "No conectado" in str(e):
                            error_handled = True
                            self.log_test_result(f"Invalid Action: {test_name}", True, 
                                               "Connection state properly detected")
                            continue
                        else:
                            raise e
                else:
                    await client.connect()
                    await asyncio.sleep(0.1)
                    await client.send_message(test_data)
                        
                # Esperar por respuesta o cierre de conexión
                await asyncio.sleep(0.2)
                
                # Verificar que se recibió un error
                last_msg = client.get_last_message()
                has_error = last_msg and ('error' in last_msg or 'code' in last_msg)
                
                # Si recibimos un error o la conexión se cerró, el manejo fue correcto
                error_handled = has_error or not client.connected
                
                self.log_test_result(f"Invalid Action: {test_name}", error_handled, 
                                   f"Error properly handled: {error_handled}")
                
                if not error_handled:
                    all_passed = False
                    
                if client.connected:
                    await client.disconnect()
                    
            except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosed):
                # Conexión cerrada = error manejado correctamente
                error_handled = True
                self.log_test_result(f"Invalid Action: {test_name}", True, 
                                   "Connection properly closed")
            except Exception as e:
                self.log_test_result(f"Invalid Action: {test_name}", False, f"Unexpected error: {e}")
                all_passed = False
                
        return all_passed
        
    async def test_cleanup_and_timeouts(self):
        """Probar limpieza de sesiones y timeouts"""
        # Este test requiere modificar temporalmente los timeouts o esperar mucho tiempo
        # Por ahora, solo verificamos que el monitor endpoint funcione
        try:
            response = requests.get(f"{self.server_url}/monitor", timeout=5)
            success = response.status_code == 200
            data = response.json() if success else {}
            
            details = f"Monitor accessible, sessions: {data.get('active_sessions_count', 'N/A')}"
            self.log_test_result("Monitor Endpoint", success, details)
            return success
            
        except Exception as e:
            self.log_test_result("Monitor Endpoint", False, f"Error: {e}")
            return False
            
    async def run_stress_test(self, num_clients=10, duration=30):
        """Ejecutar prueba de estrés con múltiples clientes"""
        logger.info(f"Iniciando prueba de estrés: {num_clients} clientes por {duration}s")
        
        clients = []
        successful_connections = 0
        messages_sent = 0
        messages_received = 0
        
        try:
            # Crear clientes
            for i in range(num_clients):
                client_type = "web" if i % 2 == 0 else "mobile"
                client = TestClient(client_type)
                clients.append(client)
                
            # Conectar clientes (mitad crea salas, mitad se une)
            for i, client in enumerate(clients):
                try:
                    if i < num_clients // 2:
                        await client.connect(action="create")
                    else:
                        # Unirse a la sala del cliente correspondiente
                        creator_idx = i - num_clients // 2
                        if creator_idx < len(clients) and clients[creator_idx].room_id:
                            await client.connect(
                                action="join",
                                room_id=clients[creator_idx].room_id,
                                room_password=clients[creator_idx].room_password
                            )
                            
                    await asyncio.sleep(0.1)  # Pequeña pausa entre conexiones
                    
                    if client.connected:
                        successful_connections += 1
                        
                except Exception as e:
                    logger.error(f"Error conectando cliente {i}: {e}")
                    
            # Enviar mensajes durante la duración especificada
            start_time = time.time()
            while time.time() - start_time < duration:
                for client in clients:
                    if client.connected:
                        try:
                            test_msg = {
                                "type": "stress_test",
                                "content": f"Message from {client.client_id}",
                                "timestamp": time.time()
                            }
                            await client.send_message({"message": test_msg})
                            messages_sent += 1
                            
                        except Exception as e:
                            logger.error(f"Error enviando mensaje desde {client.client_id}: {e}")
                            
                await asyncio.sleep(1)  # Enviar mensaje cada segundo
                
            # Contar mensajes recibidos
            for client in clients:
                messages_received += len([msg for msg in client.messages_received 
                                        if msg.get('message', {}).get('type') == 'stress_test'])
                
            # Desconectar clientes
            for client in clients:
                if client.connected:
                    await client.disconnect("leave")
                    
            success = successful_connections >= num_clients * 0.8  # 80% de éxito mínimo
            details = (f"Connections: {successful_connections}/{num_clients}, "
                      f"Messages sent: {messages_sent}, received: {messages_received}")
                      
            self.log_test_result("Stress Test", success, details)
            return success
            
        except Exception as e:
            self.log_test_result("Stress Test", False, f"Error: {e}")
            return False
            
    def print_summary(self):
        """Imprimir resumen de resultados"""
        total_tests = len(self.results)
        passed_tests = sum(1 for r in self.results if r['success'])
        failed_tests = total_tests - passed_tests
        
        print("\n" + "="*60)
        print("🧪 RESUMEN DE PRUEBAS EXHAUSTIVAS")
        print("="*60)
        print(f"Total de pruebas: {total_tests}")
        print(f"✅ Exitosas: {passed_tests}")
        print(f"❌ Fallidas: {failed_tests}")
        print(f"📊 Tasa de éxito: {(passed_tests/total_tests)*100:.1f}%")
        
        if self.failed_tests:
            print(f"\n❌ Pruebas fallidas:")
            for test in self.failed_tests:
                print(f"  - {test}")
                
        print("\n📋 Detalles de todas las pruebas:")
        for result in self.results:
            status = "✅" if result['success'] else "❌"
            print(f"  {status} {result['test']}: {result['details']}")
            
        print("="*60)
        
        return failed_tests == 0

async def main():
    """Función principal de pruebas"""
    print("🚀 Iniciando pruebas exhaustivas del servidor Minka WebSocket V2")
    print("="*60)
    
    suite = TestSuite()
    
    # Verificar que el servidor esté corriendo
    if not await suite.test_server_health():
        print("❌ Servidor no disponible. Asegúrate de que esté corriendo en localhost:5001")
        return False
        
    print("\n🔄 Ejecutando pruebas de funcionalidad básica...")
    
    # Pruebas básicas
    await suite.test_room_creation_web()
    await suite.test_room_join_web()
    await suite.test_message_broadcast()
    
    print("\n🔐 Ejecutando pruebas de seguridad JWT...")
    await suite.test_jwt_reconnection_web()
    await suite.test_jwt_blacklisting()
    
    print("\n📱 Ejecutando pruebas de modo doze móvil...")
    await suite.test_mobile_doze_mode()
    
    print("\n⚠️ Ejecutando pruebas de manejo de errores...")
    await suite.test_invalid_actions()
    
    print("\n🔧 Ejecutando pruebas de monitoreo...")
    await suite.test_cleanup_and_timeouts()
    
    print("\n⚡ Ejecutando prueba de estrés...")
    await suite.run_stress_test(num_clients=8, duration=20)
    
    # Mostrar resumen
    all_passed = suite.print_summary()
    
    return all_passed

if __name__ == "__main__":
    try:
        success = asyncio.run(main())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n🛑 Pruebas interrumpidas por el usuario")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Error inesperado: {e}")
        sys.exit(1)
