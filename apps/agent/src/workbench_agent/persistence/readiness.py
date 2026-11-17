"""Shared settings-aware local-storage policy; runtime never migrates or seeds."""
import asyncio
import os
from pathlib import Path
from workbench_agent.settings import Settings

def persistent_path_errors(settings: Settings) -> list[str]:
    path = settings.storage.database_path
    from workbench_agent.persistence.backends.sqlite_backup import quarantine_path
    if quarantine_path(path).exists():
        return ["restored database quarantined; offline reconciliation required"]
    root = settings.storage.persistent_root
    if root is None or not root.is_dir() or not path.is_relative_to(root):
        return ["database must be within a declared local persistent_root"]
    # ST_LOCAL is available on Darwin; Linux network mounts are checked below.
    flags = os.statvfs(root).f_flag
    if hasattr(os, "ST_LOCAL") and not flags & os.ST_LOCAL:
        return ["persistent_root must use a local filesystem"]
    mounts = Path("/proc/mounts")
    if mounts.exists():
        candidates = []
        for line in mounts.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                mountpoint = Path(parts[1].replace(r"\040", " "))
                if path.is_relative_to(mountpoint):
                    candidates.append((len(mountpoint.parts), parts[2]))
        if candidates and max(candidates)[1] in {"nfs", "nfs4", "cifs", "smbfs", "tmpfs", "ramfs"}:
            return ["persistent_root uses a network or volatile filesystem"]
    if settings.environment == "production":
        for temporary in ("/tmp", "/private/tmp", "/var/tmp", "/private/var/folders", "/dev/shm"):
            if path.is_relative_to(Path(temporary).resolve()):
                return ["production requires a persistent volume, not a temporary directory"]
    if not path.is_file():
        return ["persistent database is absent; run the maintenance migration"]
    if not os.access(path, os.R_OK | os.W_OK) or not os.access(path.parent, os.R_OK | os.W_OK | os.X_OK):
        return ["persistent database and parent require read/write access"]
    return []


async def storage_ready(settings, backend):
    # The driver probe retains actual connection PRAGMA/schema/version checks.
    # Policy inspection is off-loop; no DDL or diagnostic network calls.
    try:
        if backend.path != settings.storage.database_path:
            return False
        if await asyncio.to_thread(persistent_path_errors, settings):
            return False
    except OSError:
        return False
    return await backend.health()
