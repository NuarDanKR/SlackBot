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
    assert parse("#프로젝트-스마트_PJ1-킥오프").kind == "project"
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
