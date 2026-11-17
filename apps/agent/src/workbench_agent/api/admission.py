"""Pure ASGI readiness gate: no hidden streaming buffers or proxy send task."""
from fastapi.responses import JSONResponse
from workbench_agent.persistence.readiness import storage_ready
class StorageAdmissionMiddleware:
    def __init__(self,app,backend,settings): self.app,self.backend,self.settings=app,backend,settings
    async def __call__(self,scope,receive,send):
        if scope['type']=='http' and scope['path'].startswith('/v1/') and not await storage_ready(self.settings,self.backend):
            return await JSONResponse({'code':'STORAGE_UNAVAILABLE'},status_code=503)(scope,receive,send)
        return await self.app(scope,receive,send)
