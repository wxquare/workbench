import pytest


def test_eval_run_parser_preserves_defaults_and_explicit_resume():
    from workbench_agent.cli.parser import build_parser

    parser = build_parser()
    new = parser.parse_args(
        ['eval', 'run', '--dataset', 'data.json', '--config', 'config.json', '--experiment-id', 'e']
    )
    assert (new.command, new.eval_command, new.split, new.repeats, new.purpose) == (
        'eval',
        'run',
        None,
        3,
        None,
    )
    resumed = parser.parse_args(['eval', 'run', '--resume', 'experiment'])
    assert resumed.resume == 'experiment'
    assert resumed.dataset is None and resumed.config is None


def test_parser_rejects_missing_and_invalid_arguments():
    from workbench_agent.cli.parser import build_parser

    parser = build_parser()
    for arguments in ([], ['eval', 'validate'], ['eval', 'run', '--split', 'test']):
        with pytest.raises(SystemExit) as error:
            parser.parse_args(arguments)
        assert error.value.code == 2


def test_legacy_evaluate_default_dataset_type():
    from workbench_agent.cli.parser import build_parser

    args = build_parser().parse_args(['evaluate', '--dataset', 'input', '--output', 'output'])
    assert args.dataset_type == 'rca'
