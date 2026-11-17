"""Sanitized application/storage failure mapping shared by all HTTP routes."""
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from workbench_agent.application.errors import DomainError
from workbench_agent.persistence.errors import StorageError

def install_error_handlers(app):
    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse(dict(code=error.code,**({'links':error.links} if hasattr(error,'links') else {})), status_code=error.status)

    @app.exception_handler(StorageError)
    async def storage_error(request, error):
        return JSONResponse({'code': error.code}, status_code=409 if error.code == 'UNIQUE_CONFLICT' else 503)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({'code': 'INVALID_REQUEST'}, status_code=422)
