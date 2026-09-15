"""채널 **파일 목록**에서 미수집 파일을 찾는다(B-46).

메시지 이벤트만 듣던 동안 생긴 구멍을 메운다.

- 봇을 초대하기 **전에** 올라간 파일은 이벤트가 온 적이 없다.
- 캔버스·목록에서 올린 파일도 메시지 첨부로 안 오는 경우가 있다.

사람은 "채널에 있잖아" 라고 말하는데 봇에게는 없다. 파일 목록은 **그 채널에 무엇이
있는지** 를 Slack 이 직접 말해 주는 유일한 자리다.

## 기본은 판정만

한 채널에 수백 건이 있을 수 있고, 그걸 한 번에 내려받으면 rate limit 과 변환
큐가 동시에 막힌다. 그래서 이 모듈의 기본 동작은 **세는 것**이고, 실제 수집은
호출자가 명시적으로 `apply=True` 를 줄 때만 한다.

## 하지 않는 것

- **전체 워크스페이스 스캔을 하지 않는다.** `channel` 없는 `files.list` 는
  봇이 속하지 않은 채널의 파일까지 돌려준다. 그것을 수집하면 권한 경계가
  파일 목록 한 번으로 무너진다(원칙 3).
- 중복 방지 키를 새로 만들지 않는다. `files.already_staged()` 를 쓴다.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from .files import AttachmentOrigin, AttachmentStorage, already_staged, stage_attachments

logger = logging.getLogger("tybot.archive.channel_files")


def _int_env(name: str, default: int) -> int:
    """환경변수 정수. **0 은 무제한**이 아니라 각 항목 설명을 따른다."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s 값이 정수가 아니라 기본값 %s 를 씁니다: %r", name, default, raw)
        return default


def _page_size() -> int:
    # Slack files.list 의 `count` 상한은 200 이다. 더 크게 주면 Slack 이 줄인다.
    return min(200, _int_env("TYBOT_FILES_LIST_PAGE", 100) or 100)


def _max_pages() -> int:
    """읽을 페이지 수. **0 이면 끝까지 읽는다.**

    파일 목록은 「무엇이 있는지」 를 세는 자리다. 여기서 끊으면 미수집 건수가
    실제보다 작게 나오고, 사람은 그 숫자를 보고 「다 들어왔다」 고 읽는다.
    """
    return _int_env("TYBOT_FILES_LIST_MAX_PAGES", 0)


def _page_pause() -> float:
    """페이지 사이 간격(초). files.list Tier 3 상한보다 보수적으로 호출한다.

    상한을 없애는 대신 **속도를 지킨다.** 끝까지 읽되 Slack 을 밀어붙이지 않는다.
    """
    raw = (os.environ.get("TYBOT_FILES_LIST_PAUSE") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else 3.0
    except ValueError:
        return 3.0


@dataclass
class ChannelFileScan:
    """파일 목록 조회 결과. **수집은 아직 하지 않았다.**"""

    channel_id: str
    total: int = 0            # 목록에서 본 파일 수
    known: int = 0            # 이미 staging 기록이 있는 파일
    generated_excluded: int = 0  # TYBot이 만든 답변 Canvas(재귀 수집 금지)
    candidates: list[dict] = field(default_factory=list)  # 미수집 파일의 raw 이벤트
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False   # 페이지 상한에 걸려 끝까지 못 읽었다

    @property
    def missing(self) -> int:
        return len(self.candidates)

    def summary(self) -> str:
        """사람이 읽을 한 줄. **모르는 것은 모른다고 적는다.**"""
        if self.truncated:
            tail = f"파일 목록 {self.total}건 이상(상한에 걸려 끝까지 못 읽음)"
        else:
            tail = f"파일 목록 {self.total}건"
        generated = (
            f" · TYBot 생성 제외 {self.generated_excluded}건"
            if self.generated_excluded else ""
        )
        return f"{tail} · 수집됨 {self.known}건 · 미수집 {self.missing}건{generated}"


def _generated_canvas(raw: dict) -> bool:
    """파일 목록에 섞인 TYBot 답변 Canvas인가.

    ``files.list`` 경로에는 본문이 없으므로 제목과 생성 ID만 쓴다. 본문 Disclaimer
    검사는 Canvas 다운로드 경로에서 한 번 더 수행한다.
    """
    from .canvas import is_generated_canvas

    title = str(raw.get("title") or raw.get("name") or "")
    return is_generated_canvas(title, str(raw.get("id") or ""))


def scan(client, channel_id: str, storage: AttachmentStorage) -> ChannelFileScan:
    """채널 파일 목록을 끝까지 읽고 **미수집 파일만** 골라 돌려준다.

    내려받지 않는다. 판정만 한다.
    """
    if not channel_id:
        # 채널 없는 `files.list` 는 워크스페이스 전체를 훑는다. 실수로라도 그
        # 호출이 나가지 않게 여기서 막는다.
        raise ValueError("channel_id 없이 파일 목록을 조회하지 않습니다")

    out = ChannelFileScan(channel_id=channel_id)
    seen: set[str] = set()
    page = 1
    limit = _max_pages()
    while True:
        try:
            res = client.files_list(channel=channel_id, count=_page_size(), page=page)
        except Exception as exc:  # noqa: BLE001 - 한 페이지 실패가 전체를 버리게 두지 않는다
            out.warnings.append(f"파일 목록 조회 실패(page={page}): {exc}")
            # 첫 페이지부터 실패했으면 아무것도 모르는 것이고, 중간이면 **부분만**
            # 안다. 어느 쪽이든 「없다」 가 아니다.
            out.truncated = True
            break

        files = res.get("files") or []
        for raw in files:
            file_id = str(raw.get("id") or "")
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            out.total += 1
            if _generated_canvas(raw):
                out.generated_excluded += 1
                continue
            if already_staged(storage, file_id):
                out.known += 1
                continue
            out.candidates.append(raw)

        paging = res.get("paging") or {}
        pages = int(paging.get("pages") or 1)
        if page >= pages:
            break
        if limit and page >= limit:
            out.truncated = True
            break
        page += 1
        pause = _page_pause()
        if pause:
            time.sleep(pause)
    return out


def collect(
    scan_result: ChannelFileScan,
    bot_token: str | None,
    storage: AttachmentStorage,
    *,
    workspace: str,
) -> list:
    """판정된 미수집 파일을 **기존 첨부 경로 그대로** 수집한다.

    `stage_attachments` 를 부른다 — 격리 저장·변환·PII 검사가 전부 그 안에 있다.
    여기서 따로 처리하면 이 입구만 검사가 빠진다.

    호출자가 `apply` 를 판단한 뒤에만 부른다. 이 함수 자체는 되묻지 않는다.
    """
    candidates = [
        raw for raw in scan_result.candidates
        if raw.get("id")
        and not _generated_canvas(raw)
        and not already_staged(storage, str(raw["id"]))
    ]
    if not candidates:
        return []
    return stage_attachments(
        candidates, bot_token, storage,
        origin=AttachmentOrigin(
            workspace=workspace,
            channel_id=scan_result.channel_id,
            # 파일 목록에는 메시지 좌표가 없다. **빈 문자열로 둔다** — 0 이나
            # 가짜 ts 를 넣으면 없는 메시지를 가리키는 좌표가 된다.
            message_ts="",
            thread_ts="",
        ),
    )


def referenced_files(client, channel_id: str, file_ids) -> tuple[list[dict], list[str]]:
    """명시된 Slack 파일 ID 중 이 채널 공유가 확인된 파일만 반환한다.

    Canvas 본문에서 얻은 ID만 믿지 않는다. ``files.info``의 ``channels`` 또는
    ``groups``에 현재 채널이 있어야 한다. 공유 정보가 없으면 허용하지 않는다.
    """
    events: list[dict] = []
    warnings: list[str] = []
    for file_id in file_ids:
        try:
            info = client.files_info(file=file_id)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{file_id}: files.info 실패 - {exc}")
            continue
        raw = info.get("file") or {}
        if not raw.get("id"):
            warnings.append(f"{file_id}: 파일 정보를 받지 못했습니다")
            continue
        shared = set(raw.get("channels") or []) | set(raw.get("groups") or [])
        if channel_id not in shared:
            warnings.append(f"{file_id}: 이 채널 공유 여부를 확인할 수 없어 건너뜁니다")
            continue
        events.append(raw)
    return events, warnings
