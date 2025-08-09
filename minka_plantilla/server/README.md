


## 1. Conexión al Servidor

La comunicación comienza cuando un cliente establece una conexión WebSocket con el servidor.

**URL del WebSocket:**
`ws://<host>:<port>/ws` (o `wss://` para conexiones seguras)

**Parámetros de Consulta en la URL de Conexión:**

*   **`client_id`**
    *   **Obligatorio**: Sí.
    *   **Descripción**: Un identificador único generado por el cliente para esta conexión. El servidor lo utiliza para rastrear al cliente a través de diferentes conexiones (especialmente con JWT).
    *   **Formato**: Debe comenzar con `web-` para clientes web o `mobile-` para clientes móviles, seguido de una cadena única.
    *   **Ejemplo**: `client_id=mobile-abcdef1234567890`

*   **`action`**
    *   **Obligatorio**: Sí, a menos que se proporcione `jwt_token`.
    *   **Descripción**: Define la intención principal del cliente al conectarse.
    *   **Valores Posibles**:
        *   `create`: El cliente desea crear una nueva sala de chat. El servidor generará un ID de sala y una contraseña.
        *   `join`: El cliente desea unirse a una sala de chat existente. Requiere `room_id` y `room_password`.

*   **`room_id`**
    *   **Obligatorio**: Sí, si `action=join`.
    *   **Descripción**: El identificador único de la sala a la que el cliente intenta unirse.
    *   **Ejemplo**: `room_id=id-a1b2c3d4e5f6`

*   **`room_password`**
    *   **Obligatorio**: Sí, si `action=join`.
    *   **Descripción**: La contraseña requerida para acceder a la sala especificada por `room_id`.
    *   **Ejemplo**: `room_password=S3CR3T`

*   **`jwt_token`**
    *   **Obligatorio**: No. Si se proporciona, los parámetros `action`, `room_id`, y `room_password` se ignoran.
    *   **Descripción**: Un JSON Web Token previamente emitido por el servidor. El cliente lo usa para autenticarse y reanudar una sesión anterior (por ejemplo, después de una desconexión o al salir del modo "doze"). El servidor validará el token y, si es válido, intentará restaurar al cliente a su sala y estado anteriores.
    *   **Ejemplo**: `jwt_token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c`

## 2. Mensajes del Cliente al Servidor (Client-to-Server)

Una vez la conexión WebSocket está abierta, los clientes envían mensajes JSON al servidor para interactuar.

### 2.1. Enviar Mensaje de Chat

*   **Propósito**: Permite a un cliente enviar un mensaje de chat a la otra parte conectada en la misma sala.
*   **Estructura JSON**:
    ```json
    {
      "message": "Este es el contenido del mensaje que quiero enviar.",
      "message_type": "text"
    }
    ```
    *   `message`: (String u Objeto JSON) El contenido real del mensaje. Puede ser texto plano o una estructura JSON más compleja si `message_type` lo indica.
    *   `message_type`: (String, opcional, por defecto "text") Especifica el tipo de contenido en el campo `message`. Ejemplos: `"text"`, `"image_url"`, `"payment_notification"`, `"location_update"`. El cliente receptor debe estar preparado para interpretar este tipo.
*   **Acción del Servidor**:
    1.  Identifica la sala del cliente remitente.
    2.  Identifica al cliente destinatario (el otro par en la sala).
    3.  Si el destinatario está conectado activamente, el servidor le reenvía el mensaje (ver evento `new_message`).
    4.  Si el destinatario no está conectado pero tiene una sesión activa (ej. en modo "doze" o `pending_reconnect`), el servidor encola el mensaje para su entrega posterior.
    5.  Envía una confirmación al remitente (ver evento `message_sent_confirmation`).

### 2.2. Acciones Específicas del Cliente

Los clientes pueden realizar acciones específicas enviando un objeto JSON con un campo `action`.

#### 2.2.1. Dejar la Sala (`leave`)

*   **Propósito**: Informar al servidor que el cliente desea desconectarse activamente de la sala actual y cerrar su participación en la sesión.
*   **Estructura JSON**:
    ```json
    {
      "action": "leave",
      "reason": "user_request"
    }
    ```
    *   `action`: (String) Siempre `"leave"`.
    *   `reason`: (String, opcional) Una breve descripción del motivo por el cual el cliente abandona. Ejemplos: `"user_request"`, `"client_shutdown"`, `"navigation_away"`.
*   **Acción del Servidor**:
    1.  Elimina al cliente de la sesión activa en la sala.
    2.  Notifica al otro par en la sala, si existe (ver evento `peer_left_room`).
    3.  Envía una confirmación al cliente que solicitó salir (ver evento `left_room`).
    4.  Actualiza el estado del cliente en Redis (puede marcarlo como desconectado o eliminarlo, dependiendo de la lógica de persistencia).
    5.  Si la sala queda vacía, puede marcarla para su eventual eliminación por la tarea de limpieza.
    6.  El servidor puede cerrar la conexión WebSocket del cliente después de procesar esta acción.

#### 2.2.2. Iniciar Modo Doze (`doze_start`)

*   **Propósito**: Utilizado principalmente por clientes móviles para indicar que la aplicación está entrando en un estado de bajo consumo (ej. pantalla apagada, app en segundo plano). El cliente espera desconectarse temporalmente pero desea que su sesión y mensajes pendientes se conserven para una reconexión posterior.
*   **Estructura JSON**:
    ```json
    {
      "action": "doze_start"
    }
    ```
    *   `action`: (String) Siempre `"doze_start"`.
*   **Acción del Servidor**:
    1.  Actualiza el estado del cliente en Redis a "dozing".
    2.  Conserva el JWT del cliente y su asociación con la sala.
    3.  Envía un acuse de recibo al cliente (ver evento `doze_mode_acknowledged`).
    4.  El servidor cerrará la conexión WebSocket actual del cliente poco después de enviar el acuse. Los mensajes que lleguen para este cliente mientras está en "doze" serán encolados.
    5.  Para salir del modo doze, el cliente debe establecer una nueva conexión WebSocket utilizando su `jwt_token` guardado.

#### 2.2.3. Ping Personalizado (`ping_custom`)

*   **Propósito**: Un mecanismo de "heartbeat" a nivel de aplicación. Permite al cliente verificar activamente que la conexión WebSocket sigue viva y que el servidor está respondiendo, más allá de los pings a nivel de protocolo WebSocket.
*   **Estructura JSON**:
    ```json
    {
      "action": "ping_custom"
    }
    ```
    *   `action`: (String) Siempre `"ping_custom"`.
*   **Acción del Servidor**:
    1.  Responde inmediatamente al cliente con un evento `pong_custom` (ver evento `pong_custom`).

## 3. Eventos del Servidor al Cliente (Server-to-Client)

El servidor envía mensajes JSON al cliente para notificar diversos eventos. Todos los mensajes del servidor suelen tener un campo `event` que describe el tipo de evento.

### 3.1. Eventos de Gestión de Sala

#### 3.1.1. `room_created`

*   **Propósito**: Enviado exclusivamente al cliente que solicitó la creación de una nueva sala (usando `action=create` en la conexión). Confirma que la sala ha sido creada exitosamente y proporciona las credenciales necesarias.
*   **Cuándo se envía**: Inmediatamente después de que el servidor procesa una solicitud de creación de sala exitosa.
*   **Payload JSON**:
    ```json
    {
      "event": "room_created",
      "room_id": "id-generado-para-la-sala-e5f6a1b2",
      "password": "CONTRASEÑAgenerada"
    }
    ```
    *   `event`: (String) Siempre `"room_created"`.
    *   `room_id`: (String) El identificador único generado por el servidor para la nueva sala.
    *   `password`: (String) La contraseña generada por el servidor para esta sala. El cliente debe compartir estas credenciales con el otro par para que pueda unirse.
*   **Acción del Cliente**: El cliente (generalmente el cliente web que inicia) debe mostrar `room_id` y `password` al usuario para que pueda compartirlos con el cliente móvil. El cliente también debe guardar estas credenciales si planea interactuar más con la sala.

#### 3.1.2. `joined_room`

*   **Propósito**: Confirma al cliente que se ha unido exitosamente a la sala especificada (ya sea una sala recién creada por él o una existente a la que se unió con `action=join`).
*   **Cuándo se envía**: Después de que el servidor procesa una solicitud de `action=create` o `action=join` exitosa.
*   **Payload JSON**:
    ```json
    {
      "event": "joined_room",
      "room_id": "id-de-la-sala-e5f6a1b2",
      "peer_client_id": "mobile-0987fedcba", // O null si no hay otro par aún
      "messages": [ // Array de mensajes pendientes para este cliente
        {
          "sender_id": "mobile-0987fedcba",
          "message_content": "Mensaje que me perdí",
          "message_type": "text",
          "timestamp": 1678886400000
        }
      ]
    }
    ```
    *   `event`: (String) Siempre `"joined_room"`.
    *   `room_id`: (String) El ID de la sala a la que el cliente se ha unido.
    *   `peer_client_id`: (String o `null`) El `client_id` del otro participante en la sala, si ya está presente. Si es `null`, el cliente está esperando al otro par.
    *   `messages`: (Array de Objetos) Una lista de mensajes que estaban encolados para este cliente y que se entregan al unirse/reconectarse. Cada objeto mensaje tiene la estructura del evento `new_message`.
*   **Acción del Cliente**: Actualizar su estado interno para reflejar que está en la sala. Si `peer_client_id` está presente, puede indicar que el chat está listo. Procesar y mostrar cualquier mensaje pendiente del array `messages`.

#### 3.1.3. `left_room`

*   **Propósito**: Confirma al cliente que ha sido eliminado de la sala. Esto puede ser en respuesta a una solicitud `leave` del propio cliente o debido a otras razones (ej. sesión expirada).
*   **Cuándo se envía**: Después de que el cliente envía una acción `leave`, o si el servidor elimina al cliente de la sala por otras razones (ej. limpieza de sesión).
*   **Payload JSON**:
    ```json
    {
      "event": "left_room",
      "reason": "user_request" // Ej: "user_request", "session_expired", "kicked_by_admin" (si se implementara)
    }
    ```
    *   `event`: (String) Siempre `"left_room"`.
    *   `reason`: (String) Indica por qué el cliente fue eliminado de la sala.
*   **Acción del Cliente**: Actualizar su UI para reflejar que ya no está en la sala. Puede limpiar el estado relacionado con la sala. La conexión WebSocket podría cerrarse poco después por el servidor.

### 3.2. Eventos de Conexión y JWT

#### 3.2.1. `jwt_updated`

*   **Propósito**: Proporciona al cliente un JSON Web Token (JWT) nuevo o actualizado. Este token es crucial para la gestión de la sesión y permite al cliente reconectarse sin tener que pasar por el proceso completo de creación/unión de sala si su conexión se interrumpe.
*   **Cuándo se envía**:
    *   Después de que un cliente se une exitosamente a una sala (`joined_room`).
    *   Después de una reconexión exitosa (`reconnected`).
    *   Potencialmente en otros momentos si el servidor necesita refrescar el token.
*   **Payload JSON**:
    ```json
    {
      "event": "jwt_updated",
      "jwt_token": "nuevo.jwt.token.codificadoEnBase64"
    }
    ```
    *   `event`: (String) Siempre `"jwt_updated"`.
    *   `jwt_token`: (String) El JWT. Contiene información sobre el `client_id`, `room_id`, y un tiempo de expiración.
*   **Acción del Cliente**: El cliente DEBE almacenar este `jwt_token` de forma segura (ej. en localStorage, SharedPreferences). Lo usará para futuras reconexiones pasándolo como parámetro `jwt_token` en la URL de conexión WebSocket.

#### 3.2.2. `reconnected`

*   **Propósito**: Confirma al cliente que ha reanudado exitosamente una sesión anterior utilizando un `jwt_token`.
*   **Cuándo se envía**: Después de que un cliente establece una conexión WebSocket proporcionando un `jwt_token` válido y el servidor logra restaurar su sesión.
*   **Payload JSON**:
    ```json
    {
      "event": "reconnected",
      "room_id": "id-de-la-sala-e5f6a1b2",
      "peer_client_id": "web-fedcba0987", // O null si el par no está o se fue
      "messages": [ // Array de mensajes pendientes para este cliente
        {
          "sender_id": "web-fedcba0987",
          "message_content": "Otro mensaje que me perdí mientras estaba desconectado",
          "message_type": "text",
          "timestamp": 1678886500000
        }
      ]
    }
    ```
    *   `event`: (String) Siempre `"reconnected"`.
    *   `room_id`: (String) El ID de la sala a la que el cliente ha sido reconectado.
    *   `peer_client_id`: (String o `null`) El `client_id` del otro participante en la sala.
    *   `messages`: (Array de Objetos) Mensajes encolados para el cliente durante su desconexión.
*   **Acción del Cliente**: Similar a `joined_room`. Restaurar el estado de la aplicación a la sala reconectada. Procesar mensajes pendientes. El servidor también enviará un `jwt_updated` poco después para refrescar el token.

### 3.3. Eventos de Mensajería

#### 3.3.1. `new_message`

*   **Propósito**: Entrega un mensaje de chat enviado por el otro par en la sala al cliente actual.
*   **Cuándo se envía**: Cuando el otro participante en la sala envía un mensaje de chat y el cliente actual está conectado.
*   **Payload JSON**:
    ```json
    {
      "event": "new_message",
      "sender_id": "mobile-0987fedcba", // ID del cliente que envió el mensaje
      "message_content": "¡Hola desde el móvil!", // Contenido del mensaje
      "message_type": "text", // Tipo de mensaje
      "timestamp": 1678886600000 // Timestamp del servidor (opcional, pero útil)
    }
    ```
    *   `event`: (String) Siempre `"new_message"`.
    *   `sender_id`: (String) El `client_id` del remitente del mensaje.
    *   `message_content`: (String u Objeto JSON) El contenido del mensaje, según lo enviado por el remitente.
    *   `message_type`: (String) El tipo de mensaje, según lo especificado por el remitente.
    *   `timestamp`: (Number, opcional) Un timestamp (ej. Unix epoch en milisegundos) de cuándo el servidor procesó el mensaje. Útil para ordenar mensajes.
*   **Acción del Cliente**: Mostrar el mensaje recibido en la interfaz de chat, atribuyéndolo al `sender_id`.

#### 3.3.2. `message_sent_confirmation`

*   **Propósito**: Proporciona retroalimentación al cliente que envió un mensaje, indicando el estado de su entrega.
*   **Cuándo se envía**: Después de que un cliente envía un mensaje de chat y el servidor lo ha procesado.
*   **Payload JSON**:
    ```json
    {
      "event": "message_sent_confirmation",
      "original_message_id": "id-opcional-del-mensaje-cliente", // Si el cliente envió un ID
      "recipients_delivered_directly": ["web-fedcba0987"],
      "recipients_queued": []
    }
    ```
    *   `event`: (String) Siempre `"message_sent_confirmation"`.
    *   `original_message_id`: (String, opcional) Si el cliente incluyó un ID único en su mensaje original, el servidor puede devolverlo aquí para que el cliente pueda asociar la confirmación con un mensaje específico.
    *   `recipients_delivered_directly`: (Array de Strings) Lista de `client_id`s de los destinatarios a los que el mensaje se entregó directamente (estaban conectados).
    *   `recipients_queued`: (Array de Strings) Lista de `client_id`s de los destinatarios para los cuales el mensaje fue encolado (no estaban conectados activamente, ej. en modo "doze").
*   **Acción del Cliente**: Puede usar esta información para actualizar la UI (ej. mostrar una marca de "entregado" o "enviado"). Si `recipients_queued` no está vacío, el cliente sabe que el mensaje no ha sido leído aún por todos.

### 3.4. Eventos de Estado del Par (Peer)

Estos eventos informan a un cliente sobre cambios en el estado o presencia de la otra parte en la sala.

#### 3.4.1. `peer_joined`

*   **Propósito**: Notifica a un cliente que ya está en una sala (posiblemente esperando) que otro participante (el "par") se ha unido a la misma sala.
*   **Cuándo se envía**: Cuando un segundo cliente se une exitosamente a una sala donde ya hay un cliente. Se envía al cliente original.
*   **Payload JSON**:
    ```json
    {
      "event": "peer_joined",
      "peer_client_id": "mobile-0987fedcba" // ID del nuevo cliente que se unió
    }
    ```
    *   `event`: (String) Siempre `"peer_joined"`.
    *   `peer_client_id`: (String) El `client_id` del nuevo participante.
*   **Acción del Cliente**: Actualizar la UI para indicar que el otro par está ahora conectado. Puede habilitar la funcionalidad de chat.

#### 3.4.2. `peer_left_room`

*   **Propósito**: Notifica a un cliente que el otro participante en la sala la ha abandonado.
*   **Cuándo se envía**: Cuando el otro cliente en la sala envía una acción `leave`, su conexión se cierra inesperadamente, o es eliminado por el servidor (ej. por expiración de reconexión).
*   **Payload JSON**:
    ```json
    {
      "event": "peer_left_room",
      "peer_client_id": "mobile-0987fedcba", // ID del cliente que se fue
      "reason": "user_request" // Motivo por el que se fue
    }
    ```
    *   `event`: (String) Siempre `"peer_left_room"`.
    *   `peer_client_id`: (String) El `client_id` del participante que abandonó la sala.
    *   `reason`: (String) El motivo por el cual el par abandonó (ej. `"user_request"`, `"connection_lost"`, `"reconnect_failed"`).
*   **Acción del Cliente**: Actualizar la UI para indicar que el otro par se ha desconectado. Puede deshabilitar la entrada de chat y mostrar un mensaje apropiado. El cliente actual permanece en la sala, esperando una posible reconexión del par o un nuevo par (si la lógica del servidor lo permite).

#### 3.4.3. `peer_resumed`

*   **Propósito**: Notifica a un cliente que su par, que previamente estaba en modo "doze" o desconectado temporalmente, ha reanudado la sesión (se ha reconectado exitosamente).
*   **Cuándo se envía**: Cuando un cliente se reconecta usando JWT y su par todavía está en la sala. Se envía al par que estaba esperando.
*   **Payload JSON**:
    ```json
    {
      "event": "peer_resumed",
      "peer_client_id": "mobile-0987fedcba" // ID del cliente que reanudó
    }
    ```
    *   `event`: (String) Siempre `"peer_resumed"`.
    *   `peer_client_id`: (String) El `client_id` del participante que ha reanudado su sesión.
*   **Acción del Cliente**: Actualizar la UI para indicar que el par está nuevamente activo. Similar a `peer_joined`.

#### 3.4.4. `peer_reconnect_failed`

*   **Propósito**: Informa a un cliente que su par, que estaba en estado `pending_reconnect`, no logró reconectarse dentro del período de gracia (`RECONNECT_GRACE_PERIOD`).
*   **Cuándo se envía**: Cuando la tarea de limpieza del servidor (`cleanup_sessions`) determina que un cliente en `pending_reconnect` ha excedido su tiempo de gracia y lo elimina. Se envía al otro cliente que aún está en la sala.
*   **Payload JSON**:
    ```json
    {
      "event": "peer_reconnect_failed",
      "client_id": "mobile-0987fedcba" // ID del par que no pudo reconectarse
    }
    ```
    *   `event`: (String) Siempre `"peer_reconnect_failed"`.
    *   `client_id`: (String) El `client_id` del par cuya reconexión falló.
*   **Acción del Cliente**: Similar a `peer_left_room`, pero con un contexto más específico. El cliente sabe que el par no volverá bajo esa sesión. Puede mostrar un mensaje como "El otro usuario no pudo reconectarse."

### 3.5. Eventos de Modo Doze

#### 3.5.1. `doze_mode_acknowledged`

*   **Propósito**: Confirmación del servidor de que ha recibido y procesado la solicitud `doze_start` del cliente.
*   **Cuándo se envía**: Inmediatamente después de que un cliente envía una acción `doze_start` y el servidor actualiza su estado.
*   **Payload JSON**:
    ```json
    {
      "event": "doze_mode_acknowledged"
    }
    ```
    *   `event`: (String) Siempre `"doze_mode_acknowledged"`.
*   **Acción del Cliente**: El cliente sabe que el servidor ha reconocido su intención de entrar en modo doze. El cliente puede esperar que el servidor cierre la conexión WebSocket actual poco después. El cliente no debe enviar más mensajes por esta conexión.

### 3.6. Eventos de Error y Otros

#### 3.6.1. `error`

*   **Propósito**: Enviado cuando ocurre un error del lado del servidor en respuesta a una acción del cliente o debido a un problema interno.
*   **Cuándo se envía**: Cuando una solicitud del cliente no puede ser procesada, las credenciales son inválidas, se violan las reglas del servidor, o ocurre un error inesperado.
*   **Payload JSON**:
    ```json
    {
      // "event": "error", // El campo 'event' puede estar presente o no
      "error": "Descripción legible del error.",
      "code": "CODIGO_DEL_ERROR_ESPECIFICO"
    }
    ```
    *   `error`: (String) Una descripción del error para el desarrollador o, a veces, para el usuario.
    *   `code`: (String) Un código de error programático que el cliente puede usar para manejar errores específicos.
*   **Códigos de Error Comunes (`code`):**
    *   `INVALID_ROOM_CREDENTIALS`: El `room_id` o `room_password` proporcionados al intentar unirse son incorrectos o la sala no existe.
    *   `ROOM_FULL`: La sala a la que se intenta unir ya tiene el número máximo de participantes (generalmente 2).
    *   `INVALID_ACTION`: El campo `action` en un mensaje del cliente no es reconocido o no es aplicable en el estado actual.
    *   `MISSING_PARAMETERS`: Faltan parámetros requeridos en la URL de conexión o en un mensaje JSON.
    *   `JWT_INVALID`: El `jwt_token` proporcionado es inválido, ha expirado o no se puede verificar.
    *   `JWT_CLIENT_MISMATCH`: El `client_id` en el JWT no coincide con el `client_id` proporcionado en la conexión.
    *   `SESSION_NOT_FOUND`: No se pudo encontrar una sesión activa para el cliente (ej. al intentar reconectar con un JWT a una sesión que ya fue limpiada).
    *   `CLIENT_NOT_FOUND`: No se pudieron encontrar los datos del cliente en Redis.
    *   `SERVER_ERROR`: Un error interno genérico en el servidor.
    *   `INVALID_CLIENT_ID_FORMAT`: El `client_id` no sigue el formato esperado (ej. no empieza con `web-` o `mobile-`).
*   **Acción del Cliente**: Mostrar un mensaje de error apropiado al usuario. Registrar el error para depuración. Dependiendo del error, el cliente puede necesitar reintentar la acción, solicitar nuevas credenciales, o abandonar. El servidor puede cerrar la conexión después de enviar un error crítico.

#### 3.6.2. `pong_custom`

*   **Propósito**: La respuesta del servidor a un mensaje `ping_custom` enviado por el cliente.
*   **Cuándo se envía**: Inmediatamente después de recibir un mensaje `{"action": "ping_custom"}`.
*   **Payload JSON**:
    ```json
    {
      "event": "pong_custom"
    }
    ```
    *   `event`: (String) Siempre `"pong_custom"`.
*   **Acción del Cliente**: Si el cliente está rastreando los tiempos de ping/pong, puede registrar la recepción de este mensaje para confirmar la conectividad.

#### 3.6.3. Mensaje de Sesión Expirada (y cierre de conexión)

*   **Propósito**: Informa al cliente que su sesión ha sido terminada por el servidor, generalmente debido a inactividad prolongada o como parte de la tarea de limpieza del servidor.
*   **Cuándo se envía**: Cuando la tarea `cleanup_sessions` del servidor determina que una sesión o cliente debe ser eliminado y el cliente afectado todavía tiene una conexión WebSocket activa.
*   **Payload JSON**:
    ```json
    {
      "info": "Su sesión ha expirado debido a inactividad.",
      "code": "SESSION_EXPIRED"
    }
    ```
    *   `info`: (String) Un mensaje informativo para el usuario.
    *   `code`: (String) Un código programático, siempre `"SESSION_EXPIRED"`.
*   **Acción del Cliente**: Informar al usuario que su sesión ha terminado. Limpiar cualquier estado 