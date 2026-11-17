"""Versioned persisted snapshot identity digest; never archive-byte verification."""
import re
from workbench_agent.application.idempotency import digest


def snapshot_identity_hash(snapshot):
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',snapshot['commit_sha']):
        raise ValueError('immutable full Git commit required')
    return digest(dict(contract='repo-snapshot-identity-v1',**{field:snapshot[field] for field in (
        'snapshot_id','team_id','agent_id','repo_id','commit_sha','format_version','storage_ref')}))
