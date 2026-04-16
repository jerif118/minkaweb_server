import tornado.web
import tornado.escape
import time
import logging
import datetime

# Importar funciones para acceso a Redis
from session import (
    init_redis_client, get_session, get_all_session_keys,
    get_client, get_all_client_keys, get_redis_metrics_summary
)

# Importar configuraciones
from config import (
    SERVER_PORT, SERVER_HOST
)

# Importar active_websockets desde websocket_handler
from handlers.websocket_handler import active_websockets
from handlers.state_model import (
    ConnectionState, ParticipationState, PresenceState
)

SCHEMA_VERSION = 1

class MainHandler(tornado.web.RequestHandler):
    """Manejador principal para la ruta raíz del servidor."""
    def get(self):
        self.write("Servidor Minka WebSockets V2 está operativo.")

class HealthHandler(tornado.web.RequestHandler):
    """Manejador para verificar el estado de salud del servidor."""
    async def get(self):
        health_data = {
            "status": "ok",
            "timestamp": time.time(),
            "schema_version": SCHEMA_VERSION,
            "server_info": {
                "version": "MinkaV2 (Redis-based)",
                "port": SERVER_PORT,
                "host": SERVER_HOST
            }
        }
        
        # Verificar conexión a Redis
        redis_ok = False
        redis_info = {}
        try:
            rc = await init_redis_client()
            if rc:
                await rc.ping()
                redis_ok = True
                # Obtener información adicional de Redis
                info = await rc.info()
                redis_info = {
                    "connected": True,
                    "version": info.get('redis_version', 'unknown'),
                    "used_memory": info.get('used_memory_human', 'unknown'),
                    "uptime": info.get('uptime_in_seconds', 0)
                }
        except Exception as e:
            logging.error(f"[HEALTH] Error de Redis: {e}")
            redis_info = {
                "connected": False,
                "error": str(e)
            }
        
        health_data["redis"] = redis_info
        rm = get_redis_metrics_summary()
        # Cálculo simple p90/p99 aproximado si hay latencias (requiere extender métricas para almacenar muestras en el futuro)
        health_data["redis_metrics"] = rm
        
        # Obtener estadísticas del servidor
        try:
            session_keys = await get_all_session_keys()
            client_keys = await get_all_client_keys()
            active_sessions = 0
            buckets = {
                'conn_connected': 0,
                'conn_disconnected': 0,
                'conn_closed': 0,
                'presence_doze': 0,
                'presence_foreground': 0,
                'presence_offline': 0,
                'participation_active': 0,
                'participation_waiting': 0,
                'participation_left': 0
            }
            for session_key in session_keys:
                session_data = await get_session(session_key)
                if session_data:
                    active_sessions += 1
            for client_key in client_keys:
                client_data = await get_client(client_key)
                if not client_data:
                    continue
                cs = client_data.get('connection_state')
                ps = client_data.get('participation_state')
                prs = client_data.get('presence_state')
                if cs == 'connected': buckets['conn_connected'] += 1
                elif cs == 'disconnected': buckets['conn_disconnected'] += 1
                elif cs == 'closed': buckets['conn_closed'] += 1
                if prs == 'doze': buckets['presence_doze'] += 1
                elif prs == 'foreground': buckets['presence_foreground'] += 1
                elif prs == 'offline': buckets['presence_offline'] += 1
                if ps == 'active': buckets['participation_active'] += 1
                elif ps == 'waiting_peer': buckets['participation_waiting'] += 1
                elif ps in ['left', 'removed', 'terminated']: buckets['participation_left'] += 1
            health_data["server_stats"] = {
                "active_sessions": active_sessions,
                **buckets,
                "active_websockets": len(active_websockets),
            }
        except Exception as e:
            logging.error(f"[HEALTH] Error obteniendo estadísticas: {e}")
            health_data["server_stats"] = {
                "error": f"No se pudieron obtener estadísticas: {str(e)}"
            }
        
        # Determinar el estado general
        if not redis_ok:
            health_data["status"] = "degraded"
            self.set_status(503)  # Service Unavailable
        
        self.write(health_data)

class MonitorHandler(tornado.web.RequestHandler):
    """Manejador para monitorear las sesiones y clientes activos."""
    def prepare(self):
        token = self.get_argument('token', None)
        expected = self.application.settings.get('monitor_token') if self.application else None
        if expected and token != expected:
            self.set_status(403)
            self.finish({"error": "Forbidden"})
    
    async def get(self):
        # Esta función debe ser asíncrona debido a las llamadas a Redis
        sessions = []
        clients = []
        try:
            session_keys = await get_all_session_keys()
            for room_id in session_keys:
                session_data = await get_session(room_id)
                if session_data:
                    sessions.append({room_id: session_data})
            
            client_keys = await get_all_client_keys()
            for client_id in client_keys:
                client_data = await get_client(client_id)
                if client_data:
                    # No mostrar JWTs completos en el monitor
                    if 'current_jti' in client_data: client_data['current_jti'] = "****"
                    clients.append({client_id: client_data})
        except Exception as e:
            logging.error(f"[MONITOR] Error al obtener datos de Redis: {e}")
            self.set_status(500)
            self.write({"error": "Error al contactar con Redis", "details": str(e)})
            return

        active_ws_info = []
        for client_id, ws_handler in active_websockets.items():
            active_ws_info.append({
                "client_id": client_id,
                "room_id": getattr(ws_handler, 'room_id', 'N/A'),
                "handler_class": ws_handler.__class__.__name__,
                "remote_ip": ws_handler.request.remote_ip
            })

        # Generar HTML para el monitor
        html = f"""
        <html>
        <head>
          <title>Monitor - Minka WebSocket Server</title>
          <meta charset="UTF-8">
          <meta http-equiv="refresh" content="30">
          <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            h1, h2 {{ color: #333; }}
            .stats {{ display: flex; gap: 20px; margin-bottom: 20px; }}
            .stat-card {{ background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .stat-number {{ font-size: 2em; font-weight: bold; color: #007bff; }}
            .stat-label {{ color: #666; font-size: 0.9em; }}
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
            th {{ background-color: #007bff; color: white; font-weight: bold; }}
            tr:nth-child(even) {{ background-color: #f8f9fa; }}
            .status-connected {{ color: #28a745; font-weight: bold; }}
            .status-dozing {{ color: #ffc107; font-weight: bold; }}
            .status-pending {{ color: #dc3545; font-weight: bold; }}
            .json-data {{ background: #f8f9fa; padding: 10px; border-radius: 4px; font-family: monospace; font-size: 0.9em; max-height: 200px; overflow-y: auto; }}
          </style>
        </head>
        <body>
          <div class="container">
            <h1>Monitor - Servidor WebSocket Minka</h1>
            <div class="stats">
              <div class="stat-card">
                <div class="stat-number">{len(sessions)}</div>
                <div class="stat-label">Sesiones Activas</div>
              </div>
              <div class="stat-card">
                <div class="stat-number">{len(clients)}</div>
                <div class="stat-label">Clientes Registrados</div>
              </div>
              <div class="stat-card">
                <div class="stat-number">{len(active_websockets)}</div>
                <div class="stat-label">WebSockets Conectados</div>
              </div>
            </div>

            <h2>Conexiones WebSocket Activas</h2>
            <table>
              <tr>
                <th>Client ID</th>
                <th>Room ID</th>
                <th>IP Remota</th>
                <th>Handler</th>
              </tr>"""
        
        for ws_info in active_ws_info:
            html += f"""
              <tr>
                <td>{tornado.escape.xhtml_escape(ws_info['client_id'])}</td>
                <td>{tornado.escape.xhtml_escape(str(ws_info['room_id']))}</td>
                <td>{tornado.escape.xhtml_escape(ws_info['remote_ip'])}</td>
                <td>{tornado.escape.xhtml_escape(ws_info['handler_class'])}</td>
              </tr>"""
        
        html += """
            </table>

            <h2>Sesiones (Salas)</h2>
            <table>
              <tr>
                <th>Room ID</th>
                <th>Clientes</th>
                <th>Última Actividad</th>
                <th>Detalles</th>
              </tr>"""
        
        for session_dict in sessions:
            for room_id, session_data in session_dict.items():
                clients_list = ", ".join(session_data.get('clients', []))
                last_activity = session_data.get('last_activity', 0)
                last_activity_str = datetime.datetime.fromtimestamp(last_activity).strftime('%Y-%m-%d %H:%M:%S') if last_activity else 'N/A'
                
                html += f"""
              <tr>
                <td>{tornado.escape.xhtml_escape(room_id)}</td>
                <td>{tornado.escape.xhtml_escape(clients_list)}</td>
                <td>{last_activity_str}</td>
                <td><details><summary>Ver datos</summary><div class="json-data">{tornado.escape.xhtml_escape(str(session_data))}</div></details></td>
              </tr>"""
        
        html += """
            </table>

            <h2>Clientes Registrados</h2>
            <table>
              <tr>
                <th>Client ID</th>
                <th>Room ID</th>
                <th>Estado</th>
                <th>Última Conexión</th>
                <th>Detalles</th>
              </tr>"""
        
        for client_dict in clients:
            for client_id, client_data in client_dict.items():
                room_id = client_data.get('room_id', 'N/A')
                # Determinar estado visual principal usando nuevos campos
                status = client_data.get('status') or client_data.get('connection_state') or 'unknown'
                cs = client_data.get('connection_state')
                ps = client_data.get('participation_state')
                prs = client_data.get('presence_state')
                composite = f"c:{cs}|p:{ps}|pr:{prs}"
                last_seen = client_data.get('last_seen', 0)
                last_seen_str = datetime.datetime.fromtimestamp(last_seen).strftime('%Y-%m-%d %H:%M:%S') if last_seen else 'N/A'
                status_class = ""
                if cs == 'connected' and prs == 'foreground':
                    status_class = "status-connected"
                elif prs == 'doze':
                    status_class = "status-dozing"
                elif cs == 'disconnected':
                    status_class = "status-pending"
                
                html += f"""
              <tr>
                <td>{tornado.escape.xhtml_escape(client_id)}</td>
                <td>{tornado.escape.xhtml_escape(room_id)}</td>
                <td class="{status_class}">{tornado.escape.xhtml_escape(status)}</td>
                <td>{last_seen_str}<br/><small>{tornado.escape.xhtml_escape(composite)}</small></td>
                <td><details><summary>Ver datos</summary><div class="json-data">{tornado.escape.xhtml_escape(str(client_data))}</div></details></td>
              </tr>"""
        
        html += """
            </table>
            <p><small>Actualización automática cada 30 segundos</small></p>
          </div>
        </body>
        </html>"""
        
        self.set_header("Content-Type", "text/html; charset=UTF-8")
        self.write(html)
