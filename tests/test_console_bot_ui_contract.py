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
    render = app_source[app_source.index("path.startsWith('/manage/bots')"):]
    render = render[:render.index("      )}")]

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
    assert "runtime={<SpecialistManagement" in app_source


# --- 화면이 런타임을 단정하지 않는다 -------------------------------------------
#
# 2026-09-29 오너 지시 3번. 저장은 기록이고 적용이 아니다. 성공처럼 보이는 표시를
# 하면 연결을 끈 사람은 수집이 멈춘 줄 알고 자리를 뜬다.

BOTS = WEB / "pages" / "Bots.tsx"

#: 화면이 쓰면 안 되는 말. 「지금 그렇게 돌고 있다」 로 읽힌다.
FORBIDDEN_CLAIMS = (
    "적용되었습니다", "적용됐습니다", "반영되었습니다", "반영됐습니다",
    "수집이 시작됩니다", "수집이 시작되었습니다", "수집을 시작합니다",
    "연결되었습니다", "활성화되었습니다", "이제 동작합니다",
)


@pytest.fixture(scope="module")
def bots_page() -> str:
    return BOTS.read_text(encoding="utf-8")


def test_the_page_shows_the_runtime_notice(bots_page):
    """버튼 옆에 작게 적으면 사람은 버튼만 본다. 화면 위쪽에 둔다."""
    assert "export function RuntimeNotice" in bots_page
    assert bots_page.count("<RuntimeNotice effect=") >= 2, "목록·연결 양쪽에 있어야 한다"
    assert "appliesNow" in bots_page


def test_no_screen_text_claims_the_runtime_changed(bots_page):
    for claim in FORBIDDEN_CLAIMS:
        assert claim not in bots_page, f"런타임 동작을 단정한다: {claim}"


def test_saving_says_it_is_not_applied_yet(bots_page):
    """저장 성공을 「반영됐다」 로 말하지 않는다."""
    assert "아직 적용되지 않았습니다" in bots_page
    assert "수집이 시작되지는 않습니다" in bots_page
    assert "돌고 있는 프로세스는 그대로입니다" in bots_page


def test_the_token_inputs_are_passwords_and_cleared(bots_page):
    """저장 뒤 화면에 평문이 남아 있을 이유가 없다."""
    assert bots_page.count('type="password"') >= 2
    assert "setDraft({ bot: '', botToken: '', appToken: '', reason: '' })" in bots_page


def test_the_page_never_renders_a_plaintext_token_field(bots_page):
    """서버는 mask 만 준다. 화면이 평문 칸을 읽으려 하면 계약이 어긋난 것이다."""
    for field in ("botToken}", "appToken}", ".botToken ", ".appToken "):
        assert f"slack?.{field}" not in bots_page
    assert "botTokenMask" in bots_page and "appTokenMask" in bots_page


def test_internal_calls_have_no_token_input(bots_page):
    """내부 호출에는 Slack 토큰이 없다. 입력란을 두면 없는 것을 넣으라고 말하는 셈이다."""
    # 설명 문구에도 같은 말이 나오므로 **마크업 자리**를 집는다.
    marker = '<span className="bots-binding-kind">Master 내부 호출</span>'
    internal = bots_page[bots_page.index(marker):]
    internal = internal[:internal.index("</article>")]

    assert "type=\"password\"" not in internal
    assert "내부 호출에는 Slack 토큰이 없습니다" in internal


def test_the_mode_words_come_from_one_place(bots_page):
    """화면마다 문구를 새로 쓰면 같은 상태가 다르게 불린다."""
    assert "from '../botModes'" in bots_page
    assert "meaningOf" in bots_page and "tooltipOf" in bots_page
    # 주 화면 칩에는 뜻만, 키는 title(툴팁)에만.
    assert "title={tooltipOf(meaning)}" in bots_page


def test_the_bot_tabs_are_registered_paths():
    """새로고침에서 404 가 뜨면 사람은 콘솔이 고장난 줄 안다."""
    app = APP.read_text(encoding="utf-8")

    for path in ("/manage/bots/connections", "/manage/bots/runtime"):
        assert path in app
    assert "BOT_TABS" in app


def test_the_tabs_do_not_widen_permission():
    """탭을 메뉴에 넣지 않으므로 권한은 부모 항목이 정한다."""
    app = APP.read_text(encoding="utf-8")

    assert "const permissionPath = path.startsWith('/manage/bots')" in app
    assert "allowed.has(permissionPath)" in app


# --- 기존 화면 회귀 -------------------------------------------------------------

def test_the_license_menu_is_untouched():
    """PR #1 의 메뉴는 이 작업과 무관하게 그대로여야 한다."""
    app = APP.read_text(encoding="utf-8")

    assert "{ path: '/manage/licenses', label: '라이선스 현황', minimum: 'admin' }" in app
    assert "{path === '/manage/licenses' && <Licenses onToast={toast} />}" in app


def test_the_old_specialist_bookmark_still_lands_on_the_bot_page():
    """옛 북마크는 새 화면으로 간다. 선택 쿼리는 그대로 남는다."""
    app = APP.read_text(encoding="utf-8")
    alias = app[app.index("const path = location.path"):]
    alias = alias[:alias.index("const [authTick")]

    assert "'/manage/specialists'" in alias and "'/manage/bots'" in alias
    assert "path.startsWith('/manage/bots')" in app, "그 경로를 그리는 자리가 있어야 한다"


# --- 라우팅 · Manifest · 변경 이력 탭 -------------------------------------------

ROUTING = WEB / "pages" / "BotsRouting.tsx"


@pytest.fixture(scope="module")
def routing_page() -> str:
    return ROUTING.read_text(encoding="utf-8")


def test_the_routing_screen_does_not_claim_a_switch(routing_page):
    """Master 는 아직 배정 표만 보고 라우팅한다. 「전환했다」 로 말하면 거짓이다."""
    for claim in FORBIDDEN_CLAIMS:
        assert claim not in routing_page, claim
    assert "지금 답변 경로는 바뀌지 않습니다" in routing_page
    assert "<RuntimeNotice effect=" in routing_page


def test_route_buttons_use_the_shared_words(routing_page):
    """버튼에 `active` 라고 적으면 무엇이 달라지는지 말하지 않는 셈이다."""
    assert "from '../botModes'" in routing_page
    assert "meaningOf(ROUTE_MODES, mode).label" in routing_page
    assert "title={tooltipOf(meaningOf(ROUTE_MODES, mode))}" in routing_page


def test_the_routing_screen_requires_a_reason(routing_page):
    """사유가 없으면 사고가 났을 때 범위를 정할 수 없다."""
    assert "if (!reason.trim())" in routing_page


def test_the_manifest_screen_separates_identity_from_manifest(routing_page):
    """`auth.test` 성공만으로 Manifest 일치를 표시하지 않는다(§6.2)."""
    assert "신원 확인과는 다른 검사입니다" in routing_page
    assert "스코프가" in routing_page
    assert "Slack 앱 설정이 바뀌지는 않습니다" in routing_page


def test_the_manifest_screen_says_what_is_missing(routing_page):
    """없는 것을 빈칸으로 두면 「대조했는데 비었다」 와 구분되지 않는다."""
    assert "파일 없음" in routing_page
    assert "아직 대조 대상이 아님" in routing_page


def test_the_audit_screen_shows_who_and_why(routing_page):
    assert "바꾼 사람" in routing_page and "사유" in routing_page


def test_all_bot_tabs_are_registered():
    app = APP.read_text(encoding="utf-8")

    for path in (
        "/manage/bots/connections", "/manage/bots/routing",
        "/manage/bots/manifests", "/manage/bots/audit", "/manage/bots/runtime",
    ):
        assert path in app
    assert "BOT_TAB_OF" in app


def test_the_tabs_keep_the_chosen_workspace(bots_page):
    """탭을 옮길 때마다 고른 워크스페이스가 사라지면 매번 다시 고르게 된다."""
    for tab in ("routing", "manifests", "audit"):
        assert f"#/manage/bots/{tab}${{workspace ? `?workspace=${{workspace}}` : ''}}" in bots_page
