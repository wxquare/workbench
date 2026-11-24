from fastapi import APIRouter, Request, Depends, Query, Header
from fastapi.responses import StreamingResponse
from workbench_agent.api.auth import authenticate
from workbench_agent.application.errors import DomainError
from workbench_agent.runtime.streaming import EventResponse

router = APIRouter()


@router.get('/v1/runs/{run_id}/events')
async def events(
    run_id: str,
    request: Request,
    auth=Depends(authenticate),
    after_seq: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None),
):
    cursor = after_seq if after_seq is not None else 0
    if last_event_id is not None:
        rid, separator, value = last_event_id.rpartition(':')
        if (
            not separator
            or rid != run_id
            or not value.isascii()
            or not value.isdecimal()
            or len(value) > 18
        ):
            raise DomainError('INVALID_EVENT_CURSOR', 422)
        if after_seq is not None and after_seq != int(value):
            raise DomainError('INVALID_EVENT_CURSOR', 422)
        cursor = int(value)
    store = request.app.state.events
    # Full authorization/cursor/retention validation before response headers.
    await store.read_batch(auth, run_id, cursor, limit=1)
    return EventResponse(store, auth, run_id, cursor)
