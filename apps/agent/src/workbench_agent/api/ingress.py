"""Opt-in local test ingress. No production connector protocols are implied."""

from fastapi import APIRouter, Request
from fastapi.responses import Response
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import canonical
from workbench_agent.settings import resolve_env_reference

router = APIRouter(prefix='/v1/ingress')


@router.post('/{connector_id}')
async def accept(connector_id: str, request: Request):
    settings = request.app.state.settings
    if settings.environment == 'production':
        raise not_found()
    binding = settings.service.synthetic_ingress.get(connector_id)
    if binding is None:
        raise not_found()
    for key in ('x-dod-timestamp', 'x-dod-signature'):
        if len(request.headers.getlist(key)) != 1:
            raise DomainError('INGRESS_AUTH_FAILED', 401)
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 65536:
            raise DomainError('INVALID_INGRESS', 413)
        chunks.append(chunk)
    try:
        secret = resolve_env_reference(binding.secret_ref).encode('utf-8')
    except ValueError:
        raise DomainError('INGRESS_UNAVAILABLE', 503) from None
    if binding.protocol == 'synthetic-chat-v1':
        from workbench_agent.ingress.seatalk import SyntheticChatBinding, SyntheticChatIngress

        service = SyntheticChatIngress(
            request.app.state.backend,
            SyntheticChatBinding(
                connector_id=connector_id,
                team_id=binding.team_id,
                agent_id=binding.agent_id,
                actors=binding.actors,
                secret=secret,
            ),
        )
    elif binding.protocol == 'synthetic-alert-v2':
        identity = settings.service.connector_identities.get(binding.identity_id)
        if (
            not identity
            or identity['subject'] != binding.actor_subject
            or identity['team_id'] != binding.team_id
            or binding.agent_id not in identity['agent_ids']
        ):
            raise DomainError('INGRESS_UNAVAILABLE', 503)
        from workbench_agent.ingress.alerts import SyntheticAlertV2Binding, SyntheticAlertV2Ingress

        service = SyntheticAlertV2Ingress(
            request.app.state.backend,
            SyntheticAlertV2Binding(
                connector_id=connector_id,
                team_id=binding.team_id,
                agent_id=binding.agent_id,
                actor_subject=binding.actor_subject,
                secret=secret,
                identity_id=binding.identity_id,
                allowed_scopes=frozenset(binding.allowed_scopes),
            ),
        )
    else:
        from workbench_agent.ingress.alerts import SyntheticAlertBinding, SyntheticAlertIngress

        service = SyntheticAlertIngress(
            request.app.state.backend,
            SyntheticAlertBinding(
                connector_id=connector_id,
                team_id=binding.team_id,
                agent_id=binding.agent_id,
                actor_subject=binding.actor_subject,
                secret=secret,
            ),
        )
    result = await service.accept(b''.join(chunks), request.headers)
    return Response(
        canonical(result.model_dump(mode='json')),
        media_type='application/json',
        status_code=202,
        headers={'Idempotency-Replayed': 'true'} if result.replayed else {},
    )
