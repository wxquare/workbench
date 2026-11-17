import os

from .settings import secret_ref


class EnvSecrets:
    def resolve(self, ref: str) -> str:
        secret_ref(ref)
        value = os.environ.get(ref.removeprefix("env://"), "")
        if not value or value != value.strip() or len(value) > 8192:
            raise ValueError("Missing configured secret or invalid secret format")
        return value
