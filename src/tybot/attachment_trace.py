"""첨부 하나가 어디까지 갔는지 — 단계별 판정.

설계: [`docs/design/document-pipeline-trace-and-report-summary.md`](../../docs/design/document-pipeline-trace-and-report-summary.md) §4·§8

## 왜 필요한가
`metadata.status = converted` 는 **답변에 쓸 수 있다는 뜻이 아니다.** 변환은
`stage_files()` 가 하고 아카이브 쓰기는 그 뒤 `writer.ingest()` 가 한다. 그래서 이런
상태가 실제로 가능하다.

```text
metadata.status  = converted
extracted.md     존재
원문의 [첨부추출:…] 줄  없음      ← 답변은 이 파일을 전혀 읽지 못한다
```

콘솔에는 정상으로 보이고 답변은 「자료가 없다」 고 한다. 두 사실이 화면에서 구별되지
않으면 사람은 봇이 틀렸다고 결론 내린다.

## 단계는 순서가 있다
앞 단계가 실패하면 뒤 단계는 **판정하지 않는다**(`N/A`). 「색인 실패」 라고 말하면
담당자는 재색인을 돌리는데, 원인이 변환 실패였으면 아무 것도 달라지지 않는다.
**최초 실패 단계 하나**가 조치를 정한다.

## 모르는 것은 모른다고 한다
원문 줄에는 파일명만 남고 file ID 가 없다(`[첨부추출:보고서.hwp] …`). 같은 채널에
같은 이름의 첨부가 둘 있으면 어느 것이 반영됐는지 알 수 없다. 그때는 성공도 실패도
아니라 `UNKNOWN` 이다 — 찍어서 맞추면 잘못된 안심을 준다.

## 이 모듈은 읽기만 한다
상태를 고치지 않는다. 판정만 돌려주고, 무엇을 할지는 사람이 정한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tybot.attachment_trace")

# --- 단계 -------------------------------------------------------------------
OBSERVED = "observed"
STORED = "stored"
CONVERTED = "converted"
SCREENED = "screened"
ARCHIVED = "archived"
INDEXED = "indexed"
RETRIEVABLE = "retrievable"

# 이 모듈이 파일과 DB 만으로 판정할 수 있는 단계. `selected`·`delegated`·`cited` 는
# QA 기록이 있어야 하므로 여기서 다루지 않는다(설계 §8 `--qa-record`).
STAGES = (OBSERVED, STORED, CONVERTED, SCREENED, ARCHIVED, INDEXED, RETRIEVABLE)

OK = "OK"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "N/A"
SKIPPED = "SKIP"

# 사람이 읽는 이름. 실패 사유는 **비민감 코드**만 쓴다 — 파일 본문·OCR 결과는 절대
# 담지 않는다(설계 §5).
STAGE_LABEL = {
    OBSERVED: "수집됨",
    STORED: "원본 보관",
    CONVERTED: "변환",
    SCREENED: "PII 검사",
    ARCHIVED: "원문 반영",
    INDEXED: "색인 최신",
    RETRIEVABLE: "검색 가능",
}

# 최초 실패 단계 → 담당자가 할 일. 단계마다 조치가 다르므로 문장도 달라야 한다.
STAGE_ACTION = {
    OBSERVED: "수집 이벤트나 백필 범위 문제다. `/수집상태` 로 채널을 먼저 본다.",
    STORED: "Slack 파일 다운로드 문제다. 봇 토큰과 `files:read` 스코프를 본다.",
    CONVERTED: "문서 변환 문제다. 변환기 설치와 `scripts/diagnose_attachments.py` 를 본다.",
    SCREENED: "PII 로 수집을 거절한 파일이다. **정상 동작**이며 되살리지 않는다.",
    ARCHIVED: "변환은 됐는데 원문에 안 들어갔다. "
              "`scripts/convert_staged_attachments.py --apply` 로 반영한다.",
    INDEXED: "색인이 낡았다. `tybot-index.timer` 상태를 보거나 한 회차 돌린다.",
    RETRIEVABLE: "권한·채널 식별 문제다. 묻는 사람이 그 채널 멤버인지 본다.",
}


@dataclass(frozen=True)
class StageResult:
    """단계 하나의 판정. `detail` 은 건수·코드만 담는다."""

    stage: str
    status: str
    detail: str = ""

    @property
    def blocking(self) -> bool:
        """뒤 단계를 판정할 수 없게 만드는가."""
        return self.status in (FAIL, UNKNOWN)

    def line(self) -> str:
        label = STAGE_LABEL.get(self.stage, self.stage)
        return f"  {label:<12} {self.status:<8} {self.detail}".rstrip()


@dataclass
class AttachmentTrace:
    """첨부 하나의 전 단계 판정."""

    workspace: str
    channel_id: str
    file_id: str
    name: str
    filetype: str = ""
    permalink: str = ""
    stages: list[StageResult] = field(default_factory=list)

    @property
    def first_failure(self) -> StageResult | None:
        """최초로 막힌 단계. 여기가 조치할 곳이다."""
        return next((s for s in self.stages if s.blocking), None)

    @property
    def ok(self) -> bool:
        return self.first_failure is None

    def action(self) -> str:
        stuck = self.first_failure
        return STAGE_ACTION.get(stuck.stage, "") if stuck else ""

    def report(self) -> str:
        head = f"FILE {self.file_id}  {self.name}"
        body = [s.line() for s in self.stages]
        tail = []
        stuck = self.first_failure
        if stuck:
            tail = ["", f"  최초 실패: {STAGE_LABEL.get(stuck.stage, stuck.stage)}"
                        f" ({stuck.detail or stuck.status})", f"  조치: {self.action()}"]
        return "\n".join([head, *body, *tail])


# --- 원문에서 찾기 ------------------------------------------------------------
#
# 원문 줄 모양(`archive/files.py`):
#   [첨부:자동변환] 보고서.hwp (hwp, 240KB) · <링크|원본 파일>
#   [첨부추출:보고서.hwp] 본문 한 줄
#   [첨부본문:메모.txt] 본문 한 줄
# 파일명에 `]` 가 들어간다 — `[주간업무보고] 2026.09.10_….hwp` 가 실제 이름이다.
# 그래서 정규식으로 라벨을 잡으면 첫 `]` 에서 끊기고, 이름이 안 맞아 **전부 실패로**
# 보인다(2026-09-11 실측). 이름을 넣어 접두사를 만들어 비교한다.
#
# 라벨은 코드가 정한 고정 문자열이라(`자동변환`·`미지원`·`수집제외`·`처리실패`·
# `본문 수집`·`변환`·`미변환`·`검수대기`) 대괄호가 없다. 그래서 목록 줄은 첫 `] ` 로
# 자를 수 있다.
LISTED_PREFIX = "[첨부:"
EXTRACT_KINDS = ("추출", "본문")


def line_hash(text: str) -> str:
    """원문 줄의 지문. 본문을 저장하지 않고 「있었다」 만 확인하는 데 쓴다."""
    return "sha256:" + hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def extracted_line_count(doc_lines, name: str) -> int:
    """이 파일명으로 원문에 들어간 추출 줄 수."""
    target = (name or "").strip()
    if not target:
        return 0
    prefixes = tuple(f"[첨부{kind}:{target}] " for kind in EXTRACT_KINDS)
    return sum(1 for text in doc_lines if (text or "").strip().startswith(prefixes))


def listed_in_archive(doc_lines, name: str) -> bool:
    """첨부 목록 표시(`[첨부:…]`)가 원문에 있는가.

    추출이 실패한 파일도 이 줄은 남는다. 그래서 「수집은 됐다」 의 근거가 된다.
    """
    target = (name or "").strip()
    if not target:
        return False
    for raw in doc_lines:
        text = (raw or "").strip()
        if not text.startswith(LISTED_PREFIX):
            continue
        rest = text[len(LISTED_PREFIX):]
        # 라벨에는 대괄호가 없으므로 첫 `] ` 가 라벨의 끝이다.
        _, sep, after = rest.partition("] ")
        if not sep:
            continue
        # `이름 (형식, 크기)` 모양이다. 이름 뒤에 반드시 ` (` 가 온다 — 그 조건이
        # 없으면 다른 파일명의 접두사에 걸린다.
        if after.startswith(target + " ("):
            return True
    return False


# --- metadata 읽기 -----------------------------------------------------------
def read_metadata(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("첨부 메타데이터를 읽지 못했다 %s: %s", path, exc)
        return None


def staging_root(archive_dir: Path | str) -> Path:
    """`archive/files.py::attachment_storage` 와 같은 자리여야 한다.

    어긋나면 조용히 0건이 되고, 그건 「첨부가 없다」 와 구별되지 않는다.
    """
    return Path(archive_dir).parent / "staging" / "workspaces"


def staged_attachments(
    archive_dir: Path | str, *, workspace: str = "", channel_id: str = ""
) -> list[tuple[Path, dict]]:
    """검수 폴더의 첨부 메타데이터. 워크스페이스·채널로 좁힐 수 있다."""
    root = staging_root(archive_dir)
    if not root.is_dir():
        return []
    out: list[tuple[Path, dict]] = []
    for meta_path in sorted(root.glob("*/channels/*/attachments/*/metadata.json")):
        parts = meta_path.parts
        try:
            found_ws = parts[parts.index("workspaces") + 1]
            found_ch = parts[parts.index("channels") + 1]
        except (ValueError, IndexError):
            continue
        if workspace and found_ws != workspace:
            continue
        if channel_id and found_ch != channel_id:
            continue
        meta = read_metadata(meta_path)
        if meta is not None:
            out.append((meta_path, meta))
    return out


# --- 단계 판정 ---------------------------------------------------------------
#
# 판정은 **순서대로** 하고, 막히면 뒤는 `N/A` 로 둔다. 뒤 단계를 억지로 채우면
# 「색인 실패」 를 보고 재색인을 돌리는데 원인은 변환 실패인 상황이 된다.
def _observed(meta: dict) -> StageResult:
    file_id = str(meta.get("slack_file_id") or "")
    if not file_id:
        return StageResult(OBSERVED, FAIL, "code=no-file-id")
    ts = str(meta.get("origin_message_ts") or "")
    if ts:
        return StageResult(OBSERVED, OK, f"message={ts}")
    # 옛 metadata 에는 이 필드가 없다. 첨부가 기록된 것은 사실이므로 실패가 아니다.
    return StageResult(OBSERVED, OK, "message=(구형 metadata — 시각 없음)")


def _stored(meta: dict) -> StageResult:
    digest = str(meta.get("sha256") or "")
    raw = meta.get("object_path")
    if not raw:
        return StageResult(STORED, FAIL, "code=no-object-path")
    if not Path(str(raw)).is_file():
        return StageResult(STORED, FAIL, "code=object-missing")
    if not digest:
        return StageResult(STORED, UNKNOWN, "code=no-sha256")
    return StageResult(STORED, OK, f"sha256={digest[:12]}… object=yes")


def _converted(meta: dict, meta_path: Path) -> StageResult:
    status = str(meta.get("status") or "")
    if status == "pii_refused":
        # 변환 자체는 됐고 PII 로 막은 것이다. 다음 단계에서 그렇게 말한다.
        return StageResult(CONVERTED, OK, "extracted=(PII 검사에서 폐기)")
    if status == "unsupported":
        return StageResult(CONVERTED, SKIPPED, "code=unsupported-format")
    extracted = meta_path.parent / "extracted.md"
    if not meta.get("extracted") or not extracted.is_file():
        code = str(meta.get("error") or "")
        return StageResult(
            CONVERTED, FAIL,
            f"code=convert-failed{' status=' + status if status else ''}"
            + (" (사유는 로그에 있다)" if code else ""),
        )
    try:
        body = [ln for ln in extracted.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError as exc:
        return StageResult(CONVERTED, UNKNOWN, f"code=extracted-unreadable ({exc.errno})")
    # 머리글(주석 한 줄 + 제목)만 남은 파일은 내용이 없는 것이다.
    if len(body) <= 2:
        return StageResult(CONVERTED, FAIL, "code=extracted-empty")
    return StageResult(CONVERTED, OK, f"extracted={len(body) - 2} lines")


def _screened(meta: dict) -> StageResult:
    if str(meta.get("status") or "") == "pii_refused":
        # 실패가 아니라 **의도한 차단**이다. 되살리면 원칙 5 위반이다.
        return StageResult(SCREENED, FAIL, "code=pii-refused (의도된 차단)")
    return StageResult(SCREENED, OK)


def _archived(meta: dict, doc_lines, *, same_name: int) -> StageResult:
    name = str(meta.get("name") or "")
    if same_name > 1:
        # 원문 줄에는 파일명만 남는다. 같은 이름이 여럿이면 어느 것이 반영됐는지
        # 알 수 없다 — 찍어서 맞추면 잘못된 안심을 준다.
        return StageResult(
            ARCHIVED, UNKNOWN,
            f"code=ambiguous-name (같은 이름 {same_name}건)",
        )
    count = extracted_line_count(doc_lines, name)
    if count:
        return StageResult(ARCHIVED, OK, f"lines={count}")
    if listed_in_archive(doc_lines, name):
        return StageResult(ARCHIVED, FAIL, "code=archive-lines-missing (목록 표시만 있다)")
    return StageResult(ARCHIVED, FAIL, "code=archive-mark-missing")


def _not_applicable(stage: str, because: str) -> StageResult:
    return StageResult(stage, NOT_APPLICABLE, f"{because} prerequisite failed")


def trace(
    meta: dict,
    meta_path: Path,
    *,
    workspace: str,
    channel_id: str,
    doc_lines=(),
    same_name: int = 1,
    index_state: StageResult | None = None,
    retrievable_state: StageResult | None = None,
) -> AttachmentTrace:
    """첨부 하나를 앞에서부터 판정한다.

    `index_state`·`retrievable_state` 는 호출자가 판정해 넘긴다 — DB 와
    `RequestContext` 가 필요해서 이 함수의 관심사가 아니다. 안 주면 `N/A` 다.
    """
    got = AttachmentTrace(
        workspace=workspace,
        channel_id=channel_id,
        file_id=str(meta.get("slack_file_id") or meta_path.parent.name),
        name=str(meta.get("name") or ""),
        filetype=str(meta.get("filetype") or ""),
        permalink=str(meta.get("permalink") or ""),
    )

    ordered = [
        _observed(meta),
        _stored(meta),
        _converted(meta, meta_path),
        _screened(meta),
    ]
    for result in ordered:
        got.stages.append(result)
        if result.blocking:
            # 막힌 뒤 단계는 판정하지 않는다. 조치는 최초 실패 하나가 정한다.
            stuck = STAGE_LABEL.get(result.stage, result.stage)
            got.stages += [
                _not_applicable(s, stuck)
                for s in (ARCHIVED, INDEXED, RETRIEVABLE)
                if s not in {x.stage for x in got.stages}
            ]
            return got

    archived = _archived(meta, doc_lines, same_name=same_name)
    got.stages.append(archived)
    if archived.blocking:
        stuck = STAGE_LABEL[ARCHIVED]
        got.stages += [_not_applicable(s, stuck) for s in (INDEXED, RETRIEVABLE)]
        return got

    got.stages.append(index_state or _not_applicable(INDEXED, "DB"))
    if got.stages[-1].blocking:
        got.stages.append(_not_applicable(RETRIEVABLE, STAGE_LABEL[INDEXED]))
        return got
    got.stages.append(
        retrievable_state or _not_applicable(RETRIEVABLE, "RequestContext")
    )
    return got


# --- 여러 건 요약 ------------------------------------------------------------
def summarize(traces: list[AttachmentTrace]) -> dict[str, int]:
    """최초 실패 단계별 건수. 「무엇부터 고칠까」 를 정하는 데 쓴다."""
    counts: dict[str, int] = {}
    for got in traces:
        stuck = got.first_failure
        key = stuck.stage if stuck else "ok"
        counts[key] = counts.get(key, 0) + 1
    return counts


def summary_line(counts: dict[str, int]) -> str:
    if not counts:
        return "첨부가 없습니다."
    parts = [f"정상 {counts['ok']}건"] if counts.get("ok") else []
    parts += [
        f"{STAGE_LABEL.get(stage, stage)}에서 막힘 {n}건"
        for stage, n in counts.items()
        if stage != "ok"
    ]
    return " · ".join(parts)
