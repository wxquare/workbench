import hashlib
import hmac

from workbench_mcp_server.application.ports import SecretProvider


class BearerAuthenticator:
    def __init__(self, settings, secrets: SecretProvider):
        self._bindings = []
        for p in settings.principals:
            value = secrets.resolve(p.token_secret_ref)
            digest = hashlib.sha256(value.encode()).digest()
            if any(hmac.compare_digest(digest, d) for d, _ in self._bindings):
                raise ValueError("Duplicate principal credentials")
            self._bindings.append((digest, settings.principal(p.id)))

    def authenticate(self, token: str):
        digest = hashlib.sha256(token.encode()).digest()
        matched = None
        for expected, principal in self._bindings:
            if hmac.compare_digest(digest, expected):
                matched = principal
        return matched
