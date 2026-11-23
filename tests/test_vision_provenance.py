"""Vision 전사 대상 판정(`vision_provenance`) — 오너 결정 2026-11-19.

실시간 수집 플래그(`human_upload_confirmed`)가 없어도 수집 입구를 지난 정본이면 대상이다.
우리 봇 출력 표식(답변 Canvas 제목)·좌표 결손은 대상이 아니다.
"""
from __future__ import annotations

import pytest

from tybot.archive.attachment_doc import CONVERTED, AttachmentDoc
from tybot.archive.vision_provenance import admitted_by_collection, admitted_file_ids


def _doc(**over) -> AttachmentDoc:
    base = dict(
        workspace="tyit", channel_id="C0TEST0001", channel="#팀_전산(ABB100)_공지",
        file_id="F0TEST0001", name="현장사진.png", revision="r1", visibility="private",
        acl=frozenset({"#팀_전산(ABB100)_공지"}), filetype="png", sha256="a" * 64,
        conversion_state=CONVERTED, human_upload_confirmed=False,
    )
    base.update(over)
    return AttachmentDoc(**base)


def test_an_upload_without_the_live_flag_is_admitted():
    assert admitted_by_collection(_doc()) is True
    assert admitted_by_collection(_doc(human_upload_confirmed=True)) is True


@pytest.mark.parametrize("over", [
    {"name": "현장 정리 · TYBot"},
    {"name": "자금 현황 · VIPBot"},
    {"name": "TYBot 정식 답변"},
    {"workspace": ""},
    {"channel_id": ""},
    {"file_id": ""},
])
def test_bot_output_marks_and_missing_coordinates_are_refused(over):
    assert admitted_by_collection(_doc(**over)) is False


def test_none_is_refused_and_ids_follow_the_same_rule():
    assert admitted_by_collection(None) is False
    docs = [_doc(file_id="F1"), _doc(file_id="F2", name="정리 · TYBot")]
    assert admitted_file_ids(docs) == frozenset({"F1"})


def test_the_console_can_approve_an_old_photo_without_the_live_flag():
    """옛 사진·파일 목록 소급 사진도 운영자가 외부 전송을 승인할 수 있다."""
    from test_image_description_approval_admin import NOW, Store, _request
    from test_image_description_approval_admin import _doc as approval_doc

    from tybot.console.image_description_approval_admin import create_approval

    store = Store()
    create_approval(_request(), now=NOW, docs=[approval_doc(human_upload_confirmed=False)],
                    store=store)
    assert len(store.saved) == 1
