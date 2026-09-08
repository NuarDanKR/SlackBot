"""채널 명명 규칙 — 수집 대상 판정과 조직 코드 추출."""
from __future__ import annotations

import pytest

from tybot.channels import parse, should_collect


@pytest.mark.parametrize(
    "name,prefix,org,code,task",
    [
        ("#팀-전산_ABB110-주간회의", "팀", "전산", "ABB110", "주간회의"),
        ("#본부-건축_AB-경영회의", "본부", "건축", "AB", "경영회의"),
        ("#실-안전_SF100-점검", "실", "안전", "SF100", "점검"),
        ("#현장-김해외동_180182-채팅방", "현장", "김해외동", "180182", "채팅방"),
        ("#프로젝트-스마트팩토리_PJ2026-킥오프", "프로젝트", "스마트팩토리", "PJ2026", "킥오프"),
        # 업무명이 없어도 조직까지는 식별된다
        ("#팀-전산_ABB110", "팀", "전산", "ABB110", ""),
        ("#팀-현장관리_ABB540-주간-보고", "팀", "현장관리", "ABB540", "주간-보고"),
    ],
)
def test_new_format(name, prefix, org, code, task):
    s = parse(name)
    assert s is not None
    assert (s.prefix, s.org_name, s.org_code, s.task) == (prefix, org, code, task)


@pytest.mark.parametrize(
    "name",
    [
        "#팀_자금(ABB540)_주간보고",
        "#현장_김해외동(180182)_채팅방",
        "#프로젝트-업데이트",
    ],
)
def test_old_format_is_retired(name):
    """구형식은 폐기됐다. 그런 이름의 채널은 더 이상 수집 대상이 아니다.

    이미 수집된 문서는 남지만 새 수집은 멈춘다 - 이름을 신형식으로 바꾸면
    channel_rename 이벤트로 그 순간부터 다시 수집된다.
    """
    assert parse(name) is None
    assert not should_collect(name)


@pytest.mark.parametrize(
    "name",
    [
        "#일반",
        "#random",
        "#점심메뉴",
        "#전사_공지",          # 두문자가 수집 대상 목록에 없다
        "#영업-외부고객_ABC-협의",  # '영업' 은 목록에 없다
        "",
        "#",
    ],
)
def test_non_matching_channels_are_not_collected(name):
    assert not should_collect(name)
    assert parse(name) is None


def test_kind_maps_to_org_tree():
    assert parse("#본부-건축_AB-회의").kind == "hq"
    assert parse("#팀-전산_ABB110-회의").kind == "team"
    assert parse("#현장-김해외동_180182-채팅").kind == "site"
    # 프로젝트는 업무로 이름이 바뀌었지만 옛 채널도 같은 종류로 읽힌다.
    assert parse("#프로젝트-스마트_PJ1-킥오프").kind == "task"
    assert parse("#업무-스마트_ABB110-킥오프").kind == "task"
    assert parse("#실-안전_SF1-점검").kind == "div"


def test_hash_prefix_optional():
    with_hash = parse("#팀-전산_ABB110-주간회의")
    without = parse("팀-전산_ABB110-주간회의")
    assert without is not None
    assert without.raw == with_hash.raw == "#팀-전산_ABB110-주간회의"


def test_code_is_mandatory():
    """조직코드가 없으면 조직 매핑을 못 하므로 규칙 위반으로 본다."""
    assert parse("#팀-자금-주간보고") is None
    assert parse("#팀_자금_주간보고") is None


def test_explain_gives_actionable_message():
    from tybot.channels import explain

    assert "수집 대상 아님" in explain("#점심메뉴")
    assert "#팀-전산_ABB110-주간회의" in explain("#점심메뉴")
    assert "수집 대상" in explain("#팀-전산_ABB110-주간회의")


# --- 두문자 뒤 밑줄도 인식한다 (2026-09-08) ----------------------------------
#
# 사내에 `#팀_전산_ABB110-주간회의` 처럼 밑줄로 만든 채널이 많았다. 그 채널들은
# 규칙에 안 맞아 **아무 오류 없이 수집에서 빠져 있었다** — 사람은 봇을 초대했고
# 이름도 규칙대로 적었다고 믿는데 기록이 한 줄도 안 쌓인다.
@pytest.mark.parametrize(
    "name,prefix,org,code,task",
    [
        ("#팀_전산_ABB110-주간회의", "팀", "전산", "ABB110", "주간회의"),
        ("#본사팀_전산_ABB155-slack_ai프로젝트", "본사팀", "전산", "ABB155", "slack_ai프로젝트"),
        ("#현장_김해외동_180182-채팅방", "현장", "김해외동", "180182", "채팅방"),
        ("#업무_전산_ABB110-협업", "업무", "전산", "ABB110", "협업"),
        ("#팀_전산_ABB110", "팀", "전산", "ABB110", ""),
    ],
)
def test_underscore_after_the_prefix_is_also_recognised(name, prefix, org, code, task):
    s = parse(name)

    assert s is not None, f"밑줄 형식이 수집에서 빠진다: {name}"
    assert (s.prefix, s.org_name, s.org_code, s.task) == (prefix, org, code, task)


def test_the_two_separators_give_the_same_result():
    """구분자만 다른 두 이름은 **같은 조직·같은 업무**로 읽혀야 한다.

    갈리면 같은 팀 채널이 조직 트리에서 둘로 나뉜다.
    """
    dash = parse("#팀-전산_ABB110-주간회의")
    under = parse("#팀_전산_ABB110-주간회의")

    assert (dash.prefix, dash.org_name, dash.org_code, dash.task) == (
        under.prefix, under.org_name, under.org_code, under.task
    )
    assert dash.kind == under.kind


def test_the_long_prefix_still_wins_with_an_underscore():
    """`본사팀_…` 이 `팀` 으로 잘리면 조직명이 `사팀…` 이 된다."""
    s = parse("#본사팀_전산_ABB110-주간회의")

    assert s.prefix == "본사팀"
    assert s.org_name == "전산"


def test_we_still_create_only_one_shape():
    """인식은 넓히고 **생성은 좁게** 둔다.

    두 형식을 다 만들게 하면 같은 팀 채널이 두 모양으로 쌓이고, 사람이 어느
    것이 맞는지 묻기 시작한다.
    """
    from tybot.channel_management import build_channel_name

    name = build_channel_name("본사팀", "전산", "ABB110", "주간회의")

    assert name == "팀-전산_ABB110-주간회의", name


def test_the_retired_paren_format_is_still_rejected():
    """`_` 를 허용해도 구 형식이 되살아나면 안 된다 — 괄호는 조직코드가 아니다."""
    assert parse("#팀_자금(ABB540)_주간보고") is None
