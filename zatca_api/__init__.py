__version__ = '1.0.0'

APP_NAME = 'zatca_api'

# Public API surface. Import from here rather than reaching into submodules so
# internal layout can change without breaking callers.
from zatca_api.services.envelope import error_response, success_response  # noqa: E402
from zatca_api.services.zatca import get_zatca_details  # noqa: E402

__all__ = ['APP_NAME', '__version__', 'error_response', 'get_zatca_details', 'success_response']
