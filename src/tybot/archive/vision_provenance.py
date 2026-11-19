"""Vision 전사 대상이 **우리 봇 출력이 아닌가** — 판정은 이 한 곳 (오너 결정 2026-11-19).

## 무엇을 바꿨나

전에는 이미지 설명(B-81)이 정본의 `human_upload_confirmed` 가 참일 때만 돌았다. 그 값은
실시간 수집이 Slack 이벤트에서 `user` 가 있고 `bot_id`·`app_id` 가 없는 것을 **그 자리에서
본** 경우에만 참이라, 2026-10-27 전에 들어온 사진·파일 목록 소급(`channel_files`)·히스토리
재수집으로 들어온 사진은 사람이 올렸어도 전부 거짓이다. 그 사진들이 텍스트가 되지 못하면
「가능한 모든 문서의 이미지를 정확한 텍스트로」 라는 목표의 대부분이 빠진다.

PDF 쪽 전사(B-80)는 이미 같은 이유로 그 값을 요구하지 않는다(`pdf_vision_guide`).
이미지도 같은 기준으로 맞춘다.

## 그래도 원칙 1 은 지킨다 — 막는 자리는 수집 입구다

원칙 1 이 막으려는 것은 **우리 봇이 만든 것이 우리 근거로 되먹이는 고리**다. 그 고리는
정본이 생기기 전에 끊긴다.

| 입구 | 봇 출력 처리 |
|---|---|
| 실시간·히스토리 수집(`collect`) | `bot_id`·`app_id`·subtype 메시지를 버린다. 지정 변환 앱의 **PDF** 만 예외 |
| DM 수집(`dm_inbox`) | `bot_id` 메시지를 버린다 |
| 파일 복구(`file_recovery`) | `bot_id`·제외 사용자를 버린다 |
| 파일 목록 소급(`channel_files`) | TYBot·VIPBot 답변 Canvas 를 버린다. 우리 봇은 이미지·PDF 를 올리지 않는다 |

그래서 **정본에 있다 = 수집 입구를 지났다** 이고, 여기서는 그 위에 한 겹만 더 본다:
좌표가 온전한가, 우리 봇이 만든 답변 Canvas 표식이 없는가. 우리 봇이 이미지·PDF 를
올리는 기능이 생기면 **그 입구에서** 표식을 남기고 이 함수가 그것을 거절해야 한다.

`human_upload_confirmed` 값은 계속 기록한다. 운영 화면에서 「입구에서 직접 확인됨」 을
구별해 보여 주는 데 쓰고, 판정에는 쓰지 않는다.

모델·DB·네트워크를 부르지 않는다.
"""

from __future__ import annotations


def admitted_by_collection(doc) -> bool:
    """정본이 수집 입구를 지나 들어온, 우리 봇 출력이 아닌 첨부인가."""
    if doc is None:
        return False
    workspace = str(getattr(doc, "workspace", "") or "").strip()
    channel_id = str(getattr(doc, "channel_id", "") or "").strip()
    file_id = str(getattr(doc, "file_id", "") or "").strip()
    if not (workspace and channel_id and file_id):
        return False
    from .canvas import is_generated_canvas

    name = str(getattr(doc, "name", "") or "")
    return not is_generated_canvas(name, file_id)


def admitted_file_ids(docs) -> frozenset[str]:
    """`admitted_by_collection` 을 지난 정본의 파일 ID."""
    return frozenset(doc.file_id for doc in docs if admitted_by_collection(doc))


__all__ = ["admitted_by_collection", "admitted_file_ids"]
