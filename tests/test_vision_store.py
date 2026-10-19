from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_vision_policy import APPROVAL, NOW, PAGE, POLICY, RESERVATION

from tybot.archive.vision_store import VisionDenied, VisionStore, approval_scope, identity, money


@pytest.mark.parametrize('value', [True, False, '-1', 'NaN', 'Infinity', '-Infinity', None, 'bad'])
def test_invalid_money_is_not_a_zero_budget(value):
    with pytest.raises(VisionDenied, match='invalid_amount'):
        money(value)


def test_money_keeps_decimal_precision():
    assert money('0.001234567891') == Decimal('0.001234567891')


def test_renewal_and_policy_edit_do_not_authorize_a_duplicate_send():
    scope = approval_scope(APPROVAL)
    renewed = replace(APPROVAL, approval_id='renewed', policy_revision='new')
    assert identity(scope) == identity(approval_scope(renewed))
    for field, value in [('workspace', 'mgmt'), ('image_sha256', 'b' * 64),
                         ('original_sha256', 'c' * 64), ('page_number', 4),
                         ('provider', 'other'), ('model', 'other'),
                         ('renderer_version', 'new')]:
        assert identity(scope) != identity(scope | {field: value})


def test_receipt_is_bound_to_the_approval_scope():
    job = dict(id=RESERVATION.reservation_id, approval_id=APPROVAL.approval_id,
               max_output_tokens=RESERVATION.max_output_tokens,
               reserved_usd=RESERVATION.amount_usd, expires_at=RESERVATION.expires_at,
               state='reserved', scope=approval_scope(APPROVAL))
    store = VisionStore(None)
    assert store._receipt(job) == RESERVATION
    assert not store._receipt(job | {'state': 'dispatching'}).active


@pytest.mark.parametrize(('autocommit', 'status'), [(False, 0), (True, 2), (True, 3)])
def test_nested_or_uncommitted_dispatch_is_refused(autocommit, status):
    connection = SimpleNamespace(autocommit=autocommit, info=SimpleNamespace(transaction_status=status))
    with pytest.raises(VisionDenied, match='connection_not_idle_autocommit'):
        with VisionStore(connection)._transaction():
            pytest.fail('must not enter a savepoint')


def test_dispatch_transaction_exits_before_return():
    events = []

    @contextmanager
    def transaction():
        events.append('begin')
        yield
        events.append('commit')

    connection = SimpleNamespace(autocommit=True, info=SimpleNamespace(transaction_status=0),
                                 transaction=transaction)
    with VisionStore(connection)._transaction():
        events.append('update')
    assert events == ['begin', 'update', 'commit']


def test_missing_trusted_checks_are_fail_closed():
    assert VisionStore._source({}) == dict(source_verified=False, rendering_verified=False,
                                         access_verified=False, pii_blocked=True)
    assert VisionStore._catalog({}) == dict(capability_verified=False, credential_ready=False)


def test_schema_is_dormant_and_registered_without_collector_grants():
    root = Path(__file__).resolve().parents[1]
    schema = (root / 'deploy/sql/archiver_vision_schema.sql').read_text(encoding='utf-8')
    assert 'DEFAULT false' in schema
    assert 'identity_sha256 text NOT NULL UNIQUE' in schema
    assert 'TO tybot_vision' in schema
    assert 'TO tybot_archiver' not in schema
    assert 'TO tyslackai' not in schema
    assert 'archiver_vision_schema.sql' in (root / 'deploy/apply-schema.sh').read_text(encoding='utf-8')


class ScriptedConnection:
    """Only verifies Python/SQL binding and transaction order, NOT DB locks."""

    autocommit = True
    info = SimpleNamespace(transaction_status=0)

    def __init__(self, replies):
        self.replies = deque(replies)
        self.events = []

    @contextmanager
    def transaction(self):
        self.events.append('begin')
        try:
            yield
        except Exception:
            self.events.append('rollback')
            raise
        else:
            self.events.append('commit')

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, args=None):
        self.events.append((sql, args))

    def fetchone(self):
        return self.replies.popleft()

    def fetchall(self):
        return self.replies.popleft()


CONFIG = dict(model=POLICY.model, provider=POLICY.provider, max_image_bytes=1024,
              max_output_tokens=2000, timeout_seconds=30, daily_usd='1', job_usd='0.2',
              max_concurrency=2)
GLOBAL = dict(revision='global', enabled=True, config=CONFIG)
LOCAL = dict(revision='local', enabled=True, config={})
BOUND = replace(APPROVAL, policy_revision='global:local')
APPROVAL_ROW = dict(expires_at=BOUND.expires_at, revoked_at=None, scope=approval_scope(BOUND))
CHECKS = dict(capability_verified=True, credential_ready=True, source_verified=True,
              rendering_verified=True, access_verified=True, pii_blocked=False)


def job_row(**overrides):
    return dict(id='job', approval_id=BOUND.approval_id, scope=approval_scope(BOUND),
                max_output_tokens=2000, reserved_usd=Decimal('0.1'), state='reserved',
                workspace=PAGE.workspace, expires_at=RESERVATION.expires_at, now=NOW) | overrides


def test_reserve_checks_binding_limits_then_records_a_durable_receipt():
    conn = ScriptedConnection([GLOBAL, {'now': NOW}, LOCAL, APPROVAL_ROW, None, [], [], job_row()])
    receipt = VisionStore(conn).reserve(PAGE, approval_id=BOUND.approval_id,
                                       amount_usd=Decimal('0.1'), expires_at=RESERVATION.expires_at,
                                       checks=CHECKS)
    assert receipt.policy_revision == 'global:local'
    assert receipt.image_sha256 == APPROVAL.image_sha256
    assert conn.events[-1] == 'commit'
    insert = next(e for e in conn.events if isinstance(e, tuple) and 'INSERT INTO' in e[0])
    assert 'policy_snapshot' in insert[0]
    assert insert[1][-2] == Decimal('0.1')


@pytest.mark.parametrize(('extra_replies', 'code'), [
    ([{'id': 'duplicate'}], 'duplicate_job'),
    ([None, [{'workspace': 'mgmt', 'active': 2}]], 'concurrency_exceeded'),
    ([None, [], [{'workspace': 'mgmt', 'charged': Decimal('0.95')}]], 'daily_budget_exceeded'),
    ([None, [], [{'workspace': 'tyit', 'charged': Decimal('0.95')}]], 'daily_budget_exceeded'),
])
def test_rejected_reservation_never_inserts_or_commits(extra_replies, code):
    conn = ScriptedConnection([GLOBAL, {'now': NOW}, LOCAL, APPROVAL_ROW, *extra_replies])
    with pytest.raises(VisionDenied, match=code):
        VisionStore(conn).reserve(PAGE, approval_id=BOUND.approval_id,
                                 amount_usd=Decimal('0.1'), expires_at=RESERVATION.expires_at,
                                 checks=CHECKS)
    assert conn.events[-1] == 'rollback'
    assert not any(isinstance(e, tuple) and 'INSERT INTO' in e[0] for e in conn.events)


def test_dispatch_rechecks_then_commits_and_does_not_call_a_model():
    conn = ScriptedConnection([GLOBAL, job_row(), LOCAL, APPROVAL_ROW])
    VisionStore(conn).begin_dispatch('job', PAGE, checks=CHECKS)
    assert conn.events[-1] == 'commit'
    update = next(e for e in conn.events if isinstance(e, tuple) and e[0].lstrip().startswith('UPDATE'))
    assert "state='dispatching'" in update[0]
    assert update[1] == ('job',)


def test_global_off_is_checked_again_after_a_reservation():
    conn = ScriptedConnection([GLOBAL | {'enabled': False}, job_row(), LOCAL, APPROVAL_ROW])
    with pytest.raises(VisionDenied, match='vision_disabled'):
        VisionStore(conn).begin_dispatch('job', PAGE, checks=CHECKS)
    assert conn.events[-1] == 'rollback'
    assert not any(isinstance(e, tuple) and e[0].lstrip().startswith('UPDATE') for e in conn.events)


def test_unknown_cost_is_not_settled_as_zero():
    conn = ScriptedConnection([GLOBAL, {'id': 'job'}])
    VisionStore(conn).finish('job', actual_usd=None)
    update = next(e for e in conn.events if isinstance(e, tuple) and e[0].lstrip().startswith('UPDATE'))
    assert update[1] == ('unknown', None, 'job')


def test_scope_has_no_body_image_or_secret_fields():
    assert set(approval_scope(BOUND)) == set(asdict(BOUND)) - {'approval_id', 'expires_at', 'revoked'}
    assert not {'image', 'markdown', 'api_key', 'token', 'unreadable'} & set(approval_scope(BOUND))


def test_effective_policy_inherits_without_master_and_preserves_provenance():
    local = LOCAL | {'config': {'model': 'workspace-model'}}
    conn = ScriptedConnection([GLOBAL, local, {'config': local['config']}])
    policy, config, sources = VisionStore(conn).effective_policy('tec')
    assert policy.workspace == 'tec'
    assert config['model'] == 'workspace-model'
    assert sources['model'] == 'workspace'
    assert sources['provider'] == 'global'
    assert not policy.capability_verified and not policy.credential_ready
