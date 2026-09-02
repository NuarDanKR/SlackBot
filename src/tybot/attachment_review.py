"""첨부 원본 검수 — 승인된 것만 답변에 쓴다.

## 왜 검수가 필요한가
`stage_files()` 는 첨부 원본을 `objects/` 에 격리 보관하고 `pending_review` 로 표시한다.
그런데 **승인으로 넘기는 코드가 없어서** 원본은 쌓이기만 하고 쓰이지 못했다.

원본을 그대로 LLM 에 보내기로 결정하면서(검토 §3) 이 게이트가 안전장치의 전부가 된다.
수집 단계 PII 거절은 **텍스트 기반**이라 스캔본·이미지에는 작동하지 않는다. 우리가 못
읽어서 못 걸러낸 것이 벤더로 가는 유일한 경로가 여기이므로, 사람이 한 번 본 것만 통과시킨다.

## 승인은 파일에 남긴다
DB 가 없어도 동작해야 한다(첨부 수집은 DB 와 무관하다). 메타데이터 옆에 상태를 쓰고,
누가 언제 무엇을 근거로 승인했는지 함께 남긴다 — 나중에 "이건 왜 나갔나" 를 답할 수
있어야 한다.

## 되돌릴 수 있다
승인은 되돌릴 수 있다(`reject`). 원본 바이트는 지우지 않는다 — 지우면 오판을 다시
검토할 근거가 사라진다. 상태만 바꾼다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("tybot.attachment_review")

PENDING = "pending_review"
APPROVED = "approved"
REJECTED = "rejected"
FAILED = "failed"

STATES = (PENDING, APPROVED, REJECTED, FAILED)


@dataclass(frozen=True)
class Attachment:
    """검수 대상 하나. 본문은 담지 않는다 — 경로만 들고 필요할 때 읽는다."""

    workspace: str
    channel_id: str
    file_id: str
    name: str
    filetype: str
    mimetype: str
    size: int
    status: str
    object_path: Path | None
    meta_path: Path
    approved_by: str = ""
    approved_at: str = ""
    note: str = ""

    @property
    def is_approved(self) -> bool:
        return self.status == APPROVED


def staging_root(archive_dir: Path | str) -> Path:
    """`stage_files` 가 쓰는 위치와 같아야 한다. 어긋나면 조용히 0건이 된다."""
    return Path(archive_dir).parent / "staging" / "workspaces"


def _read_meta(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("첨부 메타데이터를 읽지 못했다 %s: %s", path, e)
        return None


def _from_meta(meta: dict, meta_path: Path, workspace: str, channel_id: str) -> Attachment:
    raw_object = meta.get("object_path")
    return Attachment(
        workspace=workspace,
        channel_id=channel_id,
        file_id=meta_path.parent.name,
        name=str(meta.get("name") or ""),
        filetype=str(meta.get("filetype") or ""),
        mimetype=str(meta.get("mimetype") or ""),
        size=int(meta.get("declared_size") or 0),
        status=str(meta.get("status") or PENDING),
        object_path=Path(raw_object) if raw_object else None,
        meta_path=meta_path,
        approved_by=str(meta.get("approved_by") or ""),
        approved_at=str(meta.get("approved_at") or ""),
        note=str(meta.get("review_note") or ""),
    )


def scan(archive_dir: Path | str, *, status: str | None = None) -> list[Attachment]:
    """검수 폴더 전체를 훑는다. `status` 를 주면 그 상태만."""
    root = staging_root(archive_dir)
    if not root.is_dir():
        return []
    out: list[Attachment] = []
    for meta_path in sorted(root.glob("*/channels/*/attachments/*/metadata.json")):
        meta = _read_meta(meta_path)
        if meta is None:
            continue
        # .../workspaces/<ws>/channels/<ch>/attachments/<file>/metadata.json
        parts = meta_path.parts
        try:
            ws = parts[parts.index("workspaces") + 1]
            ch = parts[parts.index("channels") + 1]
        except (ValueError, IndexError):
            continue
        item = _from_meta(meta, meta_path, ws, ch)
        if status is None or item.status == status:
            out.append(item)
    return out


def pending(archive_dir: Path | str) -> list[Attachment]:
    return scan(archive_dir, status=PENDING)


def _write_status(item: Attachment, status: str, *, actor: str, note: str) -> Attachment:
    meta = _read_meta(item.meta_path) or {}
    meta["status"] = status
    meta["approved_by"] = actor
    meta["approved_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    meta["review_note"] = note[:500]
    tmp = item.meta_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(item.meta_path)
    # 파일명·본문은 남기지 않는다. 누가 무엇을 어떤 상태로 바꿨는지만.
    logger.info(
        "첨부 검수 ws=%s ch=%s file=%s -> %s actor=%s",
        item.workspace, item.channel_id, item.file_id, status, actor,
    )
    return _from_meta(_read_meta(item.meta_path) or meta, item.meta_path,
                      item.workspace, item.channel_id)


def approve(item: Attachment, *, actor: str, note: str = "") -> Attachment:
    """이 원본을 답변에 쓸 수 있게 한다. 사람만 부른다."""
    if not actor.strip():
        raise ValueError("승인자를 남기지 않은 승인은 받지 않는다")
    return _write_status(item, APPROVED, actor=actor, note=note)


def reject(item: Attachment, *, actor: str, note: str = "") -> Attachment:
    """승인을 막거나 되돌린다. 원본 바이트는 지우지 않는다 — 재검토 근거가 사라진다."""
    return _write_status(item, REJECTED, actor=actor, note=note)


def find_approved(
    archive_dir: Path | str, *, workspace: str, channel_id: str, name: str
) -> Attachment | None:
    """채널·파일명으로 승인된 원본을 찾는다.

    원문 줄에는 파일 ID 가 없고 이름만 남는다(`[첨부:검수대기] 보고서.pdf (pdf, 240KB)`).
    같은 이름이 여러 개면 **찾지 못한 것으로 본다** — 어느 것인지 모르는 채로 원본을
    벤더에 보내지 않는다.
    """
    target = (name or "").strip()
    if not target:
        return None
    matches = [
        a for a in scan(archive_dir, status=APPROVED)
        if a.workspace == workspace and a.channel_id == channel_id and a.name == target
    ]
    return matches[0] if len(matches) == 1 else None


SUMMARY_EMPTY = "검수 대기 중인 첨부가 없습니다."


def summary(items: list[Attachment]) -> str:
    """운영자용 목록. 파일명은 담당자가 봐야 하므로 남기지만 본문은 절대 열지 않는다."""
    if not items:
        return SUMMARY_EMPTY
    lines = [f"*검수 대기 {len(items)}건*"]
    for a in items[:20]:
        kb = max(1, a.size // 1024)
        lines.append(
            f"• `{a.file_id}` {a.name} ({a.filetype or a.mimetype or '?'}, {kb}KB) "
            f"— {a.workspace}/{a.channel_id}"
        )
    if len(items) > 20:
        lines.append(f"… 그 외 {len(items) - 20}건")
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------
#
# 콘솔 화면은 다른 담당이다. 그전까지 운영자가 승인할 수단이 없으면 원본 전송 기능
# 자체가 영원히 0건이므로, 최소한의 명령줄을 둔다.
def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="첨부 원본 검수 — 승인된 것만 답변에 쓰인다")
    ap.add_argument("action", choices=("list", "approve", "reject"))
    ap.add_argument("file_id", nargs="?", help="approve/reject 대상 (list 로 확인)")
    ap.add_argument("--actor", default=os.getenv("USER") or os.getenv("USERNAME") or "",
                    help="승인자. 누가 승인했는지 남지 않는 승인은 받지 않는다")
    ap.add_argument("--note", default="", help="판단 근거 한 줄")
    ap.add_argument("--archive", default=os.getenv("ARCHIVE_DIR", "./archive"))
    args = ap.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(message)s")

    if args.action == "list":
        print(summary(pending(args.archive)))
        return 0

    if not args.file_id:
        print("대상 file_id 가 필요하다. `list` 로 확인하라.")
        return 2

    items = [a for a in scan(args.archive) if a.file_id == args.file_id]
    if len(items) != 1:
        print(f"file_id `{args.file_id}` 로 {len(items)}건이 나왔다. 하나여야 한다.")
        return 1

    item = items[0]
    try:
        if args.action == "approve":
            done = approve(item, actor=args.actor, note=args.note)
        else:
            done = reject(item, actor=args.actor, note=args.note)
    except ValueError as e:
        print(f"거절: {e}")
        return 2
    print(f"{done.name} -> {done.status} (by {done.approved_by})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
