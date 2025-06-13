# Documentación del Sistema Minka WebSocket

## Introducción

El sistema Minka WebSocket proporciona una plataforma de comunicación en tiempo real entre clientes web y móviles. Esta documentación explica cómo configurar, conectar y utilizar el sistema de manera efectiva.

## Índice

1. [Arquitectura del Sistema](#arquitectura-del-sistema)
2. [Conexión de Clientes](#conexión-de-clientes)
   - [Cliente Web](#cliente-web)
   - [Cliente Móvil](#cliente-móvil)
   - [Reconexiones](#reconexiones)
3. [Modo Doze](#modo-doze)
4. [Estructura de Mensajes](#estructura-de-mensajes)
5. [Autenticación con JWT](#autenticación-con-jwt)
6. [Sistema de Encolamiento](#sistema-de-encolamiento)
7. [Sistema de Entrega Confiable de Mensajes](#sistema-de-entrega-confiable-de-mensajes)
8. [Ejemplos de Implementación](#ejemplos-de-implementación)

## Arquitectura del Sistema

El sistema está compuesto por los siguientes componentes:

- **Servidor WebSocket** (`appv2.py`): Implementado con Tornado, maneja las conexiones y enrutamiento de mensajes usando Redis para persistencia.
- **Redis**: Almacena el estado de sesiones, clientes y colas de mensajes de forma asíncrona.
- **Clientes**: Aplicaciones web o móviles que se conectan al servidor.

### Diagrama de Flujo

```
┌──────────────┐         ┌──────────────┐
│ Cliente Web  │◄───────►│              │
└──────────────┘         │              │         ┌─────────┐
                         │   Servidor   │◄───────►│  Redis  │
┌──────────────┐         │  WebSocket   │         └─────────┘
│ Cliente Móvil│◄───────►│   (appv2.py) │
└──────────────┘         └──────────────┘
```

### Endpoints del Servidor

El servidor principal (`appv2.py`) expone los siguientes endpoints:

#### Endpoints Principales

- **`/`** - Página principal que confirma que el servidor está operativo
- **`/ws`** - Endpoint WebSocket principal para todas las conexiones de clientes
- **`/health`** - Endpoint de salud que proporciona información detallada del servidor
- **`/api/status`** - Alias del endpoint de salud
- **`/monitor`** - Panel de monitoreo que muestra el estado de sesiones y clientes activos

#### Endpoint de Salud (`/health`)

El endpoint de salud proporciona información completa sobre el estado del servidor:

```json
{
  "status": "ok",
  "timestamp": 1691234567.123,
  "server_info": {
    "version": "MinkaV2 (Redis-based)",
    "port": 5001,
    "host": "localhost"
  },
  "redis": {
    "connected": true,
    "version": "7.0.0",
    "used_memory": "1.2M",
    "uptime": 3600
  },
  "server_stats": {
    "active_sessions": 5,
    "active_clients": 12,
    "dozing_clients": 2,
    "active_websockets": 8,
    "websocket_connections": ["web-abc123", "mobile-def456", ...]
  }
}
```

### Archivos Obsoletos

**IMPORTANTE**: El archivo `api.py` contiene endpoints REST heredados que **YA NO ESTÁN EN USO**. Este archivo usa variables globales que no están sincronizadas con Redis y causará errores 403. No debe ser usado en producción.

### Configuración

El servidor se configura a través de variables de entorno definidas en `config.py`:

- `MINKA_PORT`: Puerto del servidor (default: 5001)
- `MINKA_HOST`: Host del servidor (default: localhost)
- `REDIS_HOST`: Host de Redis (default: localhost)
- `REDIS_PORT`: Puerto de Redis (default: 6379)
- `MINKA_LOG_LEVEL`: Nivel de logging (default: INFO)

### Ejecución del Servidor

Para iniciar el servidor desde este directorio utilice:

```bash
python run.py
```

El script `run.py` es el punto de entrada principal y reemplaza al antiguo
`run_server.py`.

## Conexión de Clientes

### Cliente Web (JavaScript)

#### Crear una Sala

Para crear una nueva sala desde un cliente web usando JavaScript:

```javascript
// Ejemplo con JavaScript
const ws = new WebSocket(`ws://servidor:puerto/ws?client_id=web-${clientId}&action=create`);

ws.onopen = () => {
  console.log("Conexión establecida");
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log("Mensaje recibido:", data);
  
  // Guardar room_id, password y JWT para reconexión
  if (data.room_id && data.password) {
    localStorage.setItem('room_id', data.room_id);
    localStorage.setItem('password', data.password);
  }
  
  if (data.jwt_token) {
    localStorage.setItem('jwt_token', data.jwt_token);
  }
};
```

Es importante guardar el `jwt_token` que devuelve el servidor, ya que facilitará la reconexión automática en caso de desconexiones temporales.

#### Unirse a una Sala Existente

```javascript
// Unirse con room_id y password
const ws = new WebSocket(`ws://servidor:puerto/ws?client_id=web-${clientId}&action=join&room_id=${roomId}&password=${password}`);

// Unirse con JWT (después de reconexión)
const ws = new WebSocket(`ws://servidor:puerto/ws?client_id=web-${clientId}&jwt_token=${jwtToken}`);
```

Al unirse a una sala, el servidor verificará que la sala exista y que la contraseña sea correcta. Si todo es correcto, el cliente recibirá un mensaje de confirmación y un token JWT para facilitar futuras reconexiones.

### Cliente Móvil (Java/Android)

#### Crear una Sala

```java
// Ejemplo con Java para Android
String url = "ws://servidor:puerto/ws?client_id=mobile-" + clientId + "&action=create";
WebSocketClient client = new WebSocketClient(new URI(url)) {
    @Override
    public void onOpen(ServerHandshake handshake) {
        System.out.println("Conectado");
    }

    @Override
    public void onMessage(String message) {
        JSONObject data = new JSONObject(message);
        // Guardar room_id y password
        if (data.has("room_id") && data.has("password")) {
            String roomId = data.getString("room_id");
            String password = data.getString("password");
            // Guardar para reconexión
            saveCredentials(roomId, password);
        }
    }
    // ...otros métodos...
};
client.connect();
```

#### Unirse a una Sala Existente

```java
String url = "ws://servidor:puerto/ws?client_id=mobile-" + clientId + "&action=join&room_id=" + roomId + "&password=" + password;
// Crear y conectar cliente similar al ejemplo anterior
```

Los clientes móviles deben almacenar el `room_id` y `password` de manera segura para poder reconectarse después.

### Reconexiones

#### Cliente Web (usando JWT)

El método de reconexión recomendado para clientes web es utilizar el JWT:

```javascript
// Reconexión automática con JWT
function reconnect() {
  const jwtToken = localStorage.getItem('jwt_token');
  if (jwtToken) {
    const ws = new WebSocket(`ws://servidor:puerto/ws?client_id=web-${clientId}&jwt_token=${jwtToken}`);
    // Configurar callbacks
    configurarWebSocket(ws);
    return true;
  }
  return false;
}

// Manejar desconexión con backoff exponencial
let reconnectAttempts = 0;
const maxReconnectDelay = 30000; // 30 segundos máximo

ws.onclose = (event) => {
  if (!event.wasClean) {
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), maxReconnectDelay);
    console.log(`Intentando reconexión en ${delay/1000} segundos...`);
    
    setTimeout(() => {
      if (reconnect()) {
        console.log("Reconexión iniciada con JWT");
      } else {
        console.log("No se pudo reconectar con JWT, intentando con credenciales");
        // Intentar con room_id y password como alternativa
        reconnectWithCredentials();
      }
      reconnectAttempts++;
    }, delay);
  }
};
```

#### Cliente Móvil (reconexión manual)

Los clientes móviles deben implementar una lógica robusta para manejar reconexiones:

```java
// Guardar datos de sala para reconexión
private String storedRoomId;
private String storedPassword;
private int reconnectAttempts = 0;
private final int MAX_RECONNECT_DELAY = 30000; // 30 segundos máximo

// Función de reconexión con backoff exponencial
void reconnect() {
    if (storedRoomId != null && storedPassword != null) {
        int delay = Math.min(1000 * (int)Math.pow(2, reconnectAttempts), MAX_RECONNECT_DELAY);
        
        new Handler().postDelayed(() -> {
            String url = "ws://servidor:puerto/ws?client_id=mobile-" + clientId + 
                     "&action=join&room_id=" + storedRoomId + 
                     "&password=" + storedPassword;
            
            // Crear y conectar nuevo cliente
            createAndConnectClient(url);
            reconnectAttempts++;
        }, delay);
    }
}

// Manejar desconexión
@Override
public void onClose(int code, String reason, boolean remote) {
    if (code != 1000) { // Si no fue cierre limpio
        reconnect();
    }
}
```

## Modo Doze

El modo Doze es una característica para clientes móviles que permite ahorrar batería manteniendo la capacidad de recibir mensajes cuando el dispositivo se reconecta.

### Entrar en Modo Doze

```javascript
// Cliente móvil entra en modo doze
ws.send(JSON.stringify({ 
  action: "leave", 
  reason: "doze" 
}));
// Después de enviar este mensaje, el cliente puede cerrar la conexión
```

**Importante**: Solo los clientes móviles pueden entrar en modo doze. El servidor rechazará peticiones de modo doze de clientes web.

### Salir de Modo Doze

Para salir del modo doze, el cliente móvil simplemente se reconecta usando los mismos parámetros:

```java
String url = "ws://servidor:puerto/ws?client_id=mobile-" + clientId + 
             "&action=join&room_id=" + storedRoomId + 
             "&password=" + storedPassword;
// Crear y conectar nuevo cliente
```

El servidor detectará automáticamente que el cliente estaba en modo doze y:

1. Enviará un mensaje inicial: `{"info": "Reconectado desde modo doze. Verificando mensajes pendientes..."}`
2. Enviará los mensajes pendientes si hay alguno.
3. Enviará confirmación: `{"info": "Todos los mensajes pendientes han sido entregados."}`

## Estructura de Mensajes

### Mensajes del Cliente al Servidor

#### Mensaje de texto simple

```json
{
  "message": "Texto del mensaje"
}
```

#### Acciones especiales

```json
{
  "action": "leave",
  "reason": "doze"  // Razones: "doze", "exit", etc.
}
```

Para invalidar un JWT al cerrar sesión (clientes web):

```json
{
  "action": "leave",
  "reason": "exit",
  "jwt_token": "token_actual_jwt"  // Opcional: enviar el token JWT para añadirlo a la lista negra
}
```

### Mensajes del Servidor al Cliente

#### Mensaje de texto recibido

```json
{
  "sender_id": "web-abc123",
  "message": "Texto del mensaje",
  "timestamp": 1749335940.9373791,
  "message_id": "1749335940.9373791-a1b2c3d4" // ID único para seguimiento
}
```

#### Mensajes del sistema

```json
{
  "info": "Te has unido a la sala."
}
```

```json
{
  "event": "joined",
  "client": "mobile-abc123"
}
```

```json
{
  "info": "El usuario web-abc123 se ha reconectado."
}
```

#### Estado de modo doze

```json
{
  "info": "Reconectado desde modo doze. Verificando mensajes pendientes..."
}

{
  "info": "Tienes 2 mensajes pendientes."
}

{
  "info": "Todos los mensajes pendientes han sido entregados."
}
```

#### Confirmaciones de entrega de mensajes

Al enviar un mensaje, el servidor confirma que ha sido encolado:

```json
{
  "event": "message_queued",
  "message_id": "1749335940.9373791-a1b2c3d4",
  "recipient_id": "mobile-07b9c01b",
  "original_message": "Hola a todos",
  "timestamp": 1749342054.219816
}
```

Cuando el mensaje ha sido entregado al destinatario:

```json
{
  "event": "message_delivered",
  "message_id": "1749335940.9373791-a1b2c3d4",
  "recipient_id": "mobile-07b9c01b",
  "timestamp": 1749342054.412931
}
```

## Autenticación con JWT

Los clientes web reciben un JWT que pueden usar para reconectarse sin necesidad de password:

### Formato del JWT

```json
{
  "client_id": "web-abc123",
  "room_id": "98533f51-b0e4-4df0-8757-d31c78019c1b",
  "exp": 1749595137,
  "jti": "6ac966e04a8f40d3a1c2e249d5b423e1"
}
```

### Uso del JWT

1. El cliente web recibe el JWT al crear o unirse a una sala
2. Guarda el JWT de forma segura (localStorage, sessionStorage, cookies)
3. Al reconectarse, envía el JWT en la URL de conexión

```javascript
const jwtToken = localStorage.getItem('jwt_token');
const ws = new WebSocket(`ws://servidor:puerto/ws?client_id=web-${clientId}&jwt_token=${jwtToken}`);
```

### Invalidación del JWT

Para mejorar la seguridad, al cerrar sesión explícitamente, el cliente web debería invalidar su JWT:

```javascript
// Al cerrar sesión
function logout() {
  const jwtToken = localStorage.getItem('jwt_token');
  
  // Enviar mensaje de salida con JWT para invalidar
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      action: "leave",
      reason: "exit",
      jwt_token: jwtToken
    }));
  }
  
  // Limpiar almacenamiento local
  localStorage.removeItem('jwt_token');
  localStorage.removeItem('room_id');
  localStorage.removeItem('password');
}
```

## Sistema de Encolamiento

El servidor guarda mensajes en Redis para clientes desconectados o en modo doze:

### Claves en Redis

- `session:{room_id}` - Datos de la sesión
- `client:{client_id}` - Datos del cliente
- `mq:{client_id}` - Cola de mensajes para el cliente
- `jwt_blacklist:{jti}` - JTIs invalidados

### Entrega de mensajes

1. Cuando un cliente se reconecta, el servidor verifica si hay mensajes pendientes
2. Si hay mensajes, los envía en el mismo orden en que fueron recibidos
3. Después de entregar los mensajes, la cola se vacía

## Sistema de Entrega Confiable de Mensajes

El Sistema de Entrega Confiable de Mensajes asegura que todos los mensajes sean entregados incluso si hay interrupciones temporales en la conexión. Este sistema utiliza un enfoque mejorado con IDs únicos de mensajes, seguimiento de entregas y reintentos automáticos.

### Características principales

- **IDs únicos**: Cada mensaje recibe un identificador único para su seguimiento desde el envío hasta la entrega
- **Confirmación de entrega**: El sistema confirma al remitente cuando el mensaje ha sido entregado
- **Reintentos automáticos**: Si un mensaje no se entrega, el sistema realizará reintentos automáticos
- **Estado de mensaje**: Los clientes reciben información sobre el estado de la entrega de mensajes
- **Sesiones de doze**: Identificación única de sesiones de doze para mejorar la recuperación de mensajes

### Flujo de entrega de mensajes

1. Cliente envía un mensaje: `{"message": "Hola a todos"}`
2. Servidor asigna ID único y lo encola
3. Servidor confirma el encolamiento: `{"event": "message_queued", "message_id": "abc123"}`
4. Servidor intenta entregar el mensaje a los destinatarios
5. Al recibir el mensaje, los destinatarios envían confirmación implícita
6. Servidor notifica al remitente: `{"event": "message_delivered", "message_id": "abc123"}`

### Estructura de mensajes

#### Confirmación de encolamiento

```json
{
  "event": "message_queued",
  "message_id": "26f7a2d5-9b1c-4d82-b9c4-e76f5af32c1a",
  "recipient_id": "mobile-07b9c01b",
  "original_message": "Hola a todos",
  "timestamp": 1749342054.219816
}
```

#### Confirmación de entrega

```json
{
  "event": "message_delivered",
  "message_id": "26f7a2d5-9b1c-4d82-b9c4-e76f5af32c1a",
  "recipient_id": "mobile-07b9c01b",
  "timestamp": 1749342054.412931
}
```

#### Mensaje con ID para seguimiento

```json
{
  "sender_id": "web-a4fdbf0a",
  "message": "Texto del mensaje",
  "timestamp": 1749342054.219816,
  "message_id": "26f7a2d5-9b1c-4d82-b9c4-e76f5af32c1a"
}
```

### Manejo de mensajes no entregados

Si un mensaje no puede ser entregado después de múltiples intentos (por ejemplo, porque el cliente está desconectado más allá del tiempo de espera), el sistema:

1. Mantiene el mensaje en la cola de mensajes del cliente
2. Establece un tiempo de vida (TTL) para mensajes muy antiguos
3. Informa al remitente sobre los mensajes que no pudieron ser entregados

### Claves en Redis

- `mq:{client_id}` - Cola principal de mensajes
- `delivered_msgs:{message_id}` - Registro de mensajes entregados
- `unconfirmed_msgs:{client_id}` - Mensajes enviados pero sin confirmar
- `doze_session:{session_id}` - Información de sesión de doze

### Parámetros de configuración

- `MAX_DELIVERY_ATTEMPTS`: Número máximo de reintentos (3 por defecto)
- `DELIVERY_RETRY_DELAY`: Tiempo entre reintentos (2 segundos por defecto)
- `MESSAGE_TTL_SECONDS`: Tiempo de vida de los mensajes (72 horas por defecto)

## Ejemplos de Implementación

### Implementación de Cliente Web (JavaScript)

```javascript
class MinkaWebSocketClient {
  constructor(serverUrl, clientId) {
    this.serverUrl = serverUrl;
    this.clientId = `web-${clientId}`;
    this.ws = null;
    this.roomId = null;
    this.password = null;
    this.jwt = null;
    this.reconnectAttempts = 0;
    this.maxReconnectDelay = 30000; // 30 segundos máximo
    this.callbacks = {
      onMessage: () => {},
      onConnect: () => {},
      onDisconnect: () => {},
      onReconnect: () => {},
      onError: () => {},
      onMessageQueued: () => {},
      onMessageDelivered: () => {}
    };
  }

  setCallbacks(callbacks) {
    this.callbacks = { ...this.callbacks, ...callbacks };
  }

  createRoom() {
    this.connect(`${this.serverUrl}/ws?client_id=${this.clientId}&action=create`);
  }

  joinRoom(roomId, password) {
    this.roomId = roomId;
    this.password = password;
    this.connect(`${this.serverUrl}/ws?client_id=${this.clientId}&action=join&room_id=${roomId}&password=${password}`);
  }

  reconnectWithJwt() {
    if (this.jwt) {
      this.connect(`${this.serverUrl}/ws?client_id=${this.clientId}&jwt_token=${this.jwt}`);
      return true;
    }
    return false;
  }

  reconnectWithCredentials() {
    if (this.roomId && this.password) {
      this.connect(`${this.serverUrl}/ws?client_id=${this.clientId}&action=join&room_id=${this.roomId}&password=${this.password}`);
      return true;
    }
    return false;
  }

  connect(url) {
    if (this.ws) {
      this.ws.close();
    }

    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      console.log("Conectado al servidor WebSocket");
      this.reconnectAttempts = 0; // Reiniciar contador de intentos al conectar exitosamente
      this.callbacks.onConnect();
    };

    this.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      console.log("Mensaje recibido:", data);

      // Guardar información importante para reconexiones
      if (data.room_id) this.roomId = data.room_id;
      if (data.password) this.password = data.password;
      if (data.jwt_token) this.jwt = data.jwt_token;

      // Manejar confirmaciones de mensajes
      if (data.event === "message_queued") {
        this.callbacks.onMessageQueued(data);
      } else if (data.event === "message_delivered") {
        this.callbacks.onMessageDelivered(data);
      } else {
        this.callbacks.onMessage(data);
      }
    };

    this.ws.onclose = (event) => {
      console.log("Desconectado del servidor WebSocket");
      this.callbacks.onDisconnect(event);

      // Intentar reconectar automáticamente si la desconexión no fue limpia
      if (!event.wasClean) {
        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), this.maxReconnectDelay);
        console.log(`Intentando reconexión en ${delay/1000} segundos...`);
        
        setTimeout(() => {
          console.log("Intentando reconexión...");
          // Primero intentar con JWT
          if (this.reconnectWithJwt()) {
            console.log("Reconexión iniciada con JWT");
            this.callbacks.onReconnect("jwt");
          } 
          // Si no hay JWT, intentar con credenciales
          else if (this.reconnectWithCredentials()) {
            console.log("Reconexión iniciada con credenciales");
            this.callbacks.onReconnect("credentials");
          } else {
            console.log("No se pudo reconectar: faltan credenciales");
          }
          this.reconnectAttempts++;
        }, delay);
      }
    };

    this.ws.onerror = (error) => {
      console.error("Error en la conexión WebSocket:", error);
      this.callbacks.onError(error);
    };
  }

  sendMessage(text) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ message: text }));
      return true;
    }
    return false;
  }

  disconnect(reason = "exit") {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      // Si es una desconexión explícita, invalidar JWT
      if (reason === "exit" && this.jwt) {
        this.ws.send(JSON.stringify({ 
          action: "leave", 
          reason: reason,
          jwt_token: this.jwt 
        }));
      } else {
        this.ws.send(JSON.stringify({ 
          action: "leave", 
          reason: reason
        }));
      }
      this.ws.close();
      this.ws = null;
    }
  }
}
```

### Implementación de Cliente Móvil (Java/Android)

```java
public class MinkaWebSocketClient {
    private final String serverUrl;
    private final String clientId;
    private WebSocketClient client;
    private String roomId;
    private String password;
    private int reconnectAttempts = 0;
    private final int MAX_RECONNECT_DELAY = 30000; // 30 segundos máximo
    private final Handler handler = new Handler(Looper.getMainLooper());
    private MessageCallback messageCallback;
    private ConnectionCallback connectionCallback;
    private DeliveryCallback deliveryCallback;
    private boolean intentionalDisconnect = false;

    public MinkaWebSocketClient(String serverUrl, String clientId) {
        this.serverUrl = serverUrl;
        this.clientId = "mobile-" + clientId;
    }

    public interface MessageCallback {
        void onMessage(JSONObject message);
    }

    public interface ConnectionCallback {
        void onConnect();
        void onDisconnect(boolean wasClean);
        void onReconnect();
        void onError(Exception error);
    }
    
    public interface DeliveryCallback {
        void onMessageQueued(JSONObject data);
        void onMessageDelivered(JSONObject data);
    }

    public void setMessageCallback(MessageCallback callback) {
        this.messageCallback = callback;
    }

    public void setConnectionCallback(ConnectionCallback callback) {
        this.connectionCallback = callback;
    }
    
    public void setDeliveryCallback(DeliveryCallback callback) {
        this.deliveryCallback = callback;
    }

    public void createRoom() {
        connect(serverUrl + "/ws?client_id=" + clientId + "&action=create");
    }

    public void joinRoom(String roomId, String password) {
        this.roomId = roomId;
        this.password = password;
        connect(serverUrl + "/ws?client_id=" + clientId + "&action=join&room_id=" + roomId + "&password=" + password);
    }

    public void reconnect() {
        if (roomId != null && password != null) {
            int delay = Math.min(1000 * (int)Math.pow(2, reconnectAttempts), MAX_RECONNECT_DELAY);
            
            handler.postDelayed(() -> {
                String url = "ws://servidor:puerto/ws?client_id=mobile-" + clientId + 
                         "&action=join&room_id=" + roomId + 
                         "&password=" + password;
                
                // Crear y conectar nuevo cliente
                createAndConnectClient(url);
                reconnectAttempts++;
            }, delay);
        }
    }

    private void connect(String url) {
        try {
            intentionalDisconnect = false;
            
            if (client != null) {
                client.close();
            }

            client = new WebSocketClient(new URI(url)) {
                @Override
                public void onOpen(ServerHandshake handshake) {
                    reconnectAttempts = 0; // Reiniciar contador al conectar exitosamente
                    if (connectionCallback != null) {
                        connectionCallback.onConnect();
                    }
                }

                @Override
                public void onMessage(String message) {
                    try {
                        JSONObject data = new JSONObject(message);
                        
                        // Guardar información importante para reconexiones
                        if (data.has("room_id")) {
                            roomId = data.getString("room_id");
                        }
                        
                        if (data.has("password")) {
                            password = data.getString("password");
                        }
                        
                        // Manejar eventos de entrega
                        if (data.has("event")) {
                            String event = data.getString("event");
                            if ("message_queued".equals(event) && deliveryCallback != null) {
                                deliveryCallback.onMessageQueued(data);
                                return;
                            } else if ("message_delivered".equals(event) && deliveryCallback != null) {
                                deliveryCallback.onMessageDelivered(data);
                                return;
                            }
                        }
                        
                        // Mensaje normal
                        if (messageCallback != null) {
                          messageCallback.onMessage(data);
                        }
                    } catch (JSONException e) {
                        e.printStackTrace();
                    }
                }

                @Override
                public void onClose(int code, String reason, boolean remote) {
                    if (connectionCallback != null) {
                        connectionCallback.onDisconnect(true);
                    }
                }

                @Override
                public void onClosing(int code, String reason) {
                    super.onClosing(code, reason);
                    client.close(code, reason);
                }

                @Override
                public void onError(Exception ex) {
                    if (connectionCallback != null) {
                        connectionCallback.onError(ex);
                        connectionCallback.onDisconnect(false);
                    }
                    
                    // Intentar reconexión si no fue desconexión intencional
                    if (!intentionalDisconnect) {
                        reconnect();
                    }
                }
            }.start();
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    public void sendMessage(String message) {
        if (client != null) {
            client.send(message);
        }
    }

    public void enterDozeMode() {
        try {
            JSONObject action = new JSONObject();
            action.put("action", "leave");
            action.put("reason", "doze");
            
            intentionalDisconnect = true;
            client.send(action.toString());
            client.close();
        } catch (JSONException e) {
            e.printStackTrace();
        }
    }

    public void disconnect() {
        try {
            intentionalDisconnect = true;
            
            if (client != null) {
                JSONObject action = new JSONObject();
                action.put("action", "leave");
                action.put("reason", "exit");
                
                client.send(action.toString());
                client.close();
                client = null;
            }
        } catch (JSONException e) {
            e.printStackTrace();
        }
    }
}
```

### Implementación de Cliente Móvil (Kotlin/Android)

```kotlin
import android.os.Handler
import android.os.Looper
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import okhttp3.*
import org.json.JSONException
import org.json.JSONObject
import java.util.*
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.math.pow

class MinkaWebSocketClient(
    private val serverUrl: String,
    clientIdSuffix: String,
    private val coroutineScope: CoroutineScope = CoroutineScope(Dispatchers.Main)
) {
    companion object {
        private const val TAG = "MinkaWebSocket"
        private const val MAX_RECONNECT_DELAY_MS = 30000L // 30 segundos máximo
    }

    // Identificador único del cliente (móvil)
    private val clientId = "mobile-$clientIdSuffix"

    // Cliente OkHttp para conexiones WebSocket
    private val client = OkHttpClient.Builder()
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .connectTimeout(30, TimeUnit.SECONDS)
        .pingInterval(15, TimeUnit.SECONDS)
        .build()

    // Referencia al WebSocket activo
    private var webSocket: WebSocket? = null

    // Datos para reconexión
    private var roomId: String? = null
    private var password: String? = null
    private var reconnectAttempts = 0
    private var intentionalDisconnect = false

    // Handler para UI y reconexiones
    private val handler = Handler(Looper.getMainLooper())

    // Interfaces de callback
    interface MessageCallback {
        fun onMessage(data: JSONObject)
    }

    interface ConnectionCallback {
        fun onConnect()
        fun onDisconnect(wasClean: Boolean)
        fun onReconnect()
        fun onError(error: Throwable)
    }

    interface DeliveryCallback {
        fun onMessageQueued(data: JSONObject)
        fun onMessageDelivered(data: JSONObject)
    }

    // Callbacks
    private var messageCallback: MessageCallback? = null
    private var connectionCallback: ConnectionCallback? = null
    private var deliveryCallback: DeliveryCallback? = null

    fun setMessageCallback(callback: MessageCallback) {
        messageCallback = callback
    }

    fun setConnectionCallback(callback: ConnectionCallback) {
        connectionCallback = callback
    }

    fun setDeliveryCallback(callback: DeliveryCallback) {
        deliveryCallback = callback
    }

    // Función para crear una nueva sala
    fun createRoom() {
        val url = "$serverUrl/ws?client_id=$clientId&action=create"
        connect(url)
    }

    // Función para unirse a una sala existente
    fun joinRoom(roomId: String, password: String) {
        this.roomId = roomId
        this.password = password
        val url = "$serverUrl/ws?client_id=$clientId&action=join&room_id=$roomId&password=$password"
        connect(url)
    }

    // Reconexión con backoff exponencial
    fun reconnect() {
        roomId?.let { savedRoomId ->
            password?.let { savedPassword ->
                val delay = min(1000L * 2.0.pow(reconnectAttempts.toDouble()).toLong(), MAX_RECONNECT_DELAY_MS)
                
                handler.postDelayed({
                    Log.d(TAG, "Intentando reconexión ($reconnectAttempts)...")
                    joinRoom(savedRoomId, savedPassword)
                    connectionCallback?.onReconnect()
                    reconnectAttempts++
                }, delay)
            }
        }
    }

    // Establecer la conexión WebSocket
    private fun connect(url: String) {
        // Restablecer flag de desconexión intencional
        intentionalDisconnect = false
        
        // Cerrar conexión existente si la hay
        webSocket?.close(1000, "Nueva conexión iniciada")
        
        // Crear la petición
        val request = Request.Builder()
            .url(url)
            .build()
        
        // Crear y configurar el listener de WebSocket
        val listener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.d(TAG, "Conexión WebSocket establecida")
                reconnectAttempts = 0 // Reiniciar contador al conectar exitosamente
                
                // Ejecutar callback en hilo principal
                handler.post {
                    connectionCallback?.onConnect()
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                Log.d(TAG, "Mensaje recibido: $text")
                
                try {
                    val data = JSONObject(text)
                    
                    // Guardar información importante para reconexiones
                    if (data.has("room_id")) {
                        roomId = data.getString("room_id")
                        Log.d(TAG, "Room ID guardado: $roomId")
                    }
                    
                    if (data.has("password")) {
                        password = data.getString("password")
                        Log.d(TAG, "Password guardada")
                    }
                    
                    // Ejecutar callbacks en hilo principal
                    handler.post {
                        // Manejar eventos de entrega
                        if (data.has("event")) {
                            when (data.getString("event")) {
                                "message_queued" -> deliveryCallback?.onMessageQueued(data)
                                "message_delivered" -> deliveryCallback?.onMessageDelivered(data)
                                else -> messageCallback?.onMessage(data)
                            }
                        } else {
                            // Mensaje normal
                            messageCallback?.onMessage(data)
                        }
                    }
                } catch (e: JSONException) {
                    Log.e(TAG, "Error al procesar mensaje JSON", e)
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.d(TAG, "WebSocket cerrado: code=$code, reason=$reason")
                
                // Ejecutar callback en hilo principal
                handler.post {
                    connectionCallback?.onDisconnect(true)
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Log.d(TAG, "WebSocket cerrándose: code=$code, reason=$reason")
                webSocket.close(code, reason)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "Error en WebSocket", t)
                
                // Ejecutar callbacks en hilo principal
                handler.post {
                    connectionCallback?.onError(t)
                    connectionCallback?.onDisconnect(false)
                    
                    // Intentar reconexión si no fue desconexión intencional
                    if (!intentionalDisconnect) {
                        reconnect()
                    }
                }
            }
        }
        
        // Iniciar la conexión WebSocket
        webSocket = client.newWebSocket(request, listener)
    }

    // Enviar mensaje de texto
    fun sendMessage(text: String): Boolean {
        return try {
            val message = JSONObject().apply {
                put("message", text)
            }
            
            webSocket?.send(message.toString()) ?: false
        } catch (e: JSONException) {
            Log.e(TAG, "Error al crear mensaje JSON", e)
            false
        }
    }

    // Entrar en modo doze (ahorro de batería)
    fun enterDozeMode() {
        try {
            val action = JSONObject().apply {
                put("action", "leave")
                put("reason", "doze")
            }
            
            intentionalDisconnect = true
            webSocket?.send(action.toString())
            webSocket?.close(1000, "Entrando en modo doze")
            Log.d(TAG, "Entrando en modo doze")
        } catch (e: JSONException) {
            Log.e(TAG, "Error al crear mensaje de modo doze", e)
        }
    }

    // Desconectarse completamente
    fun disconnect() {
        try {
            intentionalDisconnect = true
            
            webSocket?.let {
                val action = JSONObject().apply {
                    put("action", "leave")
                    put("reason", "exit")
                }
                
                it.send(action.toString())
                it.close(1000, "Desconexión solicitada por el usuario")
                webSocket = null
                Log.d(TAG, "Cliente desconectado")
            }
        } catch (e: JSONException) {
            Log.e(TAG, "Error al crear mensaje de desconexión", e)
        }
    }
    
    // Observador de conectividad de red (ejemplo de uso)
    fun registerNetworkCallback(networkAvailable: Boolean) {
        if (networkAvailable && webSocket == null && !intentionalDisconnect) {
            Log.d(TAG, "Red disponible, intentando reconectar...")
            reconnect()
        }
    }
}
```

#### Ejemplo de Uso del Cliente Kotlin

```kotlin
// Actividad principal
class MainActivity : AppCompatActivity() {

    private lateinit var minkaClient: MinkaWebSocketClient
    private lateinit var binding: ActivityMainBinding
    private val messagesList = mutableListOf<Message>()
    private val adapter = MessagesAdapter(messagesList)
    
    // Monitor de conectividad de red
    private lateinit var connectivityManager: ConnectivityManager
    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            // La red está disponible, intentar reconectar si es necesario
            minkaClient.registerNetworkCallback(true)
        }
        
        override fun onLost(network: Network) {
            // Notificar al usuario sobre la pérdida de conexión
            runOnUiThread {
                showSnackbar("Conexión de red perdida")
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        
        setupRecyclerView()
        setupNetworkMonitor()
        
        // Inicializar el cliente WebSocket
        minkaClient = MinkaWebSocketClient(
            serverUrl = "ws://minka-server.example.com",
            clientIdSuffix = UUID.randomUUID().toString().substring(0, 8)
        )
        
        // Configurar callbacks
        setupCallbacks()
        
        // Botones para interactuar con el WebSocket
        binding.btnCreateRoom.setOnClickListener { createRoom() }
        binding.btnJoinRoom.setOnClickListener { showJoinRoomDialog() }
        binding.btnSendMessage.setOnClickListener { sendMessage() }
        binding.btnEnterDoze.setOnClickListener { enterDozeMode() }
    }
    
    private fun setupCallbacks() {
        // Callback para mensajes recibidos
        minkaClient.setMessageCallback(object : MinkaWebSocketClient.MessageCallback {
            override fun onMessage(data: JSONObject) {
                runOnUiThread {
                    // Procesar mensaje recibido
                    if (data.has("message")) {
                        val senderId = data.optString("sender_id", "Sistema")
                        val messageText = data.getString("message")
                        val timestamp = data.optLong("timestamp", System.currentTimeMillis() / 1000)
                        val messageId = data.optString("message_id", "")
                        
                        val message = Message(
                            id = messageId,
                            senderId = senderId,
                            text = messageText,
                            timestamp = timestamp,
                            isFromMe = false,
                            status = MessageStatus.RECEIVED
                        )
                        
                        messagesList.add(message)
                        adapter.notifyItemInserted(messagesList.size - 1)
                        binding.recyclerMessages.scrollToPosition(messagesList.size - 1)
                    } 
                    else if (data.has("info")) {
                        showSnackbar(data.getString("info"))
                    }
                }
            }
        })
        
        // Callback para eventos de conexión
        minkaClient.setConnectionCallback(object : MinkaWebSocketClient.ConnectionCallback {
            override fun onConnect() {
                runOnUiThread {
                    binding.statusIndicator.setBackgroundColor(
                        ContextCompat.getColor(this@MainActivity, R.color.connected)
                    )
                    binding.textStatus.text = "Conectado"
                    showSnackbar("Conectado al servidor")
                }
            }
            
            override fun onDisconnect(wasClean: Boolean) {
                runOnUiThread {
                    binding.statusIndicator.setBackgroundColor(
                        ContextCompat.getColor(this@MainActivity, R.color.disconnected)
                    )
                    binding.textStatus.text = "Desconectado"
                    
                    if (!wasClean) {
                        showSnackbar("Desconexión inesperada. Intentando reconectar...")
                    }
                }
            }
            
            override fun onReconnect() {
                runOnUiThread {
                    binding.statusIndicator.setBackgroundColor(
                        ContextCompat.getColor(this@MainActivity, R.color.reconnecting)
                    )
                    binding.textStatus.text = "Reconectando..."
                    showSnackbar("Intentando reconectar...")
                }
            }
            
            override fun onError(error: Throwable) {
                runOnUiThread {
                    showSnackbar("Error: ${error.message}")
                }
            }
        })
        
        // Callback para eventos de entrega de mensajes
        minkaClient.setDeliveryCallback(object : MinkaWebSocketClient.DeliveryCallback {
            override fun onMessageQueued(data: JSONObject) {
                runOnUiThread {
                    val messageId = data.getString("message_id")
                    val index = messagesList.indexOfFirst { it.id == messageId }
                    
                    if (index >= 0) {
                        messagesList[index].status = MessageStatus.QUEUED
                        adapter.notifyItemChanged(index)
                    }
                }
            }
            
            override fun onMessageDelivered(data: JSONObject) {
                runOnUiThread {
                    val messageId = data.getString("message_id")
                    val index = messagesList.indexOfFirst { it.id == messageId }
                    
                    if (index >= 0) {
                        messagesList[index].status = MessageStatus.DELIVERED
                        adapter.notifyItemChanged(index)
                    }
                }
            }
        })
    }

    private fun createRoom() {
        minkaClient.createRoom()
    }
    
    private fun joinRoom(roomId: String, password: String) {
        minkaClient.joinRoom(roomId, password)
    }
    
    private fun sendMessage() {
        val messageText = binding.editTextMessage.text.toString()
        if (messageText.isNotBlank()) {
            // Crear un ID temporal para seguimiento local
            val tempId = "temp-${UUID.randomUUID()}"
            
            // Añadir mensaje a la UI con estado "enviando"
            val message = Message(
                id = tempId,
                senderId = "me",
                text = messageText,
                timestamp = System.currentTimeMillis() / 1000,
                isFromMe = true,
                status = MessageStatus.SENDING
            )
            
            messagesList.add(message)
            adapter.notifyItemInserted(messagesList.size - 1)
            binding.recyclerMessages.scrollToPosition(messagesList.size - 1)
            
            // Intentar enviar el mensaje
            val success = minkaClient.sendMessage(messageText)
            
            if (!success) {
                // Si falla, actualizar estado en UI
                message.status = MessageStatus.FAILED
                adapter.notifyItemChanged(messagesList.size - 1)
                showSnackbar("Error al enviar mensaje. Intenta de nuevo.")
            }
            
            // Limpiar campo de texto
            binding.editTextMessage.text.clear()
        }
    }
    
    private fun enterDozeMode() {
        // Guardar estado de sala antes de entrar en modo doze
        saveSessionToPreferences()
        
        // Notificar al usuario
        showSnackbar("Entrando en modo de ahorro de batería")
        
        // Entrar en modo doze
        minkaClient.enterDozeMode()
        
        // Actualizar UI
        binding.statusIndicator.setBackgroundColor(
            ContextCompat.getColor(this, R.color.doze)
        )
        binding.textStatus.text = "Modo Doze"
    }
    
    // Guardar datos de sesión para poder reconectar después
    private fun saveSessionToPreferences() {
        // Implementación para guardar roomId y password de forma segura
        // usando EncryptedSharedPreferences o similar
    }
    
    override fun onDestroy() {
        super.onDestroy()
        connectivityManager.unregisterNetworkCallback(networkCallback)
        minkaClient.disconnect()
    }
    
    // Métodos auxiliares para UI, diálogos, etc.
    private fun setupRecyclerView() {
        binding.recyclerMessages.apply {
            layoutManager = LinearLayoutManager(this@MainActivity)
            adapter = this@MainActivity.adapter
        }
    }
    
    private fun setupNetworkMonitor() {
        connectivityManager = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        connectivityManager.registerDefaultNetworkCallback(networkCallback)
    }
    
    private fun showJoinRoomDialog() {
        // Diálogo para ingresar roomId y password
    }
    
    private fun showSnackbar(message: String) {
        Snackbar.make(binding.root, message, Snackbar.LENGTH_LONG).show()
    }
}

// Clases auxiliares
data class Message(
    val id: String,
    val senderId: String,
    val text: String,
    val timestamp: Long,
    val isFromMe: Boolean,
    var status: MessageStatus = MessageStatus.SENDING
)

enum class MessageStatus {
    SENDING, QUEUED, DELIVERED, RECEIVED, FAILED
}
```

## Solución de Problemas

### Errores 403 (Forbidden)

Si encuentras errores 403, verifica lo siguiente:

1. **Archivo api.py obsoleto**: Los errores 403 frecuentemente se deben al uso del archivo `api.py` que está **DEPRECATED**. Este archivo usa variables globales vacías en lugar de Redis.

2. **Endpoints correctos**: Asegúrate de usar los endpoints del servidor principal:
   - `/ws` para conexiones WebSocket
   - `/health` para verificar el estado del servidor
   - `/monitor` para monitoreo

3. **Estado de Redis**: Verifica que Redis esté corriendo y accesible:
   ```bash
   redis-cli ping
   ```

### Verificación del Estado del Servidor

Para verificar que el servidor está funcionando correctamente:

```bash
# Verificar endpoint de salud
curl http://localhost:5001/health

# Verificar que el servidor responde
curl http://localhost:5001/
```

### Logs del Sistema

Los logs se encuentran en la carpeta `logs/`:

- `minka_server.log` - Log general del servidor
- `minka_server_error.log` - Solo errores
- `minka_YYYY-MM-DD.log` - Logs diarios
- `minka_error_YYYY-MM-DD.log` - Errores diarios

### Problemas Comunes

#### Cliente no puede conectarse

1. Verificar que Redis esté ejecutándose
2. Verificar que el servidor esté escuchando en el puerto correcto
3. Revisar los logs del servidor para errores de conexión

#### Mensajes no se entregan

1. Verificar conexión WebSocket del destinatario
2. Revisar si el cliente está en modo doze correctamente
3. Verificar las colas de mensajes en Redis:
   ```bash
   redis-cli llen mq:client_id
   ```

#### Reconexiones fallan

1. Para clientes web: verificar que el JWT no haya expirado
2. Para clientes móviles: verificar que se almacenaron room_id y password
3. Revisar logs para errores de autenticación

## Estado Actual del Sistema (Junio 2025)

### Funcionalidades Implementadas ✅

- **WebSocket Server** con Tornado y Redis
- **Autenticación JWT** para clientes web
- **Modo Doze** para clientes móviles
- **Sistema de encolamiento** de mensajes confiable
- **Reconexiones automáticas** con backoff exponencial
- **Monitoreo** en tiempo real del servidor
- **Health checks** detallados
- **Logs rotativos** con múltiples niveles
- **Configuración** mediante variables de entorno

### Pruebas Pasadas ✅

- **Test comprehensive server**: 15/15 pruebas pasando
- **Conexiones WebSocket** estables
- **Entrega de mensajes** confiable
- **Modo doze** funcionando correctamente
- **Reconexiones JWT** operativas

### Archivos Obsoletos ⚠️

- `api.py` - **NO USAR** - Contiene endpoints REST obsoletos que causan errores 403

### Configuración de Producción

Para producción, configurar las siguientes variables de entorno:

```bash
export MINKA_ENV=production
export MINKA_JWT_SECRET="tu_clave_secreta_muy_segura"
export REDIS_HOST="tu_servidor_redis"
export MINKA_LOG_LEVEL=WARNING
export MINKA_HOST=0.0.0.0
export MINKA_PORT=5001
```

### Próximos Pasos Recomendados

1. **Documentar API WebSocket** con esquemas JSON detallados
2. **Implementar rate limiting** para prevenir spam
3. **Agregar métricas** de rendimiento (opcional)
4. **Crear Docker containers** para despliegue fácil
5. **Implementar HTTPS/WSS** para producción

---

*Documentación actualizada: Junio 2025*
*Versión del servidor: MinkaV2 (Redis-based)*
