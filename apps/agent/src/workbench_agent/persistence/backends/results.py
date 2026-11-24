"""Result/evidence SQL boundary, always using the caller's UoW."""

from sqlalchemy import select, insert, update
from workbench_agent.persistence import schema as s


class SQLiteResultStore:
    async def get(self, uow, team_id, run_id):
        return (
            (
                await uow.execute(
                    select(s.run_outcomes).where(
                        s.run_outcomes.c.team_id == team_id, s.run_outcomes.c.run_id == run_id
                    )
                )
            )
            .mappings()
            .first()
        )

    async def insert(self, uow, values):
        await uow.execute(insert(s.run_outcomes).values(**values))

    async def response(self, uow, team_id, run_id, response):
        await uow.execute(
            update(s.run_outcomes)
            .where(s.run_outcomes.c.team_id == team_id, s.run_outcomes.c.run_id == run_id)
            .values(response=response)
        )

    async def evidence(self, uow, team_id, run_id, refs):
        return list(
            (
                await uow.execute(
                    select(s.evidence).where(
                        s.evidence.c.team_id == team_id,
                        s.evidence.c.run_id == run_id,
                        s.evidence.c.evidence_id.in_(refs),
                    )
                )
            ).mappings()
        )

    async def artifact(self, uow, team_id, artifact_id):
        return (
            (
                await uow.execute(
                    select(s.artifacts).where(
                        s.artifacts.c.team_id == team_id, s.artifacts.c.artifact_id == artifact_id
                    )
                )
            )
            .mappings()
            .first()
        )

    async def artifact_scopes(self, uow, team_id, artifact_id):
        return list(
            (
                await uow.execute(
                    select(s.evidence.c.run_id, s.evidence.c.scope_id).where(
                        s.evidence.c.team_id == team_id, s.evidence.c.artifact_ref == artifact_id
                    )
                )
            ).mappings()
        )

    async def create_upload(self, uow, artifact, scope):
        await uow.execute(insert(s.artifacts).values(**artifact))
        await uow.execute(insert(s.artifact_upload_scopes).values(**scope))

    async def upload_scope(self, uow, team_id, artifact_id):
        return (
            (
                await uow.execute(
                    select(s.artifact_upload_scopes).where(
                        s.artifact_upload_scopes.c.team_id == team_id,
                        s.artifact_upload_scopes.c.artifact_id == artifact_id,
                    )
                )
            )
            .mappings()
            .first()
        )

    async def complete_upload(self, uow, team_id, artifact_id, key, now):
        result = await uow.execute(
            update(s.artifacts)
            .where(
                s.artifacts.c.team_id == team_id,
                s.artifacts.c.artifact_id == artifact_id,
                s.artifacts.c.status == 'uploading',
            )
            .values(object_key=key, status='available', updated_at=now)
        )
        return result.rowcount == 1
