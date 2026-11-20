"""Console entry point; additional process commands are introduced by their tasks."""
import argparse
import json
import sys
import yaml
from workbench_agent.settings import load_settings
from workbench_agent.cli.doctor import check_sqlite_runtime


def main() -> None:
    parser = argparse.ArgumentParser(prog="workbench")
    subcommands = parser.add_subparsers(dest="command", required=True)
    runner = subcommands.add_parser("runner", help="inspect configured runner readiness")
    runner_commands = runner.add_subparsers(dest="runner_command", required=True)
    runner_check = runner_commands.add_parser("check", help="check daemon, capacity and pinned images")
    runner_check.add_argument("--config", required=True)
    action_policy = subcommands.add_parser("action-policy", help="explicit deployment action policy maintenance")
    action_policy_commands=action_policy.add_subparsers(dest="action_policy_command",required=True)
    action_policy_sync=action_policy_commands.add_parser("sync",help="atomically apply deployment action grants")
    action_policy_sync.add_argument("--config",required=True)
    action_policy_sync.add_argument("--expected-revision",type=int,required=True)
    action_policy_status=action_policy_commands.add_parser("status",help="read current action policy revision")
    action_policy_status.add_argument("--config",required=True)
    doctor = subcommands.add_parser("doctor", help="inspect storage readiness without external probes")
    doctor.add_argument("--config", required=True)
    db = subcommands.add_parser("db", help="explicit maintenance operations")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    migration = db_commands.add_parser("migrate", help="upgrade storage during maintenance")
    migration.add_argument("--config", required=True)
    bootstrap = subcommands.add_parser("bootstrap", help="explicit create-if-missing configuration seed")
    bootstrap.add_argument("--config", required=True)
    bootstrap.add_argument("--identity-token-env", help="environment variable containing verified production admin Bearer token")
    worker = subcommands.add_parser("worker", help="claim and execute supported versioned jobs")
    worker.add_argument("--config", required=True)
    worker.add_argument("--once", action="store_true")
    api = subcommands.add_parser("api", help="serve the versioned HTTP API")
    api.add_argument("--config", required=True)
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    scheduler = subcommands.add_parser("scheduler", help="scan durable recovery jobs and deadlines")
    scheduler.add_argument("--config", required=True)
    scheduler.add_argument("--once", action="store_true")
    backup = db_commands.add_parser("backup", help="create a consistent SQLite online backup")
    backup.add_argument("--config", required=True)
    backup.add_argument("--destination", required=True)
    restore = db_commands.add_parser("restore", help="restore to a new quarantined database path")
    restore.add_argument("--config", required=True)
    restore.add_argument("--source", required=True)
    restore.add_argument("--target", required=True)
    acknowledgment = db_commands.add_parser("acknowledge-restore", help="explicitly reopen only a clean M1 restored copy")
    acknowledgment.add_argument("--config", required=True)
    acknowledgment.add_argument("--target", required=True)
    acknowledgment.add_argument("--acknowledge-no-external-effects", action="store_true")
    evaluate = subcommands.add_parser("evaluate", help="evaluate a human-reviewed quality dataset")
    evaluate.add_argument("--dataset", required=True)
    evaluate.add_argument("--output", required=True)
    kb = subcommands.add_parser('kb', help='explicit knowledge index maintenance')
    kb_commands = kb.add_subparsers(dest='kb_command', required=True)
    kb_check = kb_commands.add_parser('check-index', help='inspect index consistency without repair')
    kb_check.add_argument('--config', required=True)
    kb_check.add_argument('--team-id', required=True)
    kb_rebuild = kb_commands.add_parser('rebuild-index', help='rebuild one entry index at its expected revision')
    kb_rebuild.add_argument('--config', required=True)
    kb_rebuild.add_argument('--team-id', required=True)
    kb_rebuild.add_argument('--entry-id', required=True)
    kb_rebuild.add_argument('--expected-revision', type=int, required=True)

    evaluate.add_argument("--dataset-type", choices=("rca", "knowledge"), default="rca")
    eval_command = subcommands.add_parser("eval", help="versioned and resumable knowledge evaluation")
    eval_subcommands = eval_command.add_subparsers(dest="eval_command", required=True)
    eval_validate = eval_subcommands.add_parser("validate", help="validate a versioned knowledge dataset")
    eval_validate.add_argument("--dataset", required=True)
    eval_run = eval_subcommands.add_parser("run", help="run or resume a versioned experiment")
    eval_run.add_argument("--dataset")
    eval_run.add_argument("--config")
    eval_run.add_argument("--experiment-id")
    eval_run.add_argument("--resume")
    eval_run.add_argument("--split", choices=("dev", "holdout"), default="dev")
    eval_run.add_argument("--repeats", type=int, default=3)
    eval_run.add_argument("--purpose", choices=("release",), default=None)
    eval_score = eval_subcommands.add_parser("score", help="score an existing experiment without model calls")
    eval_score.add_argument("--experiment", required=True)
    eval_score.add_argument("--dataset", required=True)
    eval_score.add_argument("--output", required=True)
    eval_score.add_argument("--split", choices=("dev", "holdout"), default="dev")
    eval_score.add_argument("--repeats", type=int, default=1)
    eval_compare = eval_subcommands.add_parser("compare", help="compare two compatible experiments")
    eval_compare.add_argument("--baseline", required=True)
    eval_compare.add_argument("--candidate", required=True)
    eval_compare.add_argument("--policy", required=True)
    eval_compare.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "eval":
        from workbench_agent.cli.eval import main as eval_main
        try:
            code = eval_main(args)
        except (ValueError, OSError):
            print(json.dumps({"error": "invalid evaluation input"}))
            sys.exit(2)
        if code:
            sys.exit(code)
        return
    if args.command == "evaluate":
        from workbench_agent.cli.evaluate import run_evaluate
        try:
            passed = run_evaluate(args.dataset, args.output, args.dataset_type)
        except (ValueError, OSError):
            print(json.dumps({"error": "invalid or unreadable evaluation dataset or output"}))
            sys.exit(1)
        if not passed:
            sys.exit(1)
        return
    try:
        settings = load_settings(args.config)
    except (ValueError, OSError, yaml.YAMLError):
        # Do not print parser input: malformed configuration can contain credentials.
        print(json.dumps({"ready": False, "errors": ["invalid or unreadable configuration"]}))
        sys.exit(1)
    if args.command in ('api', 'worker', 'scheduler'):
        from workbench_agent.observability import configure_logging
        configure_logging(settings.observability, args.command, settings.environment)
    if args.command == 'kb':
        import asyncio
        from workbench_agent.cli.knowledge import maintain
        try:
            report = asyncio.run(maintain(settings, args.kb_command, args.team_id,
                entry_id=getattr(args, 'entry_id', None), expected_revision=getattr(args, 'expected_revision', None)))
        except Exception:
            print(json.dumps({'ready': False, 'errors': ['knowledge index maintenance failed']}))
            sys.exit(1)
        print(json.dumps({'ready': True, **report}))
        return
    if args.command == "action-policy":
        import asyncio
        from workbench_agent.cli.action_policy import sync_action_policy,action_policy_status
        try:
            report=asyncio.run(sync_action_policy(settings,args.expected_revision) if args.action_policy_command=='sync'
                               else action_policy_status(settings))
        except Exception: report={'ready':False,'status':'action_policy_sync_failed'}
        print(json.dumps(report))
        if not report['ready']: sys.exit(1)
        return
    if args.command == "runner":
        import asyncio
        from workbench_agent.runner.readiness import runner_readiness
        from workbench_agent.runner.service import ContainerRunner
        storage = settings.storage
        adapter = ContainerRunner(str(storage.docker_binary) if storage.docker_binary else "docker",
                                  docker_host=storage.docker_host, identity_mode=storage.runner_identity_mode)
        report = asyncio.run(runner_readiness(adapter, storage.runner_daemon_id, [b.image for b in storage.runner_bindings]))
        print(json.dumps(report))
        if not report["ready"]: sys.exit(1)
        return
    if args.command == "api":
        from workbench_agent.cli.api import run_api
        run_api(settings,host=args.host,port=args.port)
        return
    if args.command == "scheduler":
        import asyncio
        from workbench_agent.cli.scheduler import run_scheduler
        try: report=asyncio.run(run_scheduler(settings,once=args.once))
        except Exception: report={"ready":False,"status":"scheduler_unavailable"}
        print(json.dumps(report))
        if not report['ready']: sys.exit(1)
        return
    if args.command == "db" and args.db_command == "acknowledge-restore":
        from workbench_agent.persistence.backends.sqlite_backup import acknowledge_restore,AcknowledgmentCleanupError
        try: report=acknowledge_restore(args.target,acknowledge=args.acknowledge_no_external_effects)
        except AcknowledgmentCleanupError as error:
            print(json.dumps({"ready":False,**error.report,"errors":[str(error)]}))
            sys.exit(1)
        except Exception:
            print(json.dumps({"ready":False,"errors":["restore acknowledgment incomplete; inspect quarantine/review records before startup"]}))
            sys.exit(1)
        print(json.dumps(report))
        return
    if args.command == "db" and args.db_command in ("backup","restore"):
        from workbench_agent.cli.backup import backup_database,restore_database
        try:
            report=backup_database(settings,args.destination) if args.db_command=='backup' else restore_database(args.source,args.target)
        except Exception:
            print(json.dumps({"ready":False,"errors":["backup/restore validation failed or destination exists"]}))
            sys.exit(1)
        print(json.dumps(report))
        return
    if args.command == "worker":
        import asyncio
        from workbench_agent.cli.worker import run_worker
        try:
            report = asyncio.run(run_worker(settings, once=args.once))
        except Exception:
            report = {"ready": False, "status": "worker_unavailable"}
        print(json.dumps(report))
        if not report['ready']:
            sys.exit(1)
        return
    if args.command == "db":
        from workbench_agent.cli.db import FreshAgentDatabaseRequired, migrate
        try:
            migrate(settings.storage.database_url.get_secret_value())
        except FreshAgentDatabaseRequired:
            print(json.dumps({"ready": False, "errors": [
                "incompatible Agent storage; choose a fresh Agent database path, then run db migrate, "
                "bootstrap and doctor; existing database left untouched"]}))
            sys.exit(1)
        except Exception:
            print(json.dumps({"ready": False, "errors": ["storage maintenance migration failed"]}))
            sys.exit(1)
    if args.command == "bootstrap":
        import asyncio
        import os
        from workbench_agent.cli.bootstrap import BootstrapService
        from workbench_agent.persistence.backends.sqlite import SQLiteBackend
        from workbench_agent.api.auth import OIDCVerifier
        from workbench_agent.settings import resolve_env_reference
        async def apply_seed():
            backend = SQLiteBackend(settings.storage.database_url.get_secret_value())
            try:
                identity = None
                if settings.environment == "production":
                    if not args.identity_token_env:
                        raise PermissionError("verified identity required")
                    verifier = OIDCVerifier(resolve_env_reference(settings.service.oidc_issuer_ref),
                        settings.service.oidc_audience, jwks_url=settings.service.oidc_jwks_url)
                    identity = await verifier.verify(os.environ.get(args.identity_token_env, ""))
                return await BootstrapService(backend).apply(settings, identity=identity)
            finally:
                await backend.close()
        try:
            report = asyncio.run(apply_seed())
            print(report.model_dump_json())
        except Exception:
            print(json.dumps({"ready": False, "errors": ["bootstrap failed; production requires explicitly configured verified OIDC admin"]}))
            sys.exit(1)
        return
    diagnostics = check_sqlite_runtime(settings)
    print(diagnostics.model_dump_json())
    if not diagnostics.ready:
        sys.exit(1)


if __name__ == "__main__":
    main()
