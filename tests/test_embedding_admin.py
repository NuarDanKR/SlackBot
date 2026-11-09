"""B-12 임베딩 콘솔 — 서버가 막는가를 건다. 화면을 믿지 않는다.

가짜 DB 는 이 모듈이 쓰는 문장만 흉내 낸다. 지키는 것:
* 외부 프로바이더 모델을 `local` 로 적은 정책은 켜든 끄든 저장되지 않는다
* 켤 때만 차단 사유를 보고, 끄는 것은 언제나 된다
* revision 이 다르면 덮어쓰지 않는다
* 모델은 draft·외부 미승인으로 시작하고, 퇴역은 되돌리지 않는다
* 일부 워크스페이스만 보는 사람에게 남의 정책·분량이 안 보인다
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tybot.console import app as console_app
from tybot.console import embedding_admin as admin
from tybot.console import embedding_routes as routes
from tybot.console.auth import ConsoleUser

NOW = datetime(2026, 11, 9, 9, tzinfo=UTC)


class DB:
    """레지스트리·정책·색인·이력·raw_line 을 들고 있는 작은 가짜."""

    def __init__(self):
        self.models: dict[str, dict] = {}
        self.policies: dict[str, dict] = {}
        self.docs: list[dict] = []
        self.runs: list[dict] = []
        self.raw = {"tyit": {"docs": 12, "chars": 2_000_000}}
        self.executed: list[str] = []
        self.commits = 0

    def add_model(self, revision="m1", provider="openai", state="active",
                  external_approved=True, price=0.02):
        self.models[revision] = {
            "revision": revision, "provider": provider, "model": "text-embedding-3-small",
            "dimensions": 1536, "normalized": True, "state": state,
            "external_approved": external_approved, "input_price_per_mtok": price,
            "created_at": NOW, "created_by": "a@x",
        }

    def connect(self):
        return Conn(self)


class Conn:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *rest):
        if exc_type is None:
            self.db.commits += 1
        return False

    def cursor(self):
        return Cur(self)

    def rollback(self):
        pass


class Cur:
    def __init__(self, conn):
        self.connection = conn
        self.db = conn.db
        self.rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def execute(self, sql, params=()):
        db = self.db
        s = " ".join(sql.split())
        db.executed.append(s)
        self.rows = []
        if s.startswith("SELECT 1 FROM search_embedding_model_revision"):
            self.rows = [(1,)] if params[0] in db.models else []
        elif s.startswith("INSERT INTO search_embedding_model_revision"):
            rev, provider, model, dims, norm, price, created_by = params
            db.models[rev] = {
                "revision": rev, "provider": provider, "model": model, "dimensions": dims,
                "normalized": norm, "state": "draft", "external_approved": False,
                "input_price_per_mtok": price, "created_at": NOW, "created_by": created_by,
            }
        elif s.startswith("SELECT state, external_approved FROM search_embedding_model_revision"):
            m = db.models.get(params[0])
            self.rows = [dict(m)] if m else []
        elif s.startswith("SELECT workspace FROM search_embedding_workspace_policy"):
            self.rows = [{"workspace": w} for w, p in sorted(db.policies.items())
                         if p["enabled"] and p["model_revision"] == params[0]]
        elif s.startswith("UPDATE search_embedding_model_revision"):
            state, ext, rev = params
            db.models[rev].update(state=state, external_approved=ext)
        elif s.startswith("SELECT revision, provider, state, external_approved"):
            m = db.models.get(params[0])
            self.rows = [dict(m)] if m else []
        elif s.startswith("SELECT workspace, enabled, model_revision, transport, allow_channel, "
                          "allow_attachment, allow_dm"):
            p = db.policies.get(params[0])
            self.rows = [dict(p)] if p else []
        elif s.startswith("SELECT count(DISTINCT doc_path)"):
            r = db.raw.get(params[0], {"docs": 0, "chars": 0})
            self.rows = [{"docs": r["docs"], "chars": r["chars"]}]
        elif s.startswith("INSERT INTO search_embedding_workspace_policy"):
            ws = params[0]
            if ws in db.policies:
                return
            (enabled, model, transport, ch, att, ext, reason, actor) = params[1:]
            db.policies[ws] = {
                "workspace": ws, "enabled": enabled, "model_revision": model,
                "transport": transport, "allow_channel": ch, "allow_attachment": att,
                "allow_dm": False, "external_transfer_approved": ext, "revision": 1,
                "reason": reason, "updated_at": NOW, "updated_by": actor,
            }
            self.rows = [{"revision": 1}]
        elif s.startswith("UPDATE search_embedding_workspace_policy"):
            (enabled, model, transport, ch, att, ext, reason, actor, ws, expected) = params
            p = db.policies.get(ws)
            if p is None or p["revision"] != expected:
                return
            p.update(enabled=enabled, model_revision=model, transport=transport,
                     allow_channel=ch, allow_attachment=att, allow_dm=False,
                     external_transfer_approved=ext, reason=reason, updated_by=actor,
                     revision=p["revision"] + 1)
            self.rows = [{"revision": p["revision"]}]
        elif s.startswith("SELECT revision, provider, model, dimensions"):
            self.rows = [dict(m) for m in db.models.values()]
        elif s.startswith("SELECT workspace, enabled, model_revision, transport, allow_channel, "
                          "allow_attachment, external_transfer_approved"):
            self.rows = [dict(p) for _, p in sorted(db.policies.items())]
        elif s.startswith("SELECT workspace, model_revision, count(*)"):
            self.rows = list(db.docs)
        elif s.startswith("SELECT id, model_revision, trigger"):
            self.rows = list(db.runs)
        else:  # pragma: no cover - 새 문장이 생기면 가짜도 알아야 한다
            raise AssertionError(f"unexpected SQL: {s[:80]}")


def policy(**over) -> admin.PolicyChange:
    base = dict(
        workspace="tyit", expected_revision=None, enabled=True, model_revision="m1",
        transport="external", allow_channel=True, allow_attachment=True,
        external_transfer_approved=True, reason="검색 재현율 개선 (B-12)",
        actor="owner@taeyoung.com",
    )
    return admin.PolicyChange(**(base | over))


# --- 모델 ---------------------------------------------------------------------


def registration(**over) -> admin.ModelRegistration:
    base = dict(revision="oai-3s-1536", provider="openai", model="text-embedding-3-small",
                dimensions=1536, normalized=True, price_per_mtok=0.02,
                reason="B-12 첫 모델", actor="owner@taeyoung.com")
    return admin.ModelRegistration(**(base | over))


def test_model_registers_as_draft_without_external_approval():
    db = DB()
    saved = admin.register_model(registration(), connect=db.connect)
    model = db.models["oai-3s-1536"]
    assert model["state"] == "draft" and model["external_approved"] is False
    assert saved.audit["metadata"]["reason"] == "B-12 첫 모델"
    assert saved.audit["action"] == "embedding_model_register"


@pytest.mark.parametrize("over,code", [
    ({"provider": "anthropic"}, "provider_unsupported"),
    ({"dimensions": 0}, "dimensions_invalid"),
    ({"dimensions": True}, "dimensions_invalid"),
    ({"revision": "has space"}, "revision_invalid"),
    ({"model": "x; DROP"}, "model_invalid"),
    ({"price_per_mtok": -1}, "price_invalid"),
    ({"price_per_mtok": float("nan")}, "price_invalid"),
    ({"reason": "  "}, "reason_required"),
])
def test_model_registration_rejects_bad_input(over, code):
    with pytest.raises(admin.EmbeddingAdminError) as info:
        admin.register_model(registration(**over), connect=DB().connect)
    assert info.value.code == code


def test_duplicate_model_revision_is_conflict():
    db = DB()
    db.add_model("oai-3s-1536")
    with pytest.raises(admin.EmbeddingAdminError) as info:
        admin.register_model(registration(), connect=db.connect)
    assert info.value.code == "model_exists"


def test_retired_model_cannot_come_back():
    db = DB()
    db.add_model(state="retired")
    with pytest.raises(admin.EmbeddingAdminError) as info:
        admin.change_model(admin.ModelChange("m1", "active", True, "다시", "a@x"),
                           connect=db.connect)
    assert info.value.code == "model_retired"


def test_model_in_use_cannot_be_deactivated():
    db = DB()
    db.add_model()
    admin.save_policy(policy(), connect=db.connect)
    with pytest.raises(admin.EmbeddingAdminError) as info:
        admin.change_model(admin.ModelChange("m1", "retired", True, "교체", "a@x"),
                           connect=db.connect)
    assert info.value.code == "model_in_use" and info.value.blockers == ("workspace_tyit",)


def test_revoking_external_approval_is_always_allowed():
    db = DB()
    db.add_model()
    admin.save_policy(policy(), connect=db.connect)
    saved = admin.change_model(admin.ModelChange("m1", "active", False, "철회", "a@x"),
                               connect=db.connect)
    assert db.models["m1"]["external_approved"] is False
    assert saved.audit["metadata"]["previous_external_approved"] is True


def test_supported_providers_match_adapter_map(monkeypatch):
    import tybot.gateway.providers.openai_embeddings as oe

    monkeypatch.setattr(oe, "_resolve_key", lambda: None)
    assert set(admin.SUPPORTED_PROVIDERS) == set(oe.build_embedding_providers())


# --- 워크스페이스 정책 --------------------------------------------------------


def test_enable_external_policy_with_both_approvals():
    db = DB()
    db.add_model()
    saved = admin.save_policy(policy(), connect=db.connect)
    assert db.policies["tyit"]["enabled"] is True and saved.view["revision"] == 1
    assert db.policies["tyit"]["allow_dm"] is False
    assert saved.audit["metadata"]["transport"] == "external"


@pytest.mark.parametrize("model_over,policy_over,blocker", [
    ({}, {"transport": "local", "external_transfer_approved": False},
     "provider_requires_external_transport"),
    ({"external_approved": False}, {}, "model_external_not_approved"),
    ({}, {"external_transfer_approved": False}, "workspace_external_not_approved"),
    ({"state": "draft"}, {}, "model_not_active"),
    ({}, {"model_revision": None}, "model_required"),
    ({}, {"model_revision": "nope"}, "model_unknown"),
    ({}, {"allow_channel": False, "allow_attachment": False}, "nothing_allowed"),
])
def test_activation_blockers(model_over, policy_over, blocker):
    db = DB()
    db.add_model(**model_over)
    with pytest.raises(admin.EmbeddingAdminError) as info:
        admin.save_policy(policy(**policy_over), connect=db.connect)
    assert info.value.code == "activation_blocked"
    assert blocker in info.value.blockers
    assert "tyit" not in db.policies


def test_external_provider_declared_local_is_refused_even_when_disabled():
    """끈 채로 잘못 적어 두면, 나중에 켜는 사람은 transport 칸을 다시 안 본다."""
    db = DB()
    db.add_model()
    with pytest.raises(admin.EmbeddingAdminError) as info:
        admin.save_policy(policy(enabled=False, transport="local",
                                 external_transfer_approved=False), connect=db.connect)
    assert info.value.blockers == ("provider_requires_external_transport",)


def test_disabling_is_always_allowed_even_if_model_was_unapproved():
    db = DB()
    db.add_model()
    admin.save_policy(policy(), connect=db.connect)
    db.models["m1"]["external_approved"] = False
    saved = admin.save_policy(policy(expected_revision=1, enabled=False), connect=db.connect)
    assert db.policies["tyit"]["enabled"] is False and saved.view["revision"] == 2


def test_stale_revision_does_not_overwrite():
    db = DB()
    db.add_model()
    admin.save_policy(policy(), connect=db.connect)
    admin.save_policy(policy(expected_revision=1, reason="첫 수정"), connect=db.connect)
    with pytest.raises(admin.EmbeddingAdminError) as info:
        admin.save_policy(policy(expected_revision=1, enabled=False), connect=db.connect)
    assert info.value.code == "policy_conflict"
    assert db.policies["tyit"]["enabled"] is True


def test_creating_existing_row_is_conflict_not_overwrite():
    db = DB()
    db.add_model()
    admin.save_policy(policy(), connect=db.connect)
    with pytest.raises(admin.EmbeddingAdminError) as info:
        admin.save_policy(policy(enabled=False), connect=db.connect)
    assert info.value.code == "policy_conflict"


def test_external_approval_requires_external_transport():
    with pytest.raises(admin.EmbeddingAdminError) as info:
        admin.save_policy(policy(transport="local"), connect=DB().connect)
    assert info.value.code == "external_approval_requires_external_transport"


def test_preview_writes_nothing_and_estimates_cost():
    db = DB()
    db.add_model(price=0.02)
    said = admin.preview_policy(policy(), connect=db.connect)
    assert said["savable"] is True and said["currentRevision"] is None
    assert said["estimate"]["chars"] == 2_000_000
    assert said["estimate"]["estimatedCostUsd"] == 0.04
    assert not any(s.startswith(("INSERT", "UPDATE")) for s in db.executed)


def test_preview_without_price_reports_unknown_cost():
    db = DB()
    db.add_model(price=None)
    said = admin.preview_policy(policy(), connect=db.connect)
    assert said["estimate"]["estimatedCostUsd"] is None


def test_database_error_text_does_not_escape():
    def broken():
        raise RuntimeError("postgresql://u:secret@h/db")

    with pytest.raises(admin.EmbeddingAdminError) as info:
        admin.save_policy(policy(), connect=broken)
    assert info.value.code == "policy_unavailable" and "secret" not in str(info.value)


# --- 현황 --------------------------------------------------------------------


def test_status_scopes_policies_and_hides_runs_for_partial_viewers():
    db = DB()
    db.add_model()
    admin.save_policy(policy(), connect=db.connect)
    admin.save_policy(policy(workspace="mgmt"), connect=db.connect)
    db.docs = [
        {"workspace": "tyit", "model_revision": "m1", "current_docs": 3, "chunks": 9,
         "failed_docs": 0, "last_indexed": NOW},
        {"workspace": "mgmt", "model_revision": "m1", "current_docs": 5, "chunks": 20,
         "failed_docs": 1, "last_indexed": NOW},
    ]
    db.runs = [{"id": 1, "model_revision": "m1", "trigger": "timer", "requested_by": "timer",
                "state": "succeeded", "error_code": "", "stopped_reason": "",
                "requested_count": 8, "published_count": 8, "embed_calls": 2,
                "embedded_chars": 100, "embedded_chunks": 29, "cost_usd": None,
                "started_at": NOW, "finished_at": NOW}]
    full = admin.status(workspaces=None, connect=db.connect)
    assert [p["workspace"] for p in full["policies"]] == ["mgmt", "tyit"]
    assert len(full["runs"]) == 1 and full["runs"][0]["costUsd"] is None
    partial = admin.status(workspaces=frozenset({"tyit"}), connect=db.connect)
    assert [p["workspace"] for p in partial["policies"]] == ["tyit"]
    assert [c["workspace"] for c in partial["coverage"]] == ["tyit"]
    assert partial["runs"] == []


# --- 라우트 -------------------------------------------------------------------


def _router(db, audits):
    router = routes.build_router(
        current_user=console_app.current_user,
        require_admin=console_app._require_admin,
        check_write_request=console_app._check_write_request,
        audit_event=lambda **kw: audits.append(kw),
        connect=db.connect,
    )
    return {(next(iter(r.methods)), r.path): r.endpoint for r in router.routes}


def _user(*, role="admin", workspaces=("tyit",), all_workspaces=True) -> ConsoleUser:
    return ConsoleUser(email="owner@taeyoung.com", name="운영자", role=role,
                       workspaces=frozenset(workspaces), all_workspaces=all_workspaces)


def _request(csrf="1") -> Request:
    headers = [(b"host", b"console.example"), (b"origin", b"https://console.example")]
    if csrf:
        headers.append((b"x-tybot-csrf", csrf.encode()))
    return Request({"type": "http", "method": "PUT", "scheme": "https", "path": "/",
                    "headers": headers, "server": ("console.example", 443),
                    "client": ("127.0.0.1", 1)})


def policy_body(**over) -> routes.PolicyBody:
    base = {"workspace": "tyit", "enabled": True, "modelRevision": "m1",
            "transport": "external", "externalTransferApproved": True,
            "reason": "검색 재현율 개선"}
    return routes.PolicyBody(**(base | over))


def test_route_saves_policy_with_logged_in_actor_and_audits():
    db, audits = DB(), []
    db.add_model()
    endpoints = _router(db, audits)
    said = endpoints[("PUT", "/api/embedding/policy")](policy_body(), _request(), _user())
    assert said["audited"] is True and said["revision"] == 1
    assert db.policies["tyit"]["updated_by"] == "owner@taeyoung.com"
    assert audits[0]["action"] == "embedding_policy_save"


def test_route_body_cannot_carry_actor():
    with pytest.raises(ValueError):
        routes.PolicyBody(**({"workspace": "tyit", "enabled": False, "transport": "external",
                              "reason": "x", "actor": "someone@else"}))


def test_route_requires_csrf_and_admin():
    db, audits = DB(), []
    db.add_model()
    endpoints = _router(db, audits)
    save = endpoints[("PUT", "/api/embedding/policy")]
    with pytest.raises(HTTPException) as info:
        save(policy_body(), _request(csrf=""), _user())
    assert info.value.status_code == 403
    with pytest.raises(HTTPException) as info:
        save(policy_body(), _request(), _user(role="developer", all_workspaces=False))
    assert info.value.status_code == 403
    assert "tyit" not in db.policies


def test_route_refuses_workspace_outside_scope():
    db, audits = DB(), []
    db.add_model()
    endpoints = _router(db, audits)
    user = _user(workspaces=("mgmt",), all_workspaces=False)
    # 관리자 검사가 먼저지만, 범위 검사가 따로 있는지도 본다
    import tybot.console.embedding_routes as module

    router = module.build_router(
        current_user=console_app.current_user, require_admin=lambda u: None,
        check_write_request=lambda r: None, audit_event=lambda **k: None, connect=db.connect,
    )
    save = {r.path: r.endpoint for r in router.routes if "PUT" in r.methods}["/api/embedding/policy"]
    with pytest.raises(HTTPException) as info:
        save(policy_body(), _request(), user)
    assert info.value.status_code == 403 and info.value.detail["code"] == "workspace_forbidden"
    assert endpoints  # 등록 확인


def test_route_maps_blockers_to_422_and_conflict_to_409():
    db, audits = DB(), []
    db.add_model(external_approved=False)
    endpoints = _router(db, audits)
    save = endpoints[("PUT", "/api/embedding/policy")]
    with pytest.raises(HTTPException) as info:
        save(policy_body(), _request(), _user())
    assert info.value.status_code == 422
    assert info.value.detail["blockers"] == ["model_external_not_approved"]
    db.models["m1"]["external_approved"] = True
    save(policy_body(), _request(), _user())
    with pytest.raises(HTTPException) as info:
        save(policy_body(), _request(), _user())
    assert info.value.status_code == 409


def test_route_model_register_needs_all_workspaces():
    db = DB()
    import tybot.console.embedding_routes as module

    router = module.build_router(
        current_user=console_app.current_user, require_admin=lambda u: None,
        check_write_request=lambda r: None, audit_event=lambda **k: None, connect=db.connect,
    )
    post = {r.path: r.endpoint for r in router.routes if "POST" in r.methods}[
        "/api/embedding/models"]
    body = routes.ModelBody(revision="m2", provider="openai", model="text-embedding-3-small",
                            dimensions=1536, reason="등록")
    with pytest.raises(HTTPException) as info:
        post(body, _request(), _user(all_workspaces=False))
    assert info.value.status_code == 403 and "m2" not in db.models


def test_console_app_registers_embedding_routes():
    paths = set()
    for route in console_app.app.routes:
        # 최신 FastAPI 는 include_router 를 `_IncludedRouter` 로 감싼다.
        inner = getattr(route, "original_router", None)
        if inner is not None:
            paths |= {getattr(r, "path", "") for r in inner.routes}
        else:
            paths.add(getattr(route, "path", ""))
    for path in ("/api/embedding/status", "/api/embedding/models",
                 "/api/embedding/models/{revision}", "/api/embedding/policy",
                 "/api/embedding/policy/preview"):
        assert path in paths, path
