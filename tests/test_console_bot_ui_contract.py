"""봇 관리 화면의 **말과 경계** — 네 가지를 고정한다.

결정: 2026-09-29 오너 지시.

1. `전문 봇 관리` 메뉴는 없앤다. 두 메뉴를 같이 보이면 어느 쪽이 정본인지 모른다
2. `/manage/specialists` 는 `/manage/bots` 로 보내되 **URL 의 선택은 지킨다**
3. 수집 모드는 **뜻을 먼저** 보인다. `off`·`shadow` 같은 내부 키를 주 화면에
   나란히 붙이면 결국 사람이 키를 읽고 뜻은 장식이 된다
4. 내부 키는 상세·툴팁에서 **확인할 수 있어야** 한다. 로그·감사와 대조할 때 필요하다

프런트엔드 시험 도구가 없으므로 소스를 읽어 고정한다(`test_pf_isolation` 과 같은
방법). 「문서에 적었다」 는 리팩터링을 견디지 못한다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "console-web" / "src"
MODES = WEB / "botModes.ts"
APP = WEB / "App.tsx"

#: 주 화면에 **맨 값으로** 나오면 안 되는 내부 키.
INTERNAL_KEYS = ("off", "shadow", "active", "paused", "draft", "disabled", "retired")


@pytest.fixture(scope="module")
def modes() -> str:
    return MODES.read_text(encoding="utf-8")


def _table(source: str, name: str) -> dict[str, dict[str, str]]:
    """`export const NAME: Record<...> = { ... }` 안의 항목별 `label`·`key`."""
    start = source.find(f"export const {name}")
    if start < 0:
        return {}
    body = source[start:source.index("\n}\n", start)]
    out: dict[str, dict[str, str]] = {}
    for entry in re.finditer(
        r"^  (\w+): \{(.*?)^  \},", body + "\n  },", re.S | re.M,
    ):
        fields = {}
        for field in ("label", "detail", "key", "tone"):
            found = re.search(rf"{field}:\s*(?:'([^']*)'|\"([^\"]*)\")", entry.group(2))
            if found:
                fields[field] = found.group(1) or found.group(2) or ""
        out[entry.group(1)] = fields
    return out


# --- 3. 뜻을 먼저 -------------------------------------------------------------

def test_every_channel_mode_says_what_changes(modes):
    """`off` 를 본 사람은 「수집이 꺼졌다」 로 읽지만, 원문은 계속 쌓인다."""
    table = _table(modes, "CHANNEL_MODES")

    assert set(table) == {"off", "shadow", "active", "paused"}
    assert table["off"]["label"] == "Archiver 미적용 · Master 유지"
    assert table["shadow"]["label"] == "그림자 대조 · 운영본은 Master"
    assert table["active"]["label"] == "Archiver 운영 수집"
    assert table["paused"]["label"] == "일시 중지 · 양쪽 쓰기 중단"


@pytest.mark.parametrize("name", ["CHANNEL_MODES", "CONNECTION_STATES", "ROUTE_MODES"])
def test_no_label_is_a_bare_internal_key(modes, name):
    """키를 주 화면에 나란히 붙이면 사람이 키를 읽고 뜻은 장식이 된다."""
    for state, fields in _table(modes, name).items():
        label = fields.get("label", "")
        assert label, f"{name}.{state} 에 label 이 없다"
        assert label != state
        assert not any(
            re.search(rf"(^|[\s(]){key}([\s)]|$)", label) for key in INTERNAL_KEYS
        ), f"{name}.{state} 의 label 에 내부 키가 그대로 있다: {label}"


@pytest.mark.parametrize("name", ["CHANNEL_MODES", "CONNECTION_STATES", "ROUTE_MODES"])
def test_every_state_explains_the_consequence(modes, name):
    """상태 이름만으로는 무엇이 달라지는지 알 수 없다."""
    for state, fields in _table(modes, name).items():
        assert len(fields.get("detail", "")) >= 15, f"{name}.{state} 의 설명이 너무 짧다"


# --- 4. 키는 상세에서 ---------------------------------------------------------

def test_the_internal_key_is_available_for_the_tooltip(modes):
    """로그·감사에는 키가 찍힌다. 화면에서 확인할 길이 없으면 대조가 안 된다."""
    for name in ("CHANNEL_MODES", "CONNECTION_STATES", "ROUTE_MODES"):
        for state, fields in _table(modes, name).items():
            assert fields.get("key") == state, f"{name}.{state} 의 key 가 다르다"

    assert "내부 값:" in modes, "툴팁이 키를 보여 주는 자리가 없다"
    assert "export function tooltipOf" in modes


def test_an_unknown_value_is_not_silently_blank(modes):
    """조용히 빈칸이 되면 상태가 **없는 것처럼** 보인다."""
    assert "알 수 없는 상태" in modes
    assert "export function meaningOf" in modes


# --- 1·2. 메뉴와 리다이렉트 ----------------------------------------------------

@pytest.fixture(scope="module")
def app_source() -> str:
    return APP.read_text(encoding="utf-8")


def _nav_paths(source: str) -> list[str]:
    block = source[source.index("const NAV"):source.index("const ALL_PATHS")]
    return re.findall(r"path: '([^']+)', label:", block)


def test_the_old_menu_item_is_gone(app_source):
    """두 메뉴가 같이 보이면 어느 쪽이 정본인지 모른다. 그때 사람은 둘 다 연다."""
    paths = _nav_paths(app_source)

    assert "/manage/bots" in paths
    assert "/manage/specialists" not in paths
    assert "전문 봇 관리" not in app_source


def test_the_new_menu_keeps_the_same_permission(app_source):
    """이름만 바꾼다. 권한이 넓어지면 개편이 **권한 변경**이 된다."""
    block = app_source[app_source.index("const NAV"):app_source.index("const ALL_PATHS")]
    (entry,) = re.findall(r"\{ path: '/manage/bots'[^}]*\}", block)

    assert "minimum: 'developer'" in entry
    assert "capability: 'specialists'" in entry


def test_the_old_url_still_lands_somewhere(app_source):
    """옛 북마크가 「없는 화면」 으로 떨어지면 사람은 콘솔이 고장난 줄 안다."""
    assert "'/manage/specialists'" in app_source
    alias = app_source[app_source.index("const path = location.path"):]
    alias = alias[:alias.index("const [authTick")]

    assert "/manage/specialists" in alias and "'/manage/bots'" in alias


def test_the_old_url_is_a_known_path(app_source):
    """모르는 경로면 별칭이 걸리기 전에 404 화면이 먼저 뜬다."""
    registry = app_source[app_source.index("const ALL_PATHS"):]
    registry = registry[:registry.index("\n")]

    assert "'/manage/specialists'" in registry


def test_the_redirect_does_not_touch_the_query(app_source):
    """선택한 봇·워크스페이스는 URL 에 있다. 경로만 바꾸고 쿼리는 그대로 넘긴다."""
    render = app_source[app_source.index("{path === '/manage/bots'"):]
    render = render[:render.index("}\n")]

    assert "query={location.query}" in render
    alias = app_source[app_source.index("const path = location.path"):]
    alias = alias[:alias.index("const [authTick")]
    # 주석은 뺀다. 「쿼리는 그대로 간다」 고 적어 둔 설명이 코드로 세어지면 안 된다.
    code = " ".join(
        line for line in alias.splitlines() if not line.strip().startswith("//")
    )

    assert "location.query" not in code, "별칭이 쿼리를 건드리면 선택이 사라진다"


def test_the_runtime_screen_is_reused_not_rebuilt(app_source):
    """소스·승인·health 기능을 다시 만들지 않는다(§12.1)."""
    assert "{path === '/manage/bots' && <SpecialistManagement" in app_source
