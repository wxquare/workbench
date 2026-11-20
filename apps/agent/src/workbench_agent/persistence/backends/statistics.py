"""SQLite aggregation adapter for authorized Workbench statistics."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select

from workbench_agent.persistence import schema


TERMINAL = {'succeeded', 'partial', 'failed', 'cancelled', 'timed_out', 'expired'}
FAILED = {'failed', 'timed_out', 'expired'}


class SQLiteStatisticsStore:
    async def summary(
        self,
        uow,
        *,
        team_id: str,
        agent_ids: tuple[str, ...],
        start_ms: int,
        end_ms: int,
    ) -> dict:
        if not agent_ids:
            return self._empty()
        runs = (await uow.execute(select(schema.runs).where(
            schema.runs.c.team_id == team_id,
            schema.runs.c.agent_id.in_(agent_ids),
            schema.runs.c.created_at >= start_ms,
            schema.runs.c.created_at < end_ms,
        ))).mappings().all()
        ids = [row['run_id'] for row in runs]
        if not ids:
            return self._empty()
        outcomes = {
            row['run_id']: json.loads(row['payload'])
            for row in (await uow.execute(select(schema.run_outcomes).where(
                schema.run_outcomes.c.team_id == team_id,
                schema.run_outcomes.c.run_id.in_(ids),
            ))).mappings()
        }
        feedback_rows = (await uow.execute(select(schema.feedback).where(
            schema.feedback.c.team_id == team_id,
            schema.feedback.c.run_id.in_(ids),
        ).order_by(schema.feedback.c.created_at, schema.feedback.c.feedback_id))).mappings().all()
        gaps = (await uow.execute(select(schema.knowledge_gaps).where(
            schema.knowledge_gaps.c.team_id == team_id,
            schema.knowledge_gaps.c.run_id.in_(ids),
        ))).mappings().all()
        usage_rows = (await uow.execute(select(
            schema.usage_records.c.run_id,
            schema.usage_records.c.amount_micros,
            schema.usage_records.c.currency,
            schema.usage_records.c.status,
        ).where(
            schema.usage_records.c.team_id == team_id,
            schema.usage_records.c.run_id.in_(ids),
        ))).mappings().all()
        latest_feedback = {}
        for row in feedback_rows:
            payload = json.loads(row['payload'])
            key = (row['run_id'], payload.get('subject'))
            latest_feedback[key] = payload
        statuses = Counter(row['status'] for row in runs)
        terminal = [row for row in runs if row['status'] in TERMINAL]
        succeeded = [
            row for row in terminal
            if row['status'] == 'succeeded'
            and (outcomes.get(row['run_id']) or {}).get('completed') is True
        ]
        partial = [row for row in terminal if row['status'] == 'partial']
        inconsistent = [
            row for row in terminal
            if row['status'] == 'succeeded'
            and (outcomes.get(row['run_id']) or {}).get('completed') is not True
        ]
        errors = Counter('unknown' for row in terminal if row['status'] in FAILED)
        costs = {}
        pending = 0
        for row in usage_rows:
            if row['amount_micros'] is not None and row['currency']:
                costs[row['currency']] = costs.get(row['currency'], 0) + row['amount_micros']
            if row['status'] == 'pending' or row['amount_micros'] is None:
                pending += 1
        gaps_summary = Counter(row['review_status'] for row in gaps)
        negative = sum(
            1 for value in latest_feedback.values()
            if value.get('rating') == -1
        )
        trend = self._trend(runs, outcomes)
        return {
            'counts': {
                'runs': len(runs),
                'active': sum(row['status'] not in TERMINAL for row in runs),
                'terminal': len(terminal),
                'succeeded': len(succeeded),
                'partial': len(partial),
                'state_inconsistent': len(inconsistent),
            },
            'rates': {
                'success': len(succeeded) / len(terminal) if terminal else None,
                'partial': len(partial) / len(terminal) if terminal else None,
            },
            'failure_top5': errors.most_common(5),
            'latency': {
                'average_ms': (
                    sum(row['active_elapsed_ms'] for row in terminal if row['started_at'] is not None)
                    / len([row for row in terminal if row['started_at'] is not None])
                ) if any(row['started_at'] is not None for row in terminal) else None,
            },
            'costs': {'known': costs, 'pending_run_count': pending},
            'gaps': {
                'open': gaps_summary.get('open', 0),
                'resolved': gaps_summary.get('resolved', 0),
                'dismissed': gaps_summary.get('dismissed', 0),
            },
            'feedback': {
                'negative_rate': negative / len(latest_feedback) if latest_feedback else None,
                'subjects': len(latest_feedback),
            },
            'trend': trend,
        }

    @staticmethod
    def _empty():
        return {
            'counts': {'runs': 0, 'active': 0, 'terminal': 0, 'succeeded': 0, 'partial': 0, 'state_inconsistent': 0},
            'rates': {'success': None, 'partial': None},
            'failure_top5': [],
            'latency': {'average_ms': None},
            'costs': {'known': {}, 'pending_run_count': 0},
            'gaps': {'open': 0, 'resolved': 0, 'dismissed': 0},
            'feedback': {'negative_rate': None, 'subjects': 0},
            'trend': [],
        }

    @staticmethod
    def _trend(runs, outcomes):
        shanghai = timezone(timedelta(hours=8))
        buckets = defaultdict(lambda: {'runs': 0, 'succeeded': 0, 'partial': 0})
        for row in runs:
            day = datetime.fromtimestamp(row['created_at'] / 1000, shanghai).date().isoformat()
            bucket = buckets[day]
            bucket['runs'] += 1
            if row['status'] == 'partial':
                bucket['partial'] += 1
            elif row['status'] == 'succeeded' and (outcomes.get(row['run_id']) or {}).get('completed') is True:
                bucket['succeeded'] += 1
        return [{'day': day, **values} for day, values in sorted(buckets.items())]
