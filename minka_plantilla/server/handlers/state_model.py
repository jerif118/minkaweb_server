"""
Modelo unificado de estados para clientes y sesiones Minka.

Introduce tres dimensiones independientes para reemplazar el campo ambiguo "status" existente.
Permite migración incremental manteniendo compatibilidad derivando un "legacy_status".
Incluye helpers para normalización, validación y actualización eficiente (con cache local y pipeline opcional).

Ejes de estado:
- ConnectionState: ciclo de vida del socket actual.
- ParticipationState: relación lógica con la sala.
- PresenceState: disponibilidad / contexto de actividad del cliente.

Optimización Redis:
- Cache local TTL corta para lecturas muy frecuentes.
- Función update con pipeline opcional (si el caller pasa pipeline de aioredis) para agrupar escrituras.
- Opción optimistic (futuro: implementar WATCH/MULTI) preparada.

Esta capa NO elimina todavía el campo legacy 'status'; produce uno derivado para compatibilidad.

Fases de migración sugeridas (resumen):
F1: Añadir este módulo y empezar a usar normalización + nuevos campos al crear/actualizar clientes.
F2: Actualizar handlers para leer connection_state/participation_state/presence_state en vez de status.
F3: Eliminar escrituras directas a status; solo derivar legacy_status para clientes antiguos.
F4: Retirar por completo status después de validar.
"""
from __future__ import annotations

import enum
import time
import asyncio
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple

# Importaciones de capa de sesión (asincrónicas)
try:
    from session import get_client, set_client  # type: ignore
except Exception:  # pragma: no cover - durante pruebas unitarias sin módulo real
    async def get_client(_cid):
        return None
    async def set_client(_cid, _data):  # noqa
        return None

CACHE_TTL_SECONDS = 2.0  # TTL breve para aliviar ráfagas de lecturas (optimizable por env var)
CACHE_ENABLED = True

# Cache simple en memoria (no compartida entre procesos)
_client_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_cache_lock = asyncio.Lock()


@enum.unique
class ConnectionState(str, enum.Enum):
    connecting = "connecting"
    connected = "connected"
    disconnected = "disconnected"  # Socket perdido pero ventana de reconexión vigente
    closed = "closed"              # Cierre voluntario definitivo


@enum.unique
class ParticipationState(str, enum.Enum):
    joining = "joining"            # Handshake inicial (antes de JWT completo / join confirmado)
    waiting_peer = "waiting_peer"  # Sala creada esperando otro participante
    active = "active"              # Participando activamente
    left = "left"                  # Abandono voluntario
    removed = "removed"            # Expulsado / limpieza
    terminated = "terminated"      # Sala disuelta (estado terminal)


@enum.unique
class PresenceState(str, enum.Enum):
    foreground = "foreground"      # Interacción activa
    background = "background"      # App/ventana en background pero operativa
    doze = "doze"                  # Modo ahorro (latencia tolerada)
    offline = "offline"            # Fuera de ventanas de reconexión / no disponible


@dataclass
class ClientState:
    client_id: str
    room_id: Optional[str] = None
    connection_state: ConnectionState = ConnectionState.connected
    participation_state: ParticipationState = ParticipationState.active
    presence_state: PresenceState = PresenceState.foreground
    last_seen: float = field(default_factory=lambda: time.time())
    reconnect_deadline: Optional[float] = None
    doze_deadline: Optional[float] = None
    current_jti: Optional[str] = None
    is_initiator: bool = False
    capabilities: Dict[str, Any] = field(default_factory=dict)
    message_queue_len: int = 0
    version: int = 1
    legacy_status: Optional[str] = None  # Derivado
    state_compact: Optional[int] = None  # Opcional para analítica futura

    def to_dict(self) -> Dict[str, Any]:  # Serialización para Redis
        return {
            'client_id': self.client_id,
            'room_id': self.room_id,
            'connection_state': self.connection_state.value,
            'participation_state': self.participation_state.value,
            'presence_state': self.presence_state.value,
            'last_seen': self.last_seen,
            'reconnect_deadline': self.reconnect_deadline,
            'doze_deadline': self.doze_deadline,
            'current_jti': self.current_jti,
            'is_initiator': self.is_initiator,
            'capabilities': self.capabilities,
            'message_queue_len': self.message_queue_len,
            'version': self.version,
            'status': self.legacy_status,  # Mantener compatibilidad
            'state_compact': self.state_compact,
        }


# ----- Derivación de estado legacy -----
_LEGACY_PRIORITY = [
    (ConnectionState.connected, ParticipationState.waiting_peer, PresenceState.foreground, 'waiting_for_peer'),
    (ConnectionState.connected, ParticipationState.active, PresenceState.doze, 'doze'),
    (ConnectionState.disconnected, ParticipationState.active, PresenceState.foreground, 'pending_reconnect'),
    (ConnectionState.disconnected, ParticipationState.active, PresenceState.background, 'pending_reconnect'),
    (ConnectionState.disconnected, ParticipationState.active, PresenceState.doze, 'pending_reconnect'),
    (ConnectionState.connected, ParticipationState.active, PresenceState.foreground, 'active'),
    (ConnectionState.connected, ParticipationState.active, PresenceState.background, 'active_background'),
    (ConnectionState.closed, ParticipationState.left, PresenceState.offline, 'left'),
    (ConnectionState.disconnected, ParticipationState.left, PresenceState.offline, 'left'),
]

DEFAULT_LEGACY_FALLBACK = 'unknown'


def derive_legacy_status(connection_state: ConnectionState,
                         participation_state: ParticipationState,
                         presence_state: PresenceState) -> str:
    for c, p, pr, legacy in _LEGACY_PRIORITY:
        if c == connection_state and p == participation_state and pr == presence_state:
            return legacy
    # Reglas adicionales simples
    if participation_state in (ParticipationState.left, ParticipationState.removed):
        return 'left'
    if participation_state == ParticipationState.terminated:
        return 'terminated'
    return DEFAULT_LEGACY_FALLBACK


# ----- Normalización -----

def normalize_client_record(raw: Optional[Dict[str, Any]], defaults: Optional[Dict[str, Any]] = None) -> Optional[ClientState]:
    if raw is None:
        return None
    d = {**raw}
    defaults = defaults or {}

    def _enum(enum_cls, key, fallback):
        try:
            return enum_cls(d.get(key, defaults.get(key, fallback)))
        except Exception:
            return fallback

    connection_state = _enum(ConnectionState, 'connection_state', ConnectionState.connected)
    participation_state = _enum(ParticipationState, 'participation_state', ParticipationState.active)
    presence_state = _enum(PresenceState, 'presence_state', PresenceState.foreground)

    legacy_status = derive_legacy_status(connection_state, participation_state, presence_state)

    cs = ClientState(
        client_id=d.get('client_id') or defaults.get('client_id'),
        room_id=d.get('room_id'),
        connection_state=connection_state,
        participation_state=participation_state,
        presence_state=presence_state,
        last_seen=d.get('last_seen', time.time()),
        reconnect_deadline=d.get('reconnect_deadline'),
        doze_deadline=d.get('doze_deadline'),
        current_jti=d.get('current_jti'),
        is_initiator=d.get('is_initiator', False),
        capabilities=d.get('capabilities', {}),
        message_queue_len=d.get('message_queue_len', 0),
        version=d.get('version', 1),
        legacy_status=legacy_status,
        state_compact=compute_state_compact(connection_state, participation_state, presence_state)
    )
    return cs


# ----- Compactación opcional -----
# Bits: 0-1 connection (2 bits), 2-4 participation (3 bits), 5-6 presence (2 bits)
_CONNECTION_BITS = {c: i for i, c in enumerate(ConnectionState)}
_PARTICIPATION_BITS = {p: i for i, p in enumerate(ParticipationState)}
_PRESENCE_BITS = {p: i for i, p in enumerate(PresenceState)}

def compute_state_compact(c: ConnectionState, p: ParticipationState, pr: PresenceState) -> int:
    return (_CONNECTION_BITS[c]) | (_PARTICIPATION_BITS[p] << 2) | (_PRESENCE_BITS[pr] << 5)


# ----- Cache helpers -----
async def get_client_cached(client_id: str, force_refresh: bool = False, include_legacy: bool = True) -> Optional[ClientState]:
    if not CACHE_ENABLED:
        raw = await get_client(client_id)
        return normalize_client_record(raw)
    now = time.time()
    async with _cache_lock:
        if not force_refresh and client_id in _client_cache:
            ts, data = _client_cache[client_id]
            if now - ts <= CACHE_TTL_SECONDS:
                return normalize_client_record(data)
    raw = await get_client(client_id)
    if raw is None:
        async with _cache_lock:
            _client_cache.pop(client_id, None)
        return None
    # Derivar legacy si se pidió explicitamente; normalización ya lo hace
    async with _cache_lock:
        _client_cache[client_id] = (now, raw)
    return normalize_client_record(raw)


def invalidate_client_cache(client_id: str):
    _client_cache.pop(client_id, None)


def clear_cache():  # pragma: no cover
    _client_cache.clear()


# ----- Actualización de estados -----
async def update_client_states(client_id: str,
                               *,
                               connection_state: Optional[ConnectionState] = None,
                               participation_state: Optional[ParticipationState] = None,
                               presence_state: Optional[PresenceState] = None,
                               touch_last_seen: bool = True,
                               extra_updates: Optional[Dict[str, Any]] = None,
                               pipeline=None,
                               optimistic: bool = False) -> Optional[ClientState]:
    """
    Actualiza estados del cliente de forma atómica lógica (no usa todavía WATCH/MULTI).
    - Lee registro (cache -> Redis)
    - Aplica cambios
    - Persiste
    - Invalida / refresca cache
    Retorna el estado normalizado actualizado.
    """
    current = await get_client_cached(client_id)
    if current is None:
        # Crear base si no existe (útil en flujos de join tempranos)
        current = ClientState(client_id=client_id, participation_state=ParticipationState.joining)

    changed = False
    if connection_state and connection_state != current.connection_state:
        current.connection_state = connection_state
        changed = True
    if participation_state and participation_state != current.participation_state:
        current.participation_state = participation_state
        changed = True
    if presence_state and presence_state != current.presence_state:
        current.presence_state = presence_state
        changed = True

    if touch_last_seen:
        current.last_seen = time.time()
        changed = True

    if extra_updates:
        for k, v in extra_updates.items():
            setattr(current, k, v)
            changed = True

    # Recalcular derivados
    if changed:
        current.legacy_status = derive_legacy_status(current.connection_state, current.participation_state, current.presence_state)
        current.state_compact = compute_state_compact(current.connection_state, current.participation_state, current.presence_state)
        current.version += 1

        data = current.to_dict()
        # Escritura (pipeline opcional)
        if pipeline is not None:
            from config import CLIENT_KEY_PREFIX, SESSION_TIMEOUT as _SESSION_TTL
            import json as _json
            key = f"{CLIENT_KEY_PREFIX}{client_id}"
            # Escritura parcial no destructiva
            for f, v in data.items():
                if isinstance(v, (dict, list)):
                    pipeline.hset(key, f, _json.dumps(v))
                else:
                    pipeline.hset(key, f, v)
            pipeline.expire(key, _SESSION_TTL)
        else:
            await set_client(client_id, data)
        invalidate_client_cache(client_id)
    return current


# ----- Validación de transiciones (básica) -----
_ALLOWED_CONN_TRANSITIONS = {
    ConnectionState.connecting: {ConnectionState.connected, ConnectionState.closed},
    ConnectionState.connected: {ConnectionState.disconnected, ConnectionState.closed},
    ConnectionState.disconnected: {ConnectionState.connected, ConnectionState.closed},
    ConnectionState.closed: set(),
}

_ALLOWED_PART_TRANSITIONS = {
    ParticipationState.joining: {ParticipationState.waiting_peer, ParticipationState.active, ParticipationState.left},
    ParticipationState.waiting_peer: {ParticipationState.active, ParticipationState.left, ParticipationState.terminated},
    ParticipationState.active: {ParticipationState.left, ParticipationState.removed, ParticipationState.terminated},
    ParticipationState.left: set(),
    ParticipationState.removed: set(),
    ParticipationState.terminated: set(),
}

_ALLOWED_PRESENCE_TRANSITIONS = {
    PresenceState.foreground: {PresenceState.background, PresenceState.doze, PresenceState.offline},
    PresenceState.background: {PresenceState.foreground, PresenceState.doze, PresenceState.offline},
    PresenceState.doze: {PresenceState.foreground, PresenceState.background, PresenceState.offline},
    PresenceState.offline: {PresenceState.foreground},  # Solo reconexión explícita
}


def validate_transition(current: ClientState,
                        new_connection: Optional[ConnectionState] = None,
                        new_participation: Optional[ParticipationState] = None,
                        new_presence: Optional[PresenceState] = None) -> None:
    if new_connection and new_connection != current.connection_state:
        if new_connection not in _ALLOWED_CONN_TRANSITIONS[current.connection_state]:
            raise ValueError(f"Transición de connection_state inválida {current.connection_state} -> {new_connection}")
    if new_participation and new_participation != current.participation_state:
        if new_participation not in _ALLOWED_PART_TRANSITIONS[current.participation_state]:
            raise ValueError(f"Transición de participation_state inválida {current.participation_state} -> {new_participation}")
    if new_presence and new_presence != current.presence_state:
        if new_presence not in _ALLOWED_PRESENCE_TRANSITIONS[current.presence_state]:
            raise ValueError(f"Transición de presence_state inválida {current.presence_state} -> {new_presence}")


__all__ = [
    'ConnectionState', 'ParticipationState', 'PresenceState',
    'ClientState',
    'normalize_client_record', 'derive_legacy_status', 'compute_state_compact',
    'get_client_cached', 'invalidate_client_cache', 'clear_cache',
    'update_client_states', 'validate_transition'
]
