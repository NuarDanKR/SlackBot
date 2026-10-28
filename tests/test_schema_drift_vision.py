import runpy
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_schema_drift.py'


@pytest.fixture
def drift():
    return runpy.run_path(str(SCRIPT))


def test_only_exact_vision_tables_have_a_different_owner(drift):
    declared = drift['VISION_TABLES'] | {
        'ordinary': 'console_schema.sql',
        'archiver_vision_new_table': 'archiver_vision_schema.sql',
    }
    assert drift['required_bot_access'](declared, set(declared)) == ['archiver_vision_new_table', 'ordinary']
    assert drift['required_bot_access']({'archiver_vision_job': 'other.sql'}, {'archiver_vision_job'}) == [
        'archiver_vision_job',
    ]


def test_image_description_tables_are_also_isolated_vision_tables(drift):
    tables = drift['VISION_TABLES']
    assert tables['archiver_image_model_catalog'] == 'archiver_image_description_schema.sql'
    assert tables['archiver_image_description_revision'] == 'archiver_image_description_schema.sql'
    required = drift['VISION_REQUIRED']
    assert required['archiver_image_description_job'] == {'SELECT', 'INSERT', 'UPDATE'}
    assert required['archiver_image_description_revision'] == {'SELECT', 'INSERT'}
    assert required['archiver_image_description_clearance'] == {'SELECT', 'INSERT'}
    assert drift['VISION_COLUMN_REQUIRED']['archiver_image_description_clearance'] == {
        'UPDATE': {'revoked_at', 'revoked_by', 'revoke_reason'},
    }


def run_check(drift, monkeypatch, *, absent=(), denied=(), problems=(), worker=False, columns=(), have_columns=()):
    main = drift['main']
    globals_ = main.__globals__
    tables = drift['VISION_TABLES'] | {'ordinary': 'console_schema.sql'}
    monkeypatch.setitem(globals_, 'load_env_file', lambda: None)
    monkeypatch.setitem(globals_, 'declared', lambda: (dict(columns), tables))
    monkeypatch.setitem(globals_, 'live', lambda conn:
                        (set(have_columns), set(tables) - set(absent), set(denied)))
    monkeypatch.setitem(globals_, 'vision_access', lambda conn: (list(problems), worker))
    monkeypatch.setenv('DATABASE_URL', 'postgresql://fixture')
    monkeypatch.setattr(psycopg, 'connect', lambda *args, **kwargs: MagicMock())
    return main([])


def test_dormant_worker_can_deploy_without_granting_master(drift, monkeypatch, capsys):
    assert run_check(drift, monkeypatch, denied=drift['VISION_TABLES']) == 0
    output = capsys.readouterr().out
    assert '전용 역할 미생성' in output
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE' not in output


def test_existing_worker_with_correct_permissions_is_valid(drift, monkeypatch):
    assert run_check(drift, monkeypatch, denied=drift['VISION_TABLES'], worker=True) == 0


def test_missing_private_table_is_not_hidden(drift, monkeypatch, capsys):
    assert run_check(drift, monkeypatch, absent=['archiver_vision_job'], denied=drift['VISION_TABLES']) == 1
    assert 'DB 에 없는 표' in capsys.readouterr().out


def test_missing_private_column_is_not_hidden(drift, monkeypatch, capsys):
    columns = [(('archiver_vision_job', 'policy_snapshot'), 'archiver_vision_schema.sql')]
    assert run_check(drift, monkeypatch, denied=drift['VISION_TABLES'], columns=columns) == 1
    assert 'archiver_vision_job.policy_snapshot' in capsys.readouterr().out


def test_ordinary_permission_errors_still_block_and_never_suggest_blanket_crud(drift, monkeypatch, capsys):
    assert run_check(drift, monkeypatch, denied=['ordinary']) == 1
    output = capsys.readouterr().out
    assert 'ordinary' in output and 'GRANT' in output
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE' not in output


@pytest.mark.parametrize('role', ['tyslackai', 'tybot_archiver'])
def test_accidentally_granting_bot_access_is_itself_drift(drift, monkeypatch, capsys, role):
    assert run_check(drift, monkeypatch, problems=[(role, 'archiver_vision_job', 'unexpected_select')]) == 1
    output = capsys.readouterr().out
    assert role in output and 'unexpected_select' in output


def test_worker_permission_problem_blocks_deployment(drift, monkeypatch):
    assert run_check(drift, monkeypatch, worker=True,
                     problems=[('tybot_vision', 'archiver_vision_job', 'missing_update')]) == 1


def check_access(drift, rows, roles=('tybot_vision', 'tyslackai', 'tybot_archiver')):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = [[{'rolname': role} for role in roles], rows]
    result = drift['vision_access'](connection)
    assert 'current_user' in cursor.execute.call_args_list[0][0][0]
    query = cursor.execute.call_args[0][0]
    assert 'has_table_privilege' in query and 'has_any_column_privilege' in query
    assert 'has_column_privilege' in query and 'allowed_columns' in query
    assert 'pg_roles' in query  # effective privileges, not textual ACL parsing
    return result


@pytest.mark.parametrize('column_only', [False, True])
@pytest.mark.parametrize('privilege', ['SELECT', 'REFERENCES'])
def test_effective_bot_permissions_including_column_grants_are_rejected(drift, column_only, privilege):
    problems, exists = check_access(drift, [dict(role_name='tyslackai', table_name='archiver_vision_job',
                                                 privilege=privilege, allowed=not column_only,
                                                 column_allowed=column_only)])
    assert exists and problems == [('tyslackai', 'archiver_vision_job', 'unexpected_' + privilege.lower())]


def test_worker_cannot_modify_or_delete_approval_audit(drift):
    rows = [dict(role_name='tybot_vision', table_name='archiver_vision_policy_audit',
                 privilege=privilege, allowed=True, column_allowed=False) for privilege in ('UPDATE', 'DELETE')]
    problems, _ = check_access(drift, rows)
    assert {code for _, _, code in problems} == {'unexpected_update', 'unexpected_delete'}


def test_worker_must_have_complete_required_table_permissions(drift):
    problems, _ = check_access(drift, [dict(role_name='tybot_vision', table_name='archiver_vision_job',
                                           privilege='UPDATE', allowed=False, column_allowed=True)])
    assert problems == [('tybot_vision', 'archiver_vision_job', 'missing_update')]


def test_clearance_exact_revocation_column_grants_are_valid(drift):
    row = dict(
        role_name='tybot_vision',
        table_name='archiver_image_description_clearance',
        privilege='UPDATE',
        allowed=False,
        column_allowed=True,
        allowed_columns=['revoked_at', 'revoked_by', 'revoke_reason'],
    )
    assert check_access(drift, [row]) == ([], True)


@pytest.mark.parametrize(
    ('allowed', 'columns', 'code'),
    [
        (True, ['revoked_at', 'revoked_by', 'revoke_reason'], 'unexpected_update'),
        (False, ['revoked_at', 'revoked_by'], 'missing_update'),
        (
            False,
            ['revoked_at', 'revoked_by', 'revoke_reason', 'pii_cleared'],
            'unexpected_update',
        ),
    ],
)
def test_clearance_broad_or_incomplete_update_grants_are_rejected(
    drift, allowed, columns, code
):
    row = dict(
        role_name='tybot_vision',
        table_name='archiver_image_description_clearance',
        privilege='UPDATE',
        allowed=allowed,
        column_allowed=True,
        allowed_columns=columns,
    )
    problems, _ = check_access(drift, [row])
    assert problems == [
        ('tybot_vision', 'archiver_image_description_clearance', code)
    ]


def test_missing_optional_role_is_reported_not_created(drift):
    assert check_access(drift, [], roles=('tyslackai', 'tybot_archiver')) == ([], False)


def test_custom_current_bot_role_is_not_exempt_from_isolation(drift):
    problems, _ = check_access(drift, [dict(role_name='custom_bot', table_name='archiver_vision_job',
                                           privilege='SELECT', allowed=True, column_allowed=False)],
                                roles=('custom_bot',))
    assert problems == [('custom_bot', 'archiver_vision_job', 'unexpected_select')]
