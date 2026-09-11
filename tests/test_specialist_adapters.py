"""프롬프트 계약 방식 전문가 (B-38).

설계: `docs/design/specialist-deployment.md`

전문가는 **프롬프트 파일 + 지정 모델**이다. 코드를 받지 않으므로
그 팀이 배포 중이어도 우리 봇은 답하고, 근거가 우리 밖으로 나가지 않는다.
"""
from __future__ import annotations

import pytest

from tybot import specialist_adapters as sa
from tybot.gateway.base import LLMResponse, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.specialist_contract import AuthorizedEvidence, SpecialistRequest


class Fake:
    name = "anthropic"

    def __init__(self, text: str = "8월 20일 3억으로 정정되었습니다.") -> None:
        self.text = text
        self.calls: list[tuple] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls.append((spec.model, list(messages)))
        return LLMResponse(self.text, spec.model, self.name, 100, 20, 0.001)


def _router(provider):
    return Router(
        providers={"anthropic": provider},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            ),
            "claude-haiku-4-5": ModelSpec(
                "claude-haiku-4-5", "anthropic", 1.0, 5.0, Sensitivity.CONFIDENTIAL
            ),
        },
        cost_guard=CostGuard(10.0),
        default_model="claude-sonnet-5",
    )


def _request(question: str = "기성금 얼마야", text: str = "기성금 3억 청구") -> SpecialistRequest:
    return SpecialistRequest(
        question=question,
        evidence=(
            AuthorizedEvidence.from_acl_filter(
                workspace="pilot", text=text, authorization_id="auth-1"
            ),
        ),
    )


# --- 배포 여부는 파일이 사실이다 --------------------------------------------
def test_hermes_prompt_is_deployed():
    """프롬프트가 있으면 배포된 것이다. 목록을 손으로 적지 않는다."""
    assert "hermes" in sa.available_keys()


def test_a_missing_prompt_is_refused_not_silently_empty():
    """빈 프롬프트로 돌면 전문가가 아니라 그냥 모델이 답하는데,

    기록에는 전문가가 답한 것으로 남는다. 그러면 판정을 보는 일이 무의미해진다.
    """
    with pytest.raises(sa.AdapterError, match="없습니다"):
        sa.load_prompt("존재하지않는전문가")


def test_frontmatter_does_not_reach_the_model():
    """출처·버전 기록은 우리 것이다. 모델에 보내면 토큰만 쓴다."""
    prompt = sa.load_prompt("hermes")

    assert "derived_from" not in prompt
    assert prompt.startswith("당신은")


def test_the_prompt_version_is_readable():
    """콘솔에 「지금 무엇이 돌고 있나」 를 보이려면 버전이 있어야 한다.

    **값을 못 박지 않는다.** 프롬프트를 고칠 때마다 이 테스트가 깨지면,
    고치는 사람이 숫자만 맞추고 지나가게 된다 — 검사할 것은 「읽히는가」 다.
    """
    got = sa.prompt_version("hermes")

    assert got.isdigit() and int(got) >= 1, got


# --- 모델은 전문가별로 ------------------------------------------------------
def test_the_specialist_model_is_used(tmp_path):
    """간단한 분야에 무거운 모델을 쓸 이유가 없다.

    콘솔에서 전문가별로 모델을 정하는 것이 이 자리에서 값을 갖는다.
    """
    provider = Fake()
    adapter = sa.build("hermes", _router(provider), model="claude-haiku-4-5")

    adapter.complete(_request())

    assert provider.calls[0][0] == "claude-haiku-4-5"


def test_no_model_falls_back_to_the_gateway_default():
    provider = Fake()
    adapter = sa.build("hermes", _router(provider), model="")

    adapter.complete(_request())

    assert provider.calls[0][0] == "claude-sonnet-5"


# --- 프롬프트와 근거의 자리 --------------------------------------------------
def test_evidence_comes_before_the_question():
    """캐시는 접두사 일치다. 같은 채널을 다시 물을 때 근거가 캐시되어야 한다."""
    provider = Fake()
    sa.build("hermes", _router(provider)).complete(_request())

    _, messages = provider.calls[0]
    user = next(m for m in messages if m.role == "user")

    assert user.content.index("기성금 3억 청구") < user.content.index("질문:")


def test_the_prompt_goes_in_as_a_system_message():
    provider = Fake()
    sa.build("hermes", _router(provider)).complete(_request())

    _, messages = provider.calls[0]

    assert messages[0].role == "system"
    assert "출처를 쓰지 않습니다" in messages[0].content


def test_the_prompt_forbids_writing_sources():
    """출처는 마스터가 붙인다(원칙 2). 전문가가 쓰면 본문과 출처가 어긋난다.

    계약(`_validated_text`)이 실행 시점에 거부하지만, 프롬프트에서도 말해야
    거부가 잦아지지 않는다.
    """
    prompt = sa.load_prompt("hermes")

    assert "출처를 쓰지 않습니다" in prompt


def test_the_prompt_warns_about_other_sites():
    """사업장이 30개가 넘고 금액 단위가 비슷하다.

    다른 현장 숫자가 흘러드는 것이 이 영역에서 가장 흔한 사고다.
    """
    prompt = sa.load_prompt("hermes")

    assert "다른 사업장" in prompt or "다른 현장" in prompt


# --- 근거가 커도 답이 나간다 -------------------------------------------------
def test_oversized_evidence_is_trimmed_not_dropped(monkeypatch):
    """컨텍스트를 넘겨 호출이 통째로 실패하면 답이 아예 안 나간다."""
    monkeypatch.setattr(sa, "MAX_EVIDENCE_CHARS", 100)
    provider = Fake()

    sa.build("hermes", _router(provider)).complete(_request(text="가" * 500))

    _, messages = provider.calls[0]
    user = next(m for m in messages if m.role == "user")

    assert user.content.count("가") == 100


# --- 전문가는 자기 데이터를 가져오지 않는다 ---------------------------------
def test_the_adapter_has_no_way_to_read_the_archive():
    """프롬프트 방식의 값이 여기 있다 — 읽을 통로가 아예 없다.

    같은 프로세스 안의 임의 코드는 DATABASE_URL·아카이브 파일을 그대로 읽지만,
    이 어댑터가 받는 것은 게이트웨이와 프롬프트 문자열뿐이다.
    """
    adapter = sa.build("hermes", _router(Fake()))

    for attr in ("store", "_store", "archive", "conn", "_conn"):
        assert not hasattr(adapter, attr), f"어댑터가 {attr} 를 들고 있다"


# --- 계약이 사는 자리 (2026-09-11) -------------------------------------------
#
# `subbots/<key>/contract/prompt.md` 가 정식이다. 다른 팀에게서 **받은 계약**이고
# 콘솔이 버전·검사·승인을 관리한다 — 우리 코드 옆에 두면 "우리가 만든 프롬프트"
# 처럼 보인다.
def test_hermes_lives_in_subbots():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    assert (root / "subbots" / "hermes" / "contract" / "prompt.md").is_file()
    assert (root / "subbots" / "hermes" / "tybot-specialist.toml").is_file()


def test_a_key_never_lives_in_both_places():
    """두 곳에 같으면 **어느 쪽이 도는지 아무도 모른다.** 고친 쪽이 안 도는
    상태가 조용히 생긴다 — 이 구조의 유일한 위험이다."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    official = {
        p.parents[1].name for p in (root / "subbots").glob("*/contract/prompt.md")
    }
    legacy = {p.stem for p in (root / "src/tybot/specialist_prompts").glob("*.md")}

    assert not (official & legacy), f"두 곳에 있다: {sorted(official & legacy)}"


def test_the_official_path_wins():
    """옛 자리에 남은 파일이 이기면, 옮긴 뒤에도 옛 것이 돈다."""
    from pathlib import Path

    path = sa.contract_path("hermes")

    assert path is not None
    assert Path("subbots") in Path(path).parents or "subbots" in str(path)


def test_the_console_sees_hermes_as_deployed():
    """`available_keys()` 하나가 콘솔 「배포됨」 판정의 근거다. 계약을 옮기고
    이것을 안 고치면 화면이 「미배포」 로 뒤집힌다."""
    assert "hermes" in sa.available_keys()


def test_a_directory_without_a_contract_is_not_deployed():
    """디렉터리만 있고 계약이 없으면 「등록했는데 답을 못 한다」 가 된다."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for folder in (root / "subbots").iterdir():
        if not folder.is_dir():
            continue
        has_contract = (folder / "contract" / "prompt.md").is_file()
        assert (folder.name in sa.available_keys()) == has_contract, folder.name


@pytest.mark.parametrize("evil", ["../../etc/passwd", "a/b", "Hermes", "", "x" * 40])
def test_a_bad_key_never_becomes_a_path(evil):
    """DB 나 화면을 통해 온 값이라도 그대로 붙이면 그 자리가 곧 경로 탈출이다."""
    assert sa.contract_path(evil) is None
