import io
import zipfile
import pytest

from workbench_agent.skill_management.validator import validate_bundle, InvalidBundle


def bundle(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for path, value in entries:
            archive.writestr(path, value)
    return output.getvalue()


def test_valid_bundle_produces_immutable_resource_hashes():
    result = validate_bundle(
        bundle(
            [
                (
                    'payments/SKILL.md',
                    '---\nname: payments\ndescription: Payment operations\n---\nInstructions',
                ),
                ('payments/references/a.md', 'reference'),
            ]
        ),
        'payments',
    )
    assert (
        result.resources['SKILL.md']
        == b'---\nname: payments\ndescription: Payment operations\n---\nInstructions'
    )
    assert result.hashes['SKILL.md'] != result.hashes['references/a.md']
    assert len(result.bundle_hash) == 64


@pytest.mark.parametrize(
    'path',
    [
        'payments/../escape',
        '/payments/secret',
        'payments//a',
        'payments/./a',
        'payments/\\windows',
        'other/a',
    ],
)
def test_rejects_unsafe_or_wrong_root_paths(path):
    with pytest.raises(InvalidBundle):
        validate_bundle(
            bundle(
                [('payments/SKILL.md', '---\nname: payments\ndescription: a\n---\nx'), (path, 'x')]
            ),
            'payments',
        )


def test_rejects_symlink_and_duplicate_entries():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('payments/SKILL.md', '---\nname: payments\ndescription: a\n---\nx')
        info = zipfile.ZipInfo('payments/link')
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, '../../outside')
    with pytest.raises(InvalidBundle):
        validate_bundle(out.getvalue(), 'payments')
    with pytest.warns(UserWarning):
        payload = bundle([('payments/SKILL.md', 'x'), ('payments/SKILL.md', 'y')])
    with pytest.raises(InvalidBundle):
        validate_bundle(payload, 'payments')


def test_rejects_uninterpreted_claude_hooks():
    with pytest.raises(InvalidBundle, match='unsupported'):
        validate_bundle(
            bundle(
                [
                    (
                        'payments/SKILL.md',
                        '---\nname: payments\ndescription: a\nhooks:\n  pre: shell\n---\nx',
                    )
                ]
            ),
            'payments',
        )


def test_declared_script_dependencies_are_not_silently_accepted():
    with pytest.raises(InvalidBundle, match='unsupported'):
        validate_bundle(
            bundle(
                [
                    ('payments/SKILL.md', '---\nname: payments\ndescription: a\n---\nx'),
                    ('payments/scripts/run.sh', '#!/bin/sh\necho hello'),
                ]
            ),
            'payments',
        )


@pytest.mark.parametrize(
    'key,value',
    [
        ('allowed-tools', '["Bash"]'),
        ('hooks', '{"pre": "shell"}'),
        ('variables', '{"TOKEN": "value"}'),
        ('permissions', '["write"]'),
        ('image', 'runner:latest'),
        ('tools', '["mcp.tool"]'),
    ],
)
def test_claude_execution_declarations_are_explicitly_rejected(key, value):
    doc = f'---\nname: payments\ndescription: test\n{key}: {value}\n---\nbody'
    with pytest.raises(InvalidBundle, match='unsupported'):
        validate_bundle(bundle([('payments/SKILL.md', doc)]), 'payments')
