"""Console entry point; additional process commands are introduced by their tasks."""

import json
import sys
import yaml
from workbench_agent.settings import load_settings
from workbench_agent.cli.doctor import check_sqlite_runtime
from workbench_agent.cli.parser import build_parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == 'eval':
        from workbench_agent.cli.eval import main as eval_main

        try:
            code = eval_main(args)
        except (ValueError, OSError, PermissionError):
            print(json.dumps({'error': 'invalid evaluation input'}))
            sys.exit(2)
        if code:
            sys.exit(code)
        return
    if args.command == 'evaluate':
        from workbench_agent.cli.evaluate import run_evaluate

        try:
            passed = run_evaluate(args.dataset, args.output, args.dataset_type)
        except (ValueError, OSError):
            print(json.dumps({'error': 'invalid or unreadable evaluation dataset or output'}))
            sys.exit(1)
        if not passed:
            sys.exit(1)
        return
    try:
        settings = load_settings(args.config)
    except (ValueError, OSError, yaml.YAMLError):
        # Do not print parser input: malformed configuration can contain credentials.
        print(json.dumps({'ready': False, 'errors': ['invalid or unreadable configuration']}))
        sys.exit(1)
    if args.command in ('api', 'worker', 'scheduler'):
        from workbench_agent.observability import configure_logging

        configure_logging(settings.observability, args.command, settings.environment)
    if args.command == 'kb':
        import asyncio
        from workbench_agent.cli.knowledge import maintain

        try:
            report = asyncio.run(
                maintain(
                    settings,
                    args.kb_command,
                    args.team_id,
                    entry_id=getattr(args, 'entry_id', None),
                    expected_revision=getattr(args, 'expected_revision', None),
                )
            )
        except Exception:
            print(json.dumps({'ready': False, 'errors': ['knowledge index maintenance failed']}))
            sys.exit(1)
        print(json.dumps({'ready': True, **report}))
        return
    if args.command == 'action-policy':
        import asyncio
        from workbench_agent.cli.action_policy import sync_action_policy, action_policy_status

        try:
            report = asyncio.run(
                sync_action_policy(settings, args.expected_revision)
                if args.action_policy_command == 'sync'
                else action_policy_status(settings)
            )
        except Exception:
            report = {'ready': False, 'status': 'action_policy_sync_failed'}
        print(json.dumps(report))
        if not report['ready']:
            sys.exit(1)
        return
    if args.command == 'runner':
        import asyncio
        from workbench_agent.runner.readiness import runner_readiness
        from workbench_agent.runner.service import ContainerRunner

        storage = settings.storage
        adapter = ContainerRunner(
            str(storage.docker_binary) if storage.docker_binary else 'docker',
            docker_host=storage.docker_host,
            identity_mode=storage.runner_identity_mode,
        )
        report = asyncio.run(
            runner_readiness(
                adapter, storage.runner_daemon_id, [b.image for b in storage.runner_bindings]
            )
        )
        print(json.dumps(report))
        if not report['ready']:
            sys.exit(1)
        return
    if args.command == 'api':
        from workbench_agent.cli.api import run_api

        run_api(settings, host=args.host, port=args.port)
        return
    if args.command == 'scheduler':
        import asyncio
        from workbench_agent.cli.scheduler import run_scheduler

        try:
            report = asyncio.run(run_scheduler(settings, once=args.once))
        except Exception:
            report = {'ready': False, 'status': 'scheduler_unavailable'}
        print(json.dumps(report))
        if not report['ready']:
            sys.exit(1)
        return
    if args.command == 'db' and args.db_command == 'acknowledge-restore':
        from workbench_agent.persistence.backends.sqlite_backup import (
            acknowledge_restore,
            AcknowledgmentCleanupError,
        )

        try:
            report = acknowledge_restore(
                args.target, acknowledge=args.acknowledge_no_external_effects
            )
        except AcknowledgmentCleanupError as error:
            print(json.dumps({'ready': False, **error.report, 'errors': [str(error)]}))
            sys.exit(1)
        except Exception:
            print(
                json.dumps(
                    {
                        'ready': False,
                        'errors': [
                            'restore acknowledgment incomplete; inspect quarantine/review records before startup'
                        ],
                    }
                )
            )
            sys.exit(1)
        print(json.dumps(report))
        return
    if args.command == 'db' and args.db_command in ('backup', 'restore'):
        from workbench_agent.cli.backup import backup_database, restore_database

        try:
            report = (
                backup_database(settings, args.destination)
                if args.db_command == 'backup'
                else restore_database(args.source, args.target)
            )
        except Exception:
            print(
                json.dumps(
                    {
                        'ready': False,
                        'errors': ['backup/restore validation failed or destination exists'],
                    }
                )
            )
            sys.exit(1)
        print(json.dumps(report))
        return
    if args.command == 'worker':
        import asyncio
        from workbench_agent.cli.worker import run_worker

        try:
            report = asyncio.run(run_worker(settings, once=args.once))
        except Exception:
            report = {'ready': False, 'status': 'worker_unavailable'}
        print(json.dumps(report))
        if not report['ready']:
            sys.exit(1)
        return
    if args.command == 'db':
        from workbench_agent.cli.db import FreshAgentDatabaseRequired, migrate

        try:
            migrate(settings.storage.database_url.get_secret_value())
        except FreshAgentDatabaseRequired:
            print(
                json.dumps(
                    {
                        'ready': False,
                        'errors': [
                            'incompatible Agent storage; choose a fresh Agent database path, then run db migrate, '
                            'bootstrap and doctor; existing database left untouched'
                        ],
                    }
                )
            )
            sys.exit(1)
        except Exception:
            print(json.dumps({'ready': False, 'errors': ['storage maintenance migration failed']}))
            sys.exit(1)
    if args.command == 'bootstrap':
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
                if settings.environment == 'production':
                    if not args.identity_token_env:
                        raise PermissionError('verified identity required')
                    verifier = OIDCVerifier(
                        resolve_env_reference(settings.service.oidc_issuer_ref),
                        settings.service.oidc_audience,
                        jwks_url=settings.service.oidc_jwks_url,
                    )
                    identity = await verifier.verify(os.environ.get(args.identity_token_env, ''))
                return await BootstrapService(backend).apply(settings, identity=identity)
            finally:
                await backend.close()

        try:
            report = asyncio.run(apply_seed())
            print(report.model_dump_json())
        except Exception:
            print(
                json.dumps(
                    {
                        'ready': False,
                        'errors': [
                            'bootstrap failed; production requires explicitly configured verified OIDC admin'
                        ],
                    }
                )
            )
            sys.exit(1)
        return
    diagnostics = check_sqlite_runtime(settings)
    print(diagnostics.model_dump_json())
    if not diagnostics.ready:
        sys.exit(1)


if __name__ == '__main__':
    main()
