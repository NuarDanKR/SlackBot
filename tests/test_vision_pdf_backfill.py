"""B-80 PDF 페이지 Vision 운영 연결 — 후보·dry-run·apply (실제 모델 호출 없음).

거는 것:
- 혼합 PDF 에서 **실패·미확인 쪽만** 그리고 보낸다. 로컬에서 읽은 쪽은 보내지 않는다
- timeout·429 뒤 자동 재청구 없음 — 다시 돌려도 같은 쪽은 `duplicate_job`
- 원본 없음·XLS·정책 격리·PII 알림·검사 결과 모름·목록 잘림은 대기열에 들어가지 않는다
- dry-run 은 아무것도 열거나 보내지 않는다
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tybot.archive import vision_candidates as vc
from tybot.archive import vision_pdf_batch as batch
from tybot.archive.vision_policy import VisionPolicy
from tybot.archive.vision_store import VisionDenied, approval_scope, identity
from tybot.gateway.base import LLMResponse, ModelSpec, ProviderTimeout, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router

WS, CH = "tyit", "C0TEST0001"
NOW = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)
PDF = b"%PDF-1.7 synthetic mixed document"
PDF_SHA = hashlib.sha256(PDF).hexdigest()


def _stage(archive: Path, file_id: str, **meta) -> Path:
    folder = archive.parent / "staging" / "workspaces" / WS / "channels" / CH / "attachments" / file_id
    folder.mkdir(parents=True, exist_ok=True)
    obj = archive.parent / "objects" / f"{file_id}.bin"
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(PDF)
    base = {
        "status": "converted", "original_state": "retained", "conversion_state": "partial",
        "name": f"{file_id}.pdf", "filetype": "pdf", "mimetype": "application/pdf",
        "object_path": str(obj), "sha256": PDF_SHA, "screen_version": 2,
        "screen_result": "passed", "coverage_unit": "page", "coverage_total": 5,
        "coverage_converted": 3, "missing_units": [2], "unverified_units": [4],
    }
    base.update(meta)
    (folder / "metadata.json").write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return obj


@pytest.fixture
def archive(tmp_path) -> Path:
    root = tmp_path / "archive"
    root.mkdir()
    return root


# --- 후보 ----------------------------------------------------------------------


def test_only_failed_and_unverified_pages_of_a_mixed_pdf_are_candidates(archive):
    _stage(archive, "F0MIXED0001")
    (doc,) = vc.plan(archive)
    assert doc.eligible
    assert [(p.page, p.reason) for p in doc.pages] == [(2, "missing"), (4, "unverified")]


@pytest.mark.parametrize(("meta", "code"), [
    ({"filetype": "xls", "mimetype": "application/vnd.ms-excel", "name": "a.xls"}, vc.NOT_PDF),
    ({"original_state": "missing"}, vc.ORIGINAL_MISSING),
    ({"object_path": None}, vc.ORIGINAL_MISSING),
    ({"status": "pii_refused", "conversion_state": "blocked"}, vc.POLICY_ISOLATED),
    ({"screen_result": "blocked"}, vc.POLICY_ISOLATED),
    ({"screen_result": "passed_with_notice"}, vc.SCREEN_NOTICE),  # 부분 PII 검사 fail-closed
    ({"screen_result": ""}, vc.SCREEN_UNKNOWN),
    ({"coverage_unit": ""}, vc.COVERAGE_UNKNOWN),
    ({"missing_units": list(range(1, 51)), "coverage_total": 80}, vc.LIST_TRUNCATED),
    ({"missing_units": [], "unverified_units": []}, vc.NO_FAILED_PAGES),
])
def test_documents_that_must_not_enter_the_vision_queue(archive, meta, code):
    _stage(archive, "F0EXCLUDE01", **meta)
    plans = vc.plan(archive)
    if meta.get("conversion_state") == "blocked":
        # 차단된 문서는 실패·부분 성공 목록에도 오르지 않는다(대기열 밖)
        assert plans == [] or plans[0].code == code
        return
    (doc,) = plans
    assert doc.code == code and not doc.eligible and doc.pages == ()


def test_a_deleted_original_object_is_original_missing(archive):
    obj = _stage(archive, "F0GONE00001")
    obj.unlink()
    (doc,) = vc.plan(archive)
    assert doc.code == vc.ORIGINAL_MISSING


def _plan(channel, file_id, code, pages=((2, "missing"),), sha=PDF_SHA):
    return vc.DocumentPlan(WS, channel, file_id, f"{file_id}.pdf", sha, None, code,
                           tuple(vc.PageCandidate(n, r) for n, r in pages))


@pytest.mark.parametrize("blocking", [vc.SCREEN_UNKNOWN, vc.SCREEN_NOTICE, vc.POLICY_ISOLATED])
@pytest.mark.parametrize("other_channel", [CH, "C0OTHER0001"])
def test_a_file_with_a_conflicting_pii_screen_is_held_everywhere(blocking, other_channel):
    """한 정본은 통과, 다른 정본은 알림·모름·격리 → 통과한 쪽도 보내지 않는다(fail-closed)."""
    out = vc._reconcile([_plan(CH, "F1", vc.ELIGIBLE), _plan(other_channel, "F1", blocking, ())])
    held = [p for p in out if p.channel_id == CH and p.code != blocking]
    assert [(p.code, p.pages) for p in held] == [(vc.SCREEN_CONFLICT, ())]
    assert not any(p.eligible for p in out)


def test_duplicate_plans_in_one_channel_count_once_but_other_files_stay():
    out = vc._reconcile([
        _plan(CH, "F1", vc.ELIGIBLE), _plan(CH, "F1", vc.ELIGIBLE),
        _plan("C0OTHER0001", "F1", vc.ELIGIBLE),  # 다른 채널은 따로 센다
        _plan(CH, "F2", vc.ELIGIBLE),
    ])
    assert [(p.channel_id, p.file_id) for p in out] == [
        (CH, "F1"), ("C0OTHER0001", "F1"), (CH, "F2")]


def test_fully_converted_documents_are_not_listed(archive):
    _stage(archive, "F0DONE00001", conversion_state="succeeded")
    assert vc.plan(archive) == []


def test_dry_run_lists_without_opening_or_sending(archive, monkeypatch):
    _stage(archive, "F0MIXED0001")
    _stage(archive, "F0XLS000001", filetype="xls", mimetype="application/vnd.ms-excel")
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("dry-run 이 원본을 열었다"))
    report = batch.dry_run(archive)
    assert report["eligible_documents"] == 1 and report["pages"] == 2
    assert report["codes"] == {"eligible": 1, "not_pdf": 1}


# --- apply ---------------------------------------------------------------------


class FakeRenderer:
    def __init__(self):
        self.asked = []

    def renderer_version(self):
        return "fake-renderer:1"

    def render_pages(self, pdf, pages, *, max_image_bytes):
        self.asked.append(list(pages))
        return {n: f"png-page-{n}".encode() for n in pages}


class FakeStore:
    """VisionStore 와 같은 계약. 원장 identity UNIQUE 를 흉내 낸다."""

    def __init__(self):
        self.identities = set()
        self.jobs = {}
        self.records = []
        self.approvals = {}

    def effective_policy(self, workspace):
        policy = VisionPolicy(
            workspace=workspace, revision="g:l", enabled=True, workspace_enabled=True,
            model="vision-m", provider="google", max_image_bytes=5_000_000,
            max_output_tokens=512, timeout_seconds=30, sensitivity=Sensitivity.INTERNAL,
        )
        return policy, {}, {}

    def approve(self, approval, *, actor):
        self.approvals[approval.approval_id] = approval

    def reserve(self, page, *, approval_id, amount_usd, expires_at, checks):
        assert checks["catalog_max_sensitivity"] is Sensitivity.PUBLIC
        key = identity(approval_scope(self.approvals[approval_id]))
        if key in self.identities:
            raise VisionDenied("duplicate_job")
        self.identities.add(key)
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs[job_id] = "reserved"
        from types import SimpleNamespace

        return SimpleNamespace(reservation_id=job_id)

    def begin_dispatch(self, job_id, page, *, checks):
        from types import SimpleNamespace

        assert self.jobs[job_id] == "reserved"
        self.jobs[job_id] = "dispatching"
        return SimpleNamespace(
            model="vision-m", provider="google", max_output_tokens=512,
            timeout_seconds=30.0, sensitivity=Sensitivity.INTERNAL,
        )

    def finish(self, job_id, *, actual_usd):
        self.jobs[job_id] = "unknown" if actual_usd is None else "settled"

    def record_page(self, job_id, result, *, channel_id):
        assert channel_id == CH
        self.records.append((job_id, result.page_number, result.status, result.error_code))
        return f"rev-{job_id}"


class Provider:
    """호출 순서별 결말. 429·timeout 은 예외로 낸다. 페이지 번호는 렌더 순서(2, 4)다."""

    name = "google"
    PAGES = (2, 4)

    def __init__(self, outcomes):
        self.outcomes = dict(outcomes)
        self.calls = []

    def complete(self, spec, messages, *, max_tokens, temperature, **_extra):
        index = len(self.calls)
        self.calls.append(index + 1)
        outcome = self.outcomes.get(index + 1, "ok")
        if outcome == "timeout":
            raise ProviderTimeout("timeout")
        if outcome == "429":
            raise RuntimeError("429 Too Many Requests")
        body = {
            "page_number": self.PAGES[index % len(self.PAGES)], "readable": True,
            "markdown": "\n".join(["| 항목 | 금액 |", "|---|---|", "| 공사비 | 1,200 |"]),
            "unreadable": [], "quality": [],
        }
        return LLMResponse(text=json.dumps(body, ensure_ascii=False), model=spec.model,
                           provider=spec.provider, input_tokens=10, output_tokens=5, cost_usd=0.0)


def _router(provider, tmp_path) -> Router:
    spec = ModelSpec(model="vision-m", provider="google", input_price_per_mtok=0.0,
                     output_price_per_mtok=0.0, max_sensitivity=Sensitivity.INTERNAL)
    return Router(providers={"google": provider}, registry={"vision-m": spec},
                  cost_guard=CostGuard(1.0, state_path=tmp_path / "cost.json"),
                  default_model="vision-m", fallback_models=())


def _apply(archive, store, router, renderer, *, scope=lambda ws, ch: (ws, ch) == (WS, CH), **kw):
    return batch.apply(
        archive, store=store, router=router, renderer=renderer, channel_scope=scope,
        catalog_checks=lambda policy: {
            "capability_verified": True, "credential_ready": True,
            "catalog_max_sensitivity": Sensitivity.PUBLIC,
        },
        actor="operator@taeyoung.com", reason="B-80 소급", now=NOW,
        amount_usd=Decimal("0.01"), **kw,
    )


def test_apply_renders_and_sends_only_the_failed_pages(archive, tmp_path):
    _stage(archive, "F0MIXED0001")
    store, renderer, provider = FakeStore(), FakeRenderer(), Provider({})
    stats = _apply(archive, store, _router(provider, tmp_path), renderer)
    assert renderer.asked == [[2, 4]]          # PDF 전체가 아니라 실패 쪽만 그린다
    assert len(provider.calls) == 2 and stats.sent == 2
    assert [r[1] for r in store.records] == [2, 4]


@pytest.mark.parametrize("failure", ["timeout", "429"])
def test_a_provider_failure_is_never_recharged(archive, tmp_path, failure):
    _stage(archive, "F0MIXED0001")
    store, renderer = FakeStore(), FakeRenderer()
    provider = Provider({1: failure})
    router = _router(provider, tmp_path)
    first = _apply(archive, store, router, renderer)
    assert len(provider.calls) == 2  # 실패한 쪽도 한 번만 나갔다(내부 재시도 없음)
    assert store.jobs["job-1"] == "unknown"  # 비용을 모른다 — 0 으로 적지 않는다
    assert store.records[0][2] == "failed"
    # 다시 돌려도 같은 쪽은 원장이 막는다 — 자동 재청구 없음
    second = _apply(archive, store, router, renderer)
    assert len(provider.calls) == 2
    assert second.sent == 0 and second.skipped["duplicate_job"] == 2
    assert first.sent == 2


def test_apply_respects_the_page_limit(archive, tmp_path):
    _stage(archive, "F0MIXED0001")
    store, provider = FakeStore(), Provider({})
    stats = _apply(archive, store, _router(provider, tmp_path), FakeRenderer(), max_pages=1)
    assert stats.sent == 1 and len(provider.calls) == 1


def test_a_changed_original_is_not_sent(archive, tmp_path):
    obj = _stage(archive, "F0MIXED0001")
    obj.write_bytes(PDF + b"changed")
    store, provider = FakeStore(), Provider({})
    stats = _apply(archive, store, _router(provider, tmp_path), FakeRenderer())
    assert provider.calls == [] and stats.skipped["original_changed"] == 2


def test_a_missing_renderer_sends_nothing(archive, tmp_path):
    from tybot.archive.vision_pdf_render import RendererUnavailable

    class NoRenderer(FakeRenderer):
        def renderer_version(self):
            raise RendererUnavailable("pypdfium2 미설치")

    _stage(archive, "F0MIXED0001")
    store, provider = FakeStore(), Provider({})
    stats = _apply(archive, store, _router(provider, tmp_path), NoRenderer())
    assert provider.calls == [] and stats.skipped["RendererUnavailable"] == 2


def test_apply_needs_an_actor_and_reason(archive, tmp_path):
    with pytest.raises(ValueError):
        batch.apply(archive, store=FakeStore(), router=None, renderer=FakeRenderer(),
                    catalog_checks=lambda p: {}, channel_scope=lambda ws, ch: True,
                    actor="", reason="x", now=NOW,
                    amount_usd=Decimal("0.01"))


# --- 스크립트 ------------------------------------------------------------------


def _script():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "vision_pdf_backfill.py"
    spec = importlib.util.spec_from_file_location("vision_pdf_backfill", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_defaults_to_dry_run(archive, monkeypatch, capsys):
    _stage(archive, "F0MIXED0001")
    monkeypatch.setenv("ARCHIVE_DIR", str(archive))
    monkeypatch.setenv("TYBOT_ENV_FILE", str(archive / "none.env"))
    assert _script().main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pages"] == 2


@pytest.mark.parametrize("args", [
    ["--apply", "--workspace", WS, "--reason", "x", "--max-pages", "5", "--amount-usd", "0.01"],
    ["--apply", "--workspace", WS, "--actor", "a", "--reason", "x", "--max-pages", "0",
     "--amount-usd", "0.01"],
    ["--apply", "--workspace", WS, "--actor", "a", "--reason", "x", "--max-pages", "5",
     "--amount-usd", "0"],
])
def test_apply_refuses_without_actor_limit_or_known_amount(archive, monkeypatch, args):
    monkeypatch.setenv("ARCHIVE_DIR", str(archive))
    monkeypatch.setenv("TYBOT_ENV_FILE", str(archive / "none.env"))
    assert _script().main(args) == 2


# --- access_verified 는 실제 조회 결과다 -------------------------------------------


@pytest.mark.parametrize("scope", [
    lambda ws, ch: False,                              # 등록 해제·수집 범위 밖
    lambda ws, ch: (_ for _ in ()).throw(RuntimeError("db down")),  # 조회 실패
])
def test_a_channel_outside_the_current_scope_is_never_opened(archive, tmp_path, monkeypatch, scope):
    _stage(archive, "F0MIXED0001")
    opened = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: opened.append(self) or real(self))
    store, renderer, provider = FakeStore(), FakeRenderer(), Provider({})
    stats = _apply(archive, store, _router(provider, tmp_path), renderer, scope=scope)
    assert provider.calls == [] and renderer.asked == []
    assert not any(p.suffix == ".bin" for p in opened)  # 원본을 열지도 않았다
    assert stats.skipped["channel_not_in_scope"] == 2


def test_an_original_outside_the_archive_is_refused(archive, tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}.bin"
    outside.write_bytes(PDF)
    _stage(archive, "F0MIXED0001", object_path=str(outside))
    store, provider = FakeStore(), Provider({})
    stats = _apply(archive, store, _router(provider, tmp_path), FakeRenderer())
    assert provider.calls == [] and stats.skipped["original_outside_archive"] == 2


def test_the_batch_never_hardcodes_access():
    source = (Path(__file__).resolve().parents[1] / "src/tybot/archive/vision_pdf_batch.py").read_text(
        encoding="utf-8"
    )
    before = source.split('"access_verified": True')[0]
    assert "access = access_code(archive_dir, doc, channel_scope)" in before
