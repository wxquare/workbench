"""Serve the built SPA from the configured asset root; never fall back API paths."""
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from workbench_agent.runtime.graphs.ids import ALL_GRAPH_IDS

router = APIRouter()


@router.get('/app', include_in_schema=False)
@router.get('/app/{path:path}', include_in_schema=False)
async def frontend(path: str = '', *, request: Request):
    settings = request.app.state.settings
    root = (settings.frontend_dist_root or (Path(settings.asset_root) / 'web' / 'dist')).resolve()
    index = (root / 'index.html').resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status_code=404)
    if path.startswith('graphs/') and path != 'graphs/' and path[7:] not in ALL_GRAPH_IDS:
        raise HTTPException(status_code=404)
    if path and candidate.is_file():
        return FileResponse(candidate)
    graph_detail = path.startswith('graphs/') and path[7:] in ALL_GRAPH_IDS
    # Graph IDs contain dots; every other file-looking request stays outside the SPA fallback.
    if path and (path.startswith('assets/') or (Path(path).suffix and not graph_detail) or 'text/html' not in request.headers.get('accept', '')):
        raise HTTPException(status_code=404)
    if not index.is_relative_to(root) or not index.is_file():
        raise HTTPException(status_code=503, detail='Frontend build unavailable')
    return FileResponse(index, headers={'Cache-Control': 'no-cache'})
