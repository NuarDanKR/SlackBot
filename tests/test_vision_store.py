from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_vision_policy import APPROVAL, NOW, PAGE, POLICY, RESERVATION

from tybot.archive.vision_store import VisionDenied, VisionStore, approval_scope, identity, money
from tybot.gateway.base import Sensitivity


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
                                         access_verified=False, pii_blocked=True,
                                         catalog_max_sensitivity=None)
    assert VisionStore._catalog({}) == dict(capability_verified=False, credential_ready=False)


def test_schema_is_dormant_and_registered_without_collector_grants():
    root = Path(__file__).resolve().parents[1]
    schema = (root / 'deploy/sql/archiver_vision_schema.sql').read_text(encoding='utf-8')
    assert 'DEFAULT false' in schema
    assert 'identity_sha256 text NOT NULL UNIQUE' in schema
    assert 'TO tybot_vision' in schema
    assert 'TO tybot_archiver' not in schema
    # Master 는 검색 투영 하나만 **읽는다**(B-80 2026-11-19). 원장·revision 은 열지 않는다.
    master = [line.strip() for line in schema.splitlines() if 'TO tyslackai' in line]
    assert master == ['GRANT SELECT ON TABLE pdf_page_search_entry TO tyslackai;']
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
              max_concurrency=2, input_usd_per_mtok='1', output_usd_per_mtok='10')
GLOBAL = dict(revision='global', enabled=True, config=CONFIG)
LOCAL = dict(revision='local', enabled=True, config={})
BOUND = replace(APPROVAL, policy_revision='global:local')
APPROVAL_ROW = dict(expires_at=BOUND.expires_at, revoked_at=None, scope=approval_scope(BOUND))
CHECKS = dict(capability_verified=True, credential_ready=True, source_verified=True,
              rendering_verified=True, access_verified=True, pii_blocked=False,
              catalog_max_sensitivity=Sensitivity.PUBLIC)


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


@pytest.mark.parametrize(('config', 'amount', 'code'), [
    # 요금표가 없으면 정산할 수 없다 — 0 으로 메우지 않는다
    ({k: v for k, v in CONFIG.items() if k != 'output_usd_per_mtok'}, '0.1', 'tariff_unconfigured'),
    (CONFIG | {'input_usd_per_mtok': 'abc'}, '0.1', 'tariff_unconfigured'),
    # 0 원 요금표는 정산을 0 으로 만든다 — 예산 우회
    (CONFIG | {'output_usd_per_mtok': '0'}, '0.1', 'tariff_unconfigured'),
    # 출력 상한(2000 tok × $10/M = $0.02)보다 작은 예약은 보수적 상한이 아니다
    (CONFIG, '0.019', 'reservation_below_tariff'),
])
def test_reservation_requires_a_tariff_and_a_bound_that_covers_it(config, amount, code):
    conn = ScriptedConnection([GLOBAL | {'config': config}, {'now': NOW}, LOCAL, APPROVAL_ROW])
    with pytest.raises(VisionDenied, match=code):
        VisionStore(conn).reserve(PAGE, approval_id=BOUND.approval_id,
                                 amount_usd=Decimal(amount), expires_at=RESERVATION.expires_at,
                                 checks=CHECKS)
    assert conn.events[-1] == 'rollback'
    assert not any(isinstance(e, tuple) and 'INSERT INTO' in e[0] for e in conn.events)


def test_dispatch_carries_the_tariff_bound_at_reservation_not_the_current_one():
    snapshot = {'global_config': CONFIG | {'input_usd_per_mtok': '2', 'output_usd_per_mtok': '20'},
                'effective': CONFIG | {'input_usd_per_mtok': '0.0001', 'output_usd_per_mtok': '0.0001'}}
    conn = ScriptedConnection([GLOBAL, job_row(policy_snapshot=snapshot), LOCAL, APPROVAL_ROW])
    grant = VisionStore(conn).begin_dispatch('job', PAGE, checks=CHECKS)
    assert (grant.input_usd_per_mtok, grant.output_usd_per_mtok) == (Decimal('2'), Decimal('20'))
    assert grant.charge_usd(500_000, 50_000) == Decimal('2')
    assert grant.charge_usd(1000, 0) is None  # usage 누락을 공짜로 읽지 않는다
    assert grant.charge_usd(1000, 500) == Decimal('0.012')
    assert grant.charge_usd(None, 500) is None


@pytest.mark.parametrize('override', [
    {'output_usd_per_mtok': '0.0001'}, {'model': 'cheaper-model'}, {'provider': 'other'},
])
def test_a_workspace_row_cannot_override_the_model_or_tariff(override):
    """워크스페이스 관리자가 요금표·모델을 바꾸면 전역 하루 예산의 근거가 갈라진다."""
    conn = ScriptedConnection([GLOBAL, {'now': NOW}, LOCAL | {'config': override}, APPROVAL_ROW])
    with pytest.raises(VisionDenied):
        VisionStore(conn).reserve(PAGE, approval_id=BOUND.approval_id,
                                 amount_usd=Decimal('0.1'), expires_at=RESERVATION.expires_at,
                                 checks=CHECKS)
    assert not any(isinstance(e, tuple) and 'INSERT INTO' in e[0] for e in conn.events)


def test_an_old_reservation_without_a_tariff_settles_as_unknown_not_zero():
    conn = ScriptedConnection([GLOBAL, job_row(), LOCAL, APPROVAL_ROW])
    grant = VisionStore(conn).begin_dispatch('job', PAGE, checks=CHECKS)
    assert grant.charge_usd(1000, 500) is None


def test_dispatch_rechecks_then_commits_and_does_not_call_a_model():
    conn = ScriptedConnection([GLOBAL, job_row(), LOCAL, APPROVAL_ROW])
    grant = VisionStore(conn).begin_dispatch('job', PAGE, checks=CHECKS)
    assert conn.events[-1] == 'commit'
    assert grant.reservation.reservation_id == 'job'
    assert (grant.model, grant.provider) == (POLICY.model, POLICY.provider)
    assert grant.max_output_tokens == CONFIG['max_output_tokens']
    assert grant.timeout_seconds == CONFIG['timeout_seconds']
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


@pytest.mark.parametrize('value', ['public', 'confidential'])
def test_a_workspace_row_cannot_change_the_sensitivity(value):
    """민감도를 낮추면 전역이 막은 모델 카탈로그를 통과한다(2026-11-19 보안 검토).

    예약·전송 모두 `_effective` 를 지나므로 거기서 막는다. 높이는 것도 갈라진 정책이다.
    """
    conn = ScriptedConnection([GLOBAL, {'now': NOW}, LOCAL | {'config': {'sensitivity': value}},
                               APPROVAL_ROW])
    with pytest.raises(VisionDenied) as caught:
        VisionStore(conn).reserve(PAGE, approval_id=BOUND.approval_id,
                                 amount_usd=Decimal('0.1'), expires_at=RESERVATION.expires_at,
                                 checks=CHECKS)
    assert str(caught.value) == 'workspace_override_forbidden' or \
        getattr(caught.value, 'code', '') == 'workspace_override_forbidden'
    assert not any(isinstance(e, tuple) and 'INSERT INTO' in e[0] for e in conn.events)
