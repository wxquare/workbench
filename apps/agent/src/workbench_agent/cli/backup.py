"""Explicit offline maintenance operations. Restore never opens external writes."""
from pathlib import Path
from dataclasses import asdict
from workbench_agent.persistence.backends.sqlite_backup import SQLiteBackup,restore

def backup_database(settings,destination):
    return asdict(SQLiteBackup(settings.storage.database_path).create(Path(destination)))

def restore_database(source,target):
    return asdict(restore(Path(source),Path(target)))
