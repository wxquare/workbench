from sqlalchemy import (MetaData, Table, Column, String, Text, BigInteger,
                        LargeBinary, ForeignKeyConstraint, UniqueConstraint, Index, CheckConstraint)

metadata = MetaData(naming_convention={"ix":"ix_%(table_name)s_%(column_0_name)s",
    "uq":"uq_%(table_name)s_%(column_0_N_name)s", "fk":"fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk":"pk_%(table_name)s", "ck":"ck_%(table_name)s_%(constraint_name)s"})
ID = String(36)
KEY = String(128)
HASH = String(64)

def col(name, type_=Text, *, nullable=True, default=None, primary=False):
    return Column(name, type_, nullable=False if primary else nullable,
                  primary_key=primary, server_default=default)

def ref(local, target, remote=None):
    """All tenant references verify the team at the database boundary."""
    local = [local] if isinstance(local, str) else local
    remote = local if remote is None else ([remote] if isinstance(remote,str) else remote)
    return ForeignKeyConstraint(["team_id",*local], [f"{target}.team_id",*[f"{target}.{c}" for c in remote]], ondelete="RESTRICT")

def tenant(name, identity, *items):
    return Table(name, metadata, col("team_id",ID,nullable=False), col(identity,ID,primary=True),
        *(item for item in items if isinstance(item,Column)),
        *(item for item in items if not isinstance(item,Column)),
        UniqueConstraint("team_id",identity),
        ForeignKeyConstraint(["team_id"],["teams.team_id"],ondelete="RESTRICT"))

def timestamps():
    return [col("created_at",BigInteger,nullable=False,default="0"),col("updated_at",BigInteger,nullable=False,default="0")]

def revision():
    return col("revision",BigInteger,nullable=False,default="0")
