"""Argument declarations for the Workbench command line."""

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='workbench')
    subcommands = parser.add_subparsers(dest='command', required=True)
    runner = subcommands.add_parser('runner', help='inspect configured runner readiness')
    runner_commands = runner.add_subparsers(dest='runner_command', required=True)
    runner_check = runner_commands.add_parser(
        'check', help='check daemon, capacity and pinned images'
    )
    runner_check.add_argument('--config', required=True)
    action_policy = subcommands.add_parser(
        'action-policy', help='explicit deployment action policy maintenance'
    )
    action_policy_commands = action_policy.add_subparsers(
        dest='action_policy_command', required=True
    )
    action_policy_sync = action_policy_commands.add_parser(
        'sync', help='atomically apply deployment action grants'
    )
    action_policy_sync.add_argument('--config', required=True)
    action_policy_sync.add_argument('--expected-revision', type=int, required=True)
    action_policy_status = action_policy_commands.add_parser(
        'status', help='read current action policy revision'
    )
    action_policy_status.add_argument('--config', required=True)
    doctor = subcommands.add_parser(
        'doctor', help='inspect storage readiness without external probes'
    )
    doctor.add_argument('--config', required=True)
    db = subcommands.add_parser('db', help='explicit maintenance operations')
    db_commands = db.add_subparsers(dest='db_command', required=True)
    migration = db_commands.add_parser('migrate', help='upgrade storage during maintenance')
    migration.add_argument('--config', required=True)
    bootstrap = subcommands.add_parser(
        'bootstrap', help='explicit create-if-missing configuration seed'
    )
    bootstrap.add_argument('--config', required=True)
    bootstrap.add_argument(
        '--identity-token-env',
        help='environment variable containing verified production admin Bearer token',
    )
    worker = subcommands.add_parser('worker', help='claim and execute supported versioned jobs')
    worker.add_argument('--config', required=True)
    worker.add_argument('--once', action='store_true')
    api = subcommands.add_parser('api', help='serve the versioned HTTP API')
    api.add_argument('--config', required=True)
    api.add_argument('--host', default='127.0.0.1')
    api.add_argument('--port', type=int, default=8000)
    scheduler = subcommands.add_parser('scheduler', help='scan durable recovery jobs and deadlines')
    scheduler.add_argument('--config', required=True)
    scheduler.add_argument('--once', action='store_true')
    backup = db_commands.add_parser('backup', help='create a consistent SQLite online backup')
    backup.add_argument('--config', required=True)
    backup.add_argument('--destination', required=True)
    restore = db_commands.add_parser('restore', help='restore to a new quarantined database path')
    restore.add_argument('--config', required=True)
    restore.add_argument('--source', required=True)
    restore.add_argument('--target', required=True)
    acknowledgment = db_commands.add_parser(
        'acknowledge-restore', help='explicitly reopen only a clean M1 restored copy'
    )
    acknowledgment.add_argument('--config', required=True)
    acknowledgment.add_argument('--target', required=True)
    acknowledgment.add_argument('--acknowledge-no-external-effects', action='store_true')
    evaluate = subcommands.add_parser('evaluate', help='evaluate a human-reviewed quality dataset')
    evaluate.add_argument('--dataset', required=True)
    evaluate.add_argument('--output', required=True)
    kb = subcommands.add_parser('kb', help='explicit knowledge index maintenance')
    kb_commands = kb.add_subparsers(dest='kb_command', required=True)
    kb_check = kb_commands.add_parser(
        'check-index', help='inspect index consistency without repair'
    )
    kb_check.add_argument('--config', required=True)
    kb_check.add_argument('--team-id', required=True)
    kb_rebuild = kb_commands.add_parser(
        'rebuild-index', help='rebuild one entry index at its expected revision'
    )
    kb_rebuild.add_argument('--config', required=True)
    kb_rebuild.add_argument('--team-id', required=True)
    kb_rebuild.add_argument('--entry-id', required=True)
    kb_rebuild.add_argument('--expected-revision', type=int, required=True)

    evaluate.add_argument('--dataset-type', choices=('rca', 'knowledge'), default='rca')
    eval_command = subcommands.add_parser(
        'eval', help='versioned and resumable knowledge evaluation'
    )
    eval_subcommands = eval_command.add_subparsers(dest='eval_command', required=True)
    eval_validate = eval_subcommands.add_parser(
        'validate', help='validate a versioned knowledge dataset'
    )
    eval_validate.add_argument('--dataset', required=True)
    eval_validate.add_argument('--purpose', choices=('business', 'engineering'), default='business')
    eval_run = eval_subcommands.add_parser('run', help='run or resume a versioned experiment')
    eval_run.add_argument('--dataset')
    eval_run.add_argument('--config')
    eval_run.add_argument('--experiment-id')
    eval_run.add_argument('--resume')
    eval_run.add_argument('--split', choices=('dev', 'holdout'))
    eval_run.add_argument('--repeats', type=int, default=3)
    eval_run.add_argument('--purpose', choices=('business', 'engineering', 'release'), default=None)
    eval_score = eval_subcommands.add_parser(
        'score', help='score an existing experiment without model calls'
    )
    eval_score.add_argument('--experiment', required=True)
    eval_score.add_argument('--dataset', required=True)
    eval_score.add_argument('--output', required=True)
    eval_score.add_argument('--split', choices=('dev', 'holdout'), default='dev')
    eval_score.add_argument('--repeats', type=int, default=1)
    eval_score.add_argument(
        '--purpose', choices=('business', 'engineering', 'release'), default='business'
    )
    for command in (eval_run, eval_score):
        command.add_argument('--release-id')
        command.add_argument('--holdout-permit')
        command.add_argument('--holdout-ledger')
    eval_compare = eval_subcommands.add_parser('compare', help='compare two compatible experiments')
    eval_compare.add_argument('--baseline', required=True)
    eval_compare.add_argument('--candidate', required=True)
    eval_compare.add_argument('--policy', required=True)
    eval_compare.add_argument('--output', required=True)
    return parser
