import tornado.web
import logging
import json
import time

class BaseHandler(tornado.web.RequestHandler):
    """
    Clase base para todos los handlers HTTP.
    Proporciona métodos comunes y funcionalidad compartida.
    """
    
    def initialize(self):
        """Inicializa atributos comunes para todos los handlers."""
        self.request_start_time = time.time()
    
    def prepare(self):
        """Se ejecuta antes de cada solicitud."""
        self.log_request_start()
    
    def on_finish(self):
        """Se ejecuta después de completar cada solicitud."""
        self.log_request_end()
    
    def log_request_start(self):
        """Registra el inicio de la solicitud."""
        logging.debug(f"[REQUEST-START] {self.request.method} {self.request.uri} - IP: {self.request.remote_ip}")
    
    def log_request_end(self):
        """Registra el final de la solicitud con el tiempo transcurrido."""
        duration = time.time() - self.request_start_time
        logging.debug(f"[REQUEST-END] {self.request.method} {self.request.uri} - Duración: {duration:.4f}s")
    
    def write_error(self, status_code, **kwargs):
        """Personaliza la respuesta de error para devolver JSON o HTML según el tipo de cliente."""
        self.set_header('Content-Type', 'application/json')
        
        error_data = {
            'status': 'error',
            'code': status_code,
            'message': self._reason
        }
        
        # Incluir detalles de la excepción en modo debug
        if self.settings.get("serve_traceback") and "exc_info" in kwargs:
            import traceback
            error_data['traceback'] = traceback.format_exception(*kwargs["exc_info"])
        
        self.finish(json.dumps(error_data))
    
    def send_json_response(self, data, status_code=200):
        """
        Envía una respuesta JSON con el código de estado especificado.
        
        Args:
            data: Datos a convertir a JSON
            status_code: Código de estado HTTP (default: 200)
        """
        self.set_header('Content-Type', 'application/json')
        self.set_status(status_code)
        self.write(json.dumps(data))
    
    def send_error_response(self, message, code='ERROR', status_code=400):
        """
        Envía una respuesta de error en formato JSON.
        
        Args:
            message: Mensaje de error
            code: Código de error específico de la aplicación
            status_code: Código de estado HTTP
        """
        error_data = {
            'status': 'error',
            'message': message,
            'code': code,
            'timestamp': time.time()
        }
        self.send_json_response(error_data, status_code)
