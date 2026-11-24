"""One consistent read snapshot for all review source tables."""
from sqlalchemy import func, select

from workbench_agent.persistence import schema


class SQLiteReviewStore:
    async def candidates(self, uow, auth, state):
        actor = func.json_extract(schema.interrupts.c.schema, '$.actor')
        runs = [dict(row) for row in (await uow.execute(
            select(schema.runs).join(
                schema.interrupts,
                (schema.interrupts.c.team_id == schema.runs.c.team_id)
                & (schema.interrupts.c.run_id == schema.runs.c.run_id),
            ).where(
                schema.runs.c.team_id == auth.team_id,
                schema.runs.c.agent_id.in_(
                    select(schema.agents.c.agent_id).where(
                        schema.agents.c.team_id == auth.team_id,
                        schema.agents.c.status == 'active',
                    )
                ),
                schema.runs.c.status.in_(('waiting_input', 'waiting_approval')),
                schema.interrupts.c.status == 'open',
                schema.interrupts.c.accepted_resume_request_id.is_(None),
                actor == auth.subject,
            ).order_by(
                func.coalesce(
                    schema.runs.c.waiting_deadline_at,
                    schema.interrupts.c.expires_at,
                    schema.runs.c.wall_deadline_at,
                ),
                schema.interrupts.c.created_at,
                schema.interrupts.c.interrupt_id,
            ).limit(5_001)
        )).mappings()]
        interrupts = {
            row['run_id']: dict(row)
            for row in (await uow.execute(
                select(schema.interrupts).where(
                    schema.interrupts.c.team_id == auth.team_id,
                    schema.interrupts.c.run_id.in_([row['run_id'] for row in runs]),
                )
            )).mappings()
        }
        checkpoint_run_ids = set()
        if runs:
            checkpoint_run_ids = set((await uow.execute(
                select(schema.graph_checkpoints.c.run_id).where(
                    schema.graph_checkpoints.c.team_id == auth.team_id,
                    schema.graph_checkpoints.c.namespace == '',
                    schema.graph_checkpoints.c.run_id.in_([row['run_id'] for row in runs]),
                ).distinct()
            )).scalars())
        page = (await uow.execute(
            select(schema.actions).join(
                schema.approvals,
                (schema.approvals.c.team_id == schema.actions.c.team_id)
                & (schema.approvals.c.action_id == schema.actions.c.action_id),
            ).where(
                schema.actions.c.team_id == auth.team_id,
                schema.actions.c.state.in_(
                    ('proposed', 'reapproval_pending', 'failed', 'unknown')
                ),
                schema.approvals.c.subject == auth.subject,
            ).order_by(
                schema.approvals.c.created_at.desc(),
                schema.approvals.c.approval_id.desc(),
                schema.actions.c.action_id,
            ).limit(5_001)
        )).mappings().all()
        action_rows = []
        selected = set()
        for row in page:
            if len(selected) == 5_001:
                break
            if row['action_id'] not in selected:
                action_rows.append(row)
                selected.add(row['action_id'])
        actions = []
        seen = set()
        for row in action_rows:
            if row['action_id'] not in seen:
                actions.append(dict(row))
                seen.add(row['action_id'])
        approvals = {}
        outboxes = {}
        if actions:
            action_ids = [row['action_id'] for row in actions]
            for row in (await uow.execute(
                select(schema.approvals).where(
                    schema.approvals.c.team_id == auth.team_id,
                    schema.approvals.c.action_id.in_(action_ids),
                ).order_by(
                    schema.approvals.c.action_id,
                    schema.approvals.c.created_at,
                    schema.approvals.c.approval_id,
                )
            )).mappings():
                approvals[row['action_id']] = dict(row)
            for row in (await uow.execute(
                select(schema.outbox).where(
                    schema.outbox.c.team_id == auth.team_id,
                    schema.outbox.c.action_id.in_(action_ids),
                )
            )).mappings():
                outboxes[row['action_id']] = dict(row)
        run_by_id = {row['run_id']: row for row in runs}
        return {
            'runs': runs,
            'interrupts': interrupts,
            'checkpoint_run_ids': checkpoint_run_ids,
            'actions': actions,
            'approvals': approvals,
            'outboxes': outboxes,
            'run_by_id': run_by_id,
        }
