"""상태 파일 계약 검사 — 화면이 막는 것과 도구가 말하는 것은 다른 일이다.

`/pf/` 화면은 계약 밖의 값을 **읽지 않는다**(allowlist). 그래서 상태 파일에 토큰이
들어와도 화면은 조용하다 — 조용한 것이 목적이었으니 맞다. 그런데 그러면 **계약이
깨진 것을 아무도 모른다.**

화면은 새는 것을 막고, `scripts/check_pf_health.py` 는 깨진 것을 말한다.
이 시험은 그 분업이 실제로 성립하는지 본다.

## 픽스처를 조립해서 쓰는 이유
시크릿 모양을 시험하려면 시크릿처럼 생긴 값이 필요한데, 그걸 그대로 적으면 커밋
가드(`.githooks`)가 파일을 막는다. **가드가 맞다** — 저장소에 시크릿 모양이 박히면
나중에 진짜인지 가짜인지 아무도 구분하지 못한다. 그래서 조각으로 나눠 붙인다.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

from tybot_pf import health


def _checker():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_pf_health.py"
    spec = importlib.util.spec_from_file_location("check_pf_health", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _checker()
NOW = dt.datetime.now(dt.UTC)

# 가짜 시크릿. 조각으로 나눠 붙여 커밋 가드에 걸리지 않게 한다(파일 머리말 참조).
#
# **숫자를 길게 넣지 않는다.** 처음에는 숫자열로 채웠는데, 그것이 주민번호
# 형식(여섯 자리-일곱 자리)과 우연히 겹쳐 커밋 가드가 매번 경고했다. 오탐이지만
# 같은 경고가 되풀이되면 사람은 경고 자체를 안 보게 되고, 그러면 진짜가 섞여
# 들어올 때 못 잡는다. 탐지에 필요한 것은 접두사뿐이므로 뒤는 글자로 채운다.
FAKE_BOT_TOKEN = "xox" + "b-" + "T" * 10 + "-" + "K" * 12 + "-" + "a" * 24
FAKE_APP_TOKEN = "xap" + "p-" + "A" * 4 + "-" + "B" * 8 + "-" + "c" * 32
FAKE_MODEL_KEY = "sk-" + "ant-api" + "-" + "z" * 40
FAKE_PRIVATE_KEY = "-----" + "BEGIN OPENSSH PRIVATE" + " KEY-----"


def _state(**over) -> dict:
    data = {
        "source_commit": "a1b2c3d",
        "started_at": NOW.isoformat(),
        "generated_at": NOW.isoformat(),
        "last_ingest": NOW.isoformat(),
        "unpushed_commits": 0,
        "calls_today": 5,
        "errors": [],
    }
    data.update(over)
    return data


def check(data: dict, *, stale_after: int = 900):
    return checker.inspect(data, stale_after=stale_after, now=NOW)


# --- 계약을 지킨 파일 ----------------------------------------------------------
def test_a_contract_abiding_file_passes_clean():
    bad, warn = check(_state())
    assert bad == []
    assert warn == []


# --- 계약 항목 ----------------------------------------------------------------
def test_missing_contract_fields_are_reported():
    """없으면 화면이 「정상」 이라고 칠할 근거가 없다."""
    bad, _ = check({"calls_today": 3})
    assert any("계약 항목이 없습니다" in item for item in bad)


# --- 시크릿 --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "label"),
    [
        (FAKE_BOT_TOKEN, "Slack 토큰"),
        (FAKE_APP_TOKEN, "Slack 앱 토큰"),
        (FAKE_MODEL_KEY, "Anthropic 키"),
        (FAKE_PRIVATE_KEY, "개인 키"),
    ],
)
def test_a_secret_shaped_value_is_caught_wherever_it_sits(value, label):
    """칸 이름이 무해해도 값의 모양은 못 숨긴다."""
    bad, _ = check(_state(last_digest=value))
    assert any(label in item for item in bad)


def test_the_tool_never_prints_the_secret_itself():
    """이 도구의 출력이 새 유출 경로가 되면 안 된다."""
    bad, warn = check(_state(slack_bot_token=FAKE_BOT_TOKEN))

    assert bad, "시크릿을 잡지 못했습니다."
    for line in [*bad, *warn]:
        assert FAKE_BOT_TOKEN not in line
        assert FAKE_BOT_TOKEN[:12] not in line


def test_a_secret_nested_in_a_list_is_still_caught():
    bad, _ = check(_state(errors=[{"code": "slack", "at": FAKE_MODEL_KEY}]))
    assert any("Anthropic 키" in item for item in bad)


# --- 본문 ----------------------------------------------------------------------
def test_a_long_string_reads_as_leaked_prose():
    """질문·답변·문서 본문은 상태 파일에 담지 않는다(§7.4)."""
    bad, _ = check(_state(archive_conflict="가" * 300))
    assert any("담지 않습니다" in item for item in bad)


def test_a_short_value_in_the_same_field_is_fine():
    """길이로 판정하므로 정상 값이 걸리면 도구가 못 쓰게 된다."""
    bad, _ = check(_state(archive_conflict="refs/heads/main"))
    assert bad == []


# --- 계약에 없는 칸 -------------------------------------------------------------
def test_an_unknown_field_is_something_to_look_at_not_a_blocker():
    """프금팀이 칸을 더 쓰는 것 자체는 위반이 아니다 — 화면이 안 읽으면 그만이다.

    다만 **모르고 지나가면 안 된다.** 다음에 계약을 넓힐 때 후보가 된다.
    """
    bad, warn = check(_state(node_version="20.11.1"))
    assert bad == []
    assert any("node_version" in item for item in warn)


# --- 오류 코드 ------------------------------------------------------------------
def test_an_unnormalised_error_code_is_a_violation():
    """예외 메시지를 코드 자리에 넣으면 경로·식별자가 그대로 따라 들어온다."""
    bad, _ = check(_state(errors=[{"code": "Error: /var/lib/x 를 열지 못함", "at": ""}]))
    assert any("정규화되지 않았습니다" in item for item in bad)


def test_every_documented_error_code_is_accepted():
    """인계서 §8 이 요구한 일곱 가지가 전부 통과해야 한다."""
    for code in health.ERROR_CODES:
        bad, _ = check(_state(errors=[{"code": code, "at": NOW.isoformat()}]))
        assert bad == [], f"{code} 가 거절됐습니다: {bad}"


def test_a_message_field_on_an_error_is_flagged():
    bad, warn = check(_state(errors=[{"code": "slack", "at": "", "message": "무엇이든"}]))
    assert bad == []
    assert any("계약 밖 칸: message" in item for item in warn)


# --- 시각 ----------------------------------------------------------------------
def test_a_stale_file_is_something_to_look_at():
    old = (NOW - dt.timedelta(hours=4)).isoformat()
    bad, warn = check(_state(generated_at=old))
    assert bad == []
    assert any("마지막 기록이" in item for item in warn)


def test_a_future_timestamp_says_the_clock_is_off():
    """앞선 시각은 「최신」 으로 보이지만 사실은 시계가 어긋난 것이다."""
    ahead = (NOW + dt.timedelta(hours=2)).isoformat()
    _, warn = check(_state(generated_at=ahead))
    assert any("시계가 어긋났습니다" in item for item in warn)


def test_a_timestamp_without_a_timezone_is_called_out():
    naive = NOW.replace(tzinfo=None).isoformat()
    _, warn = check(_state(generated_at=naive))
    assert any("시간대가 없습니다" in item for item in warn)


def test_an_unparsable_timestamp_is_a_violation():
    bad, _ = check(_state(generated_at="어제쯤"))
    assert any("ISO 시각으로 읽지 못했습니다" in item for item in bad)


# --- 화면과의 분업 --------------------------------------------------------------
def test_the_screen_stays_clean_while_the_tool_reports_the_breach(tmp_path):
    """이것이 이 도구의 존재 이유다.

    화면은 계약 밖 값을 안 읽으므로 조용하다 — 그래서 조용한 것만 보면 계약이 깨진
    줄 모른다. 도구가 말해 준다.
    """
    leaked = _state(slack_bot_token=FAKE_BOT_TOKEN)
    (tmp_path / health.STATE_FILENAME).write_text(
        json.dumps(leaked, ensure_ascii=False), encoding="utf-8"
    )

    shown = health.read("pf-hermes", tmp_path, now=NOW)
    assert shown.status == health.OK
    assert "slack_bot_token" not in json.dumps(shown.to_json(), ensure_ascii=False)

    bad, _ = check(leaked)
    assert bad, "화면은 막았는데 도구도 조용하면 아무도 모른다."
