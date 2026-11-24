from fastapi import Header
from fastapi.responses import JSONResponse
from workbench_agent.application.errors import DomainError


def revision(if_match: str | None = Header(default=None)) -> int:
    if if_match is None:
        raise DomainError('IF_MATCH_REQUIRED', 428)
    value = if_match.strip('"')
    if not value.isdecimal() or len(value) > 18:
        raise DomainError('INVALID_REVISION', 422)
    return int(value)


def response(result):
    return JSONResponse(
        result.body,
        status_code=result.status,
        headers={'Idempotency-Replayed': 'true'} if result.replayed else {},
    )
