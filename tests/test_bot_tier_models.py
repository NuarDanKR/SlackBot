"""봇 등급별 모델 (B-96, 2026-11-12 오너 결정) — VIPBot 은 모든 단계가 TYBot 보다 한 단계 위.

| 단계 | TYBot | VIPBot |
|---|---|---|
| 판단(분해·전문 봇 선택) | Haiku(`CLASSIFIER_MODEL`) | Sonnet |
| 전문 봇(Hermes) | Sonnet | Opus |
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tybot import answer as answer_module
from tybot import intent, specialist_router

ROOT = Path(__file__).resolve().parents[1] / "src/tybot"


def test_the_tier_table():
    assert answer_module.TYBOT_SPECIALIST_MODEL == "claude-sonnet-5-5"
    assert answer_module.VIP_PLANNER_MODEL == "claude-sonnet-5-5"
    assert answer_module.VIP_SPECIALIST_MODEL == "claude-opus-5-5"
    assert intent.CLASSIFIER_MODEL.startswith("claude-haiku")


class _Router:
    def __init__(self):
        self.models = []

    def complete(self, messages, *, model=None, **_kw):
        self.models.append(model)
        raise RuntimeError("stop")  # 모델 선택만 본다


def test_the_planner_uses_the_engine_tier_model():
    tybot = _Router()
    intent.plan("현장 공정 회의 내용 정리해줘", tybot, allow_rule_fallback=True)
    vip = _Router()
    intent.plan(
        "현장 공정 회의 내용 정리해줘", vip, allow_rule_fallback=True,
        planner_model=answer_module.VIP_PLANNER_MODEL,
    )
    assert tybot.models[:1] == [intent.CLASSIFIER_MODEL]
    assert vip.models[:1] == ["claude-sonnet-5-5"]


def test_the_engine_passes_its_planner_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(answer_module, "plan", lambda q, r, **kw: seen.update(kw) or [])
    engine = answer_module.AnswerEngine(
        SimpleNamespace(), SimpleNamespace(), planner_model="claude-sonnet-5-5"
    )
    engine.plan("질문")
    assert seen["planner_model"] == "claude-sonnet-5-5"
    default = answer_module.AnswerEngine(SimpleNamespace(), SimpleNamespace())
    default.plan("질문")
    assert seen["planner_model"] == ""


def test_the_caller_tier_model_reaches_the_external_hermes_adapter(monkeypatch):
    from tests.test_master_orchestration import _row, _task
    from tybot.specialist_tools import ToolBudget, Touched

    chosen = _row(key="hermes", execution_mode="tools")
    monkeypatch.setattr(
        specialist_router, "available_for_routes", lambda workspace, *, route_modes: [chosen]
    )
    monkeypatch.setattr(
        specialist_router, "capabilities_of", lambda specialist: ("internal_document_qa",)
    )
    made = {}

    class External:
        touched = Touched(documents=[object()])
        budget = ToolBudget()
        last_model = "claude-opus-5-5"
        last_cost_usd = 0.01
        last_stop_reason = ""

        def complete(self, request):
            return "외부 Hermes 답변"

    def factory(specialist, **kwargs):
        made.update(kwargs)
        return External()

    outcome = specialist_router.serve(
        _task(suggested_specialist="hermes", routing_confidence=0.9),
        workspace="mgmt",
        evidence=[],
        router=None,
        authorization_id="mgmt:exec",
        toolbox_factory=lambda: object(),
        active_routes_only=True,
        external_v3_factory=factory,
        record_call_row=False,
        specialist_model="claude-opus-5-5",
    )
    assert outcome.ok
    assert made["model"] == "claude-opus-5-5"


def test_each_bot_passes_its_tier():
    """운영 배선: TYBot 훅은 Sonnet, VIPBot 훅은 Opus, VIPBot 판단은 Sonnet."""
    pilot = (ROOT / "slack/pilot.py").read_text(encoding="utf-8")
    assert "specialist_model=TYBOT_SPECIALIST_MODEL" in pilot
    assert "specialist_model=VIP_SPECIALIST_MODEL" in pilot
    assert "model=model,\n        )" in pilot  # 외부 v3 팩토리가 모델을 어댑터로 넘긴다
    vip = (ROOT / "vip_workspace.py").read_text(encoding="utf-8")
    assert "planner_model=VIP_PLANNER_MODEL" in vip
