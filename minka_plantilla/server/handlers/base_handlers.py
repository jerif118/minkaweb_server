"""
NOTA DE DEPRECACIÓN:
Este archivo ha sido reemplazado por:
- handlers/base.py (clase base)
- handlers/http_handlers.py (implementaciones específicas)

Se mantiene temporalmente para compatibilidad, pero será eliminado en futuras versiones.
"""

# Re-exportar desde los nuevos módulos para mantener compatibilidad
from handlers.http_handlers import MainHandler, HealthHandler, MonitorHandler

# Advertir sobre el uso de este módulo
import warnings
warnings.warn(
    "El módulo base_handlers.py está obsoleto. Use handlers/http_handlers.py en su lugar.",
    DeprecationWarning,
    stacklevel=2
)
