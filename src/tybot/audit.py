"""질의응답 감사 기록 — 요청 1건마다 JSONL 1줄 + 일자별 MD 1블록.

환각방지 4겹의 마지막 겹("사람이 잡아낼 수 있게")을 파일로 남긴다.
질문·의도·권한범위·근거·모델·비용을 모두 적어서 사고를 역추적할 수 있게 한다.

**중요: 이 기록은 아카이브가 아니다.**
- 저장 위치는 `archive/workspaces/` **밖**이다. ArchiveStore 는 이 파일을 절대 읽지 않는다.
- 봇 답변을 근거로 재사용하면 요약 재귀가 발생한다(원칙 1). 그래서 물리적으로 분리한다.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("tybot.audit")

KST = timezone(timedelta(hours=9))
MAX_TEXT = 4000  # 한 건이 로그를 잡아먹지 않게 상한
# 스레드 문맥으로 읽을 최근 문답 수. 전문이 아니라 메타데이터라 3건보다 넉넉히 본다 —
# 긴 답변 하나 때문에 앞선 관련 문답이 밀려나는 것이 예전 구조의 고장이었다.
THREAD_TURNS = 10
# 봇과의 DM 은 개인 작업공간이라 하루 안에서 이어 묻는 것이 기본이다(B-57).
# 예전 값은 2시간이었는데, 오전에 물어본 것을 오후에 이어 물으면 끊겼다.
#
# **무제한으로 두지 않는다.** 턴 수와 시간창이 없으면 오래된 주제가 새 질문에
# 섞이고, 토큰이 대화 길이에 비례해 계속 는다. 끊는 판단 자체는 분해기(LLM)가
# `reference_mode` 로 하고, 이 값은 그 판단에 보여 줄 범위의 상한일 뿐이다.
DM_CONTEXT_MINUTES = 720
# 참조가 없는 **구형 레코드**에서만 싣는 답변 조각. 지칭어 해석 전용이다.
LEGACY_ANSWER_CHARS = 600


#: 감사 JSONL 한 줄을 덧붙이는 동안 잡는 락. **보존 집행이 같은 락을 쓴다**
#: (`retention._filter_jsonl`).
#:
#: 없으면 이렇게 깨진다 — 집행이 파일을 읽어 만료된 줄을 뺀 사본을 만드는 사이에
#: 한 줄이 덧붙고, 사본으로 바꿔 끼우는 순간 그 줄이 사라진다. 사라진 감사
#: 기록은 **사라진 줄 모른다.**
#:
#: 전에는 락을 못 잡아도 **살아 있는 파일에 그대로 덧붙였다.** 그러면 이 순서가
#: 생긴다 — 집행이 락을 잡고 → writer 가 타임아웃되고 → 집행이 크기를 확인하고
#: → writer 가 덧붙이고 → 집행이 `os.replace` 한다. 마지막 줄은 교체되는 사본에
#: 없으므로 사라진다. 크기 대조는 **확인한 뒤**의 덧붙임을 못 본다.
#:
#: 그래서 **락 없이는 살아 있는 파일을 건드리지 않는다.** 대신 옆에 쌓는다
#: (`spill_path`). 잃는 것은 없고, 다음 락 잡는 사람이 합친다.
APPEND_LOCK_TIMEOUT = 5.0


def append_lock(path: Path):
    """그 JSONL 의 덧붙이기 락. 경로는 **쓰는 쪽과 지우는 쪽이 같아야 한다.**"""
    from .lock import FileLock

    return FileLock(path.with_name(path.name + ".lock"), label=f"audit append {path.name}")


def spill_dir(path: Path) -> Path:
    """락을 못 잡았을 때 대신 쌓는 **디렉터리**.

    한 파일이 아니라 디렉터리인 이유가 이 함수의 전부다.

    한 파일에 덧붙이면 합치는 쪽과 쓰는 쪽이 **같은 파일**을 만진다. 합치는 쪽은
    읽고 → 본 파일에 붙이고 → 지운다. 읽기와 지우기 사이에 쓰는 쪽이 끼어들면
    그 줄은 읽히지도 않고 지워진다. 쓰는 쪽에는 락이 없으므로 그 틈을 좁힐 수는
    있어도 없앨 수는 없다.

    디렉터리면 그런 틈이 아예 없다. 항목 하나는 **만들어진 뒤 바뀌지 않고**,
    합치는 중에 들어온 기록은 **새 항목**이 된다. 합치는 쪽은 자기가 본 항목만
    지운다.

    디렉터리는 `qa-*.jsonl` 글롭에 안 걸린다(글롭은 한 겹만 본다). 그래서 합치기
    전에 두 번 읽히는 일도 없다.
    """
    return path.with_name(path.name + ".spill")


#: 합칠 항목. 쓰다 만 임시 파일(`.tmp-*`)과 **이름으로** 갈라야 한다 — 반쯤
#: 쓰인 줄을 감사 기록에 합치면 그게 더 나쁘다.
SPILL_ENTRY_GLOB = "*.part"


def spill_entries(path: Path) -> list[Path]:
    """합칠 항목들을 **만들어진 순서로.** 순서가 섞이면 기록의 시간순이 깨진다."""
    directory = spill_dir(path)
    if not directory.is_dir():
        return []
    return sorted(directory.glob(SPILL_ENTRY_GLOB))


def _spill_data(entry: Path) -> bytes:
    """Give each fallback record a stable identity across drain retries."""
    raw = entry.read_bytes()
    if not raw.strip():
        return b""
    try:
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("a spill entry must contain JSON objects")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OSError(f"incomplete audit spill entry: {entry}") from exc
    for index, row in enumerate(rows):
        row["_audit_spill_id"] = f"{entry.stem}:{index}"
    return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode("utf-8")


def _ends_with(path: Path, data: bytes) -> bool:
    """본 파일이 이미 **그 바이트로 끝나는가.**

    합친 뒤 지우기 전에 죽으면 같은 항목이 다시 보인다. 그때 무턱대고 또 붙이면
    같은 질문이 두 번 있었던 것처럼 보인다. 꼬리를 보고 이미 붙었으면 건너뛴다.

    본 파일에 붙이는 것은 **락을 쥔 쪽뿐**이고, 락을 쥐는 쪽은 언제나 합치기부터
    한다. 그래서 죽은 뒤 다음 합치기까지 꼬리는 그대로다.

    별개 항목에 같은 내용이 들어올 수 있으므로 `_audit_spill_id` 가 포함된
    바이트만 비교한다. 항목 이름이 같을 때만 재시도로 취급한다.
    """
    if not data or not path.is_file():
        return False
    try:
        size = path.stat().st_size
        if size < len(data):
            return False
        with path.open("rb") as handle:
            handle.seek(size - len(data))
            return handle.read() == data
    except OSError:
        return False


def drain_spill(path: Path) -> int:
    """옆에 쌓인 항목을 본 파일로 합친다. **락을 쥔 채로만 부른다.**

    돌려주는 값은 합친 줄 수다. 읽지 못한 항목이 있으면 `OSError` 를 던진다 —
    조용히 0 을 돌려주면 부르는 쪽이 「합칠 것이 없었다」 로 읽고, 그 기록은
    아무도 다시 보지 않는다.
    """
    merged = 0
    for entry in spill_entries(path):
        data = _spill_data(entry)  # 못 읽거나 잘린 JSON 이면 그대로 올린다(OSError)
        if data.strip() and not _ends_with(path, data):
            # 본 파일은 없을 수 있다(`"ab"` 가 만든다). 그 **디렉터리**는 반드시
            # 있다 — 옆자리가 그 아래에 있어야 여기까지 온다.
            if path.is_file():
                with path.open("rb") as current:
                    current.seek(max(0, current.seek(0, os.SEEK_END) - len(data)))
                    tail = current.read()
                if tail and not tail.endswith(b"\n"):
                    fragment = tail.rsplit(b"\n", 1)[-1]
                    if not data.startswith(fragment):
                        raise OSError(f"audit tail does not match spill entry: {path}")
                    data = data[len(fragment):]
            with path.open("ab") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            merged += len([line for line in data.splitlines() if line.strip()])
        entry.unlink(missing_ok=True)
    return merged


@contextlib.contextmanager
def appending(path: Path):
    """덧붙일 **자리**를 내준다. 락을 못 잡으면 옆자리(`spill_path`)다.

    부르는 쪽은 `with appending(p) as target:` 로 받아 **그 경로에** 쓴다.
    살아 있는 파일을 직접 쓰지 않는 이유가 이 함수의 전부다 — 락 없는 덧붙임이
    집행의 크기 확인과 교체 사이에 끼면 그 줄이 사라진다.
    """
    from .lock import AlreadyRunning, LockUnavailable

    lock = None
    held = False
    try:
        # **락을 만드는 것부터** try 안이다. 밖에 두면 락 경로를 못 만드는 날
        # 감사 기록 자체가 예외로 날아간다 — 보호하려던 것을 보호가 죽인다.
        lock = append_lock(path)
        lock.acquire(timeout=APPEND_LOCK_TIMEOUT)
        held = True
    except (AlreadyRunning, LockUnavailable, OSError) as exc:
        logger.warning("감사 기록 락을 못 잡았다(옆에 쌓는다): %s", exc)
    try:
        if not held:
            yield from _spilling(path)
            return
        # 락을 잡았으면 **먼저 합친다.** 그래야 옆에 쌓인 것이 오래 안 보이지
        # 않는다 — 보통 다음 기록이 밀리초 뒤에 온다.
        with contextlib.suppress(OSError):
            merged = drain_spill(path)
            if merged:
                logger.info("락 없이 쌓였던 감사 기록 %d줄을 합쳤다: %s", merged, path.name)
        yield path
    finally:
        if held and lock is not None:
            lock.release()


def _spilling(path: Path):
    """옆자리에 **항목 하나**를 만든다. 다 쓴 뒤에야 합칠 대상이 된다.

    쓰는 중인 파일을 바로 `*.part` 로 두면, 그 사이에 합치는 쪽이 반쯤 쓰인 줄을
    가져간다. 그래서 임시 이름으로 쓰고 **끝난 뒤 한 번에** 이름을 바꾼다.
    """
    directory = spill_dir(path)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.time_ns():020d}-{uuid.uuid4().hex}"
    temporary = directory / f".tmp-{stamp}"
    try:
        yield temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if temporary.is_file() and temporary.stat().st_size:
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, directory / f"{stamp}.part")
    else:
        temporary.unlink(missing_ok=True)


MD_HEADER = """# 질의응답 기록 {date}

> 이 파일은 **감사 기록**이다. 아카이브 원문이 아니며 봇 답변의 근거로 쓰이지 않는다.
> 원문 아카이브는 `archive/workspaces/` 에 있다.

"""


def _clip(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\r\n", "\n").strip()
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + f"…(총 {len(s)}자)"


@dataclass
class QARecord:
    ts: str
    workspace: str
    channel: str
    channel_id: str
    user: str
    user_name: str
    question: str
    intent_kind: str
    intent_source: str
    reason: str
    hits: int
    scope: str  # 권한 판정 결과 요약 (exec / 채널 N개)
    citations: list[str] = field(default_factory=list)
    model: str | None = None
    cost_usd: float = 0.0
    elapsed_ms: int = 0
    answer: str = ""
    record_id: str = ""
    request_ts: str = ""
    response_ts: str = ""
    thread_ts: str = ""
    channel_type: str = ""
    error: str = ""
    # --- 후속 질문이 이어 갈 것 (설계: thread-follow-up-evidence.md §5.3) -----
    #
    # **여기 남기는 것은 원문 좌표지 원문이 아니다.** 다음 질문은 이 좌표를
    # 현재 권한으로 다시 열어 읽는다. 답변 문장(`answer`)은 콘솔 감사와 피드백
    # 연결용으로 남을 뿐, 근거 해석에는 쓰지 않는다 — 쓰는 순간 요약을 근거로
    # 요약하게 된다(원칙 1).
    evidence_refs: list[dict] = field(default_factory=list)
    attachment_refs: list[dict] = field(default_factory=list)
    subject_terms: list[str] = field(default_factory=list)
    context_parent_ids: list[str] = field(default_factory=list)
    context_resolution: str = "none"
    # --- 오케스트레이션 추적 (설계: master-specialist-orchestration.md §7) -----
    #
    # **업무 질문은 성공 여부와 관계없이 남긴다.** 예전에는 전문 봇을 못 고른
    # 경우가 아무 데도 안 남아서, 같은 질문이 왜 Hermes 대신 마스터로 갔는지
    # 콘솔에서 판별할 수 없었다.
    #
    # `final_responder` 가 업무 답변에서 `none` 이면 그 자체가 정책 위반이다.
    decision_id: str = ""
    required_capability: str = ""
    final_responder: str = ""
    attempted_specialists: list[str] = field(default_factory=list)
    specialist_error_code: str = ""
    planner_model: str = ""
    # One request may contain several business tasks. Keep only routing metadata;
    # questions, evidence, and answer bodies remain in the parent QA record.
    task_traces: list[dict] = field(default_factory=list)
    # --- 산출물·판정 추적 (설계: pii-guardrail-and-canvas-artifacts.md §E) ------
    #
    # **본문은 하나도 남기지 않는다.** 코드·개수·버전뿐이다. 원문값을 여기 넣으면
    # 감사 기록이 근거의 사본이 되고, 권한 경계가 두 곳으로 갈린다.
    delivery_mode: str = "message"
    artifact_layout: str = ""
    artifact_operation: str = ""
    title_source: str = ""
    harness_version: int = 0
    harness_result: str = ""
    target_unit: str = ""
    converted_cell_count: int = 0
    format_retry_count: int = 0
    guardrail_result: str = ""

    @classmethod
    def build(cls, **kw) -> QARecord:
        kw["ts"] = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
        kw.setdefault("record_id", uuid.uuid4().hex)
        kw["question"] = _clip(kw.get("question"))
        kw["answer"] = _clip(kw.get("answer"))
        return cls(**kw)

    def log_line(self) -> str:
        """journalctl 한 줄 — 경로와 무관하게 항상 질문이 보인다."""
        return (
            f'qa user={self.user_name}({self.user}) ch={self.channel} '
            f'intent={self.intent_kind}/{self.intent_source} reason={self.reason} '
            f'hits={self.hits} scope={self.scope} model={self.model} '
            f'cost=${self.cost_usd:.5f} {self.elapsed_ms}ms q="{self.question}"'
        )


class QALog:
    """JSONL(기계용) + 일자별 MD(사람용) 이중 기록."""

    def __init__(self, root: Path | str, *, write_md: bool = True) -> None:
        self.root = Path(root)
        self.write_md = write_md

    def _jsonl_path(self, ts: str) -> Path:
        return self.root / f"qa-{ts[:7]}.jsonl"  # 월별 파일

    def _md_path(self, ts: str) -> Path:
        return self.root / f"{ts[:10]}.md"  # 일자별 파일

    def write(self, rec: QARecord) -> None:
        """기록 실패가 답변을 막아서는 안 된다 — 예외는 로그만 남기고 삼킨다."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._jsonl_path(rec.ts)
            # 보존 집행이 이 파일을 거르는 중일 수 있다(`retention`). 그때는
            # `target` 이 옆자리다 — 살아 있는 파일을 락 없이 건드리지 않는다.
            with appending(path) as target, target.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            if self.write_md:
                self._append_md(rec)
        except Exception as e:  # noqa: BLE001 - 감사 실패로 봇을 죽이지 않는다
            logger.error("감사 기록 실패: %s", e)

    def recent_for_user(
        self, workspace: str, slack_user: str, *, days: int = 7, limit: int = 5
    ) -> list[tuple[str, str]]:
        """**요청자 본인의** 최근 질문만 (시각, 질문) 으로 돌려준다.

        남의 질문은 절대 섞지 않는다 - 감사 기록이 열람 우회 경로가 되면 안 된다.
        답변 생성에는 쓰이지 않는다. 화면에 보여주기 위한 것이다(원칙 1: 요약 재귀 금지).
        """
        import datetime as _dt

        if not slack_user:
            return []
        cutoff = (_dt.datetime.now(KST) - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        out: list[tuple[str, str]] = []
        try:
            months = sorted(self.root.glob("qa-*.jsonl"), reverse=True)[:2]
            for path in months:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("workspace") != workspace or row.get("user") != slack_user:
                        continue
                    ts = str(row.get("ts", ""))
                    if ts[:19] < cutoff:
                        continue
                    out.append((ts, str(row.get("question", ""))))
        except OSError as e:
            logger.warning("감사 기록 조회 실패: %s", e)
            return []
        out.sort(reverse=True)
        return out[:limit]

    def context_for_thread(
        self,
        workspace: str,
        channel_id: str,
        thread_ts: str,
        *,
        limit: int = THREAD_TURNS,
    ) -> list[dict]:
        """같은 Slack 스레드의 이전 TYBot 문답을 **구조화해서** 시간순으로 돌려준다.

        설계: `docs/design/thread-follow-up-evidence.md` §7

        예전에는 질문과 답변 전문을 돌려줬다. 그 구조는 두 곳에서 샜다.

        - 답변이 길면 앞선 관련 문답이 글자 예산에서 밀려났다. 밀려난 것은
          **오류 없이** 사라지고, 후속 질문은 지칭 대상을 잃는다.
        - 답변 문장을 다음 답의 재료로 쓰게 된다. 요약을 근거로 요약하는 길이다.

        그래서 지금은 **질문과 작은 메타데이터**를 돌려준다. 이어 갈 것은
        `evidence_refs` — 원문 좌표다. 답변 전문(`answer`)은 참조가 없는 **구형
        레코드에만** 실린다. 그 값은 지칭어 해석에만 쓰고, 검색 근거나 전문 봇
        입력으로는 절대 넘기지 않는다.
        """
        from .evidence_refs import attachment_refs_from_json, refs_from_json

        if not workspace or not channel_id or not thread_ts or limit < 1:
            return []
        rows: list[dict] = []
        try:
            for path in sorted(self.root.glob("qa-*.jsonl"), reverse=True)[:2]:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        row.get("workspace") != workspace
                        or row.get("channel_id") != channel_id
                        or row.get("thread_ts") != thread_ts
                    ):
                        continue
                    rows.append(row)
        except OSError as exc:
            logger.warning("스레드 문답 맥락 조회 실패: %s", exc)
            return []
        rows.sort(key=lambda row: str(row.get("ts") or ""))

        out: list[dict] = []
        for row in rows[-limit:]:
            refs = refs_from_json(row.get("evidence_refs"))
            attachments = attachment_refs_from_json(row.get("attachment_refs"))
            turn: dict = {
                "record_id": str(row.get("record_id") or ""),
                "ts": str(row.get("ts") or ""),
                "question": _clip(str(row.get("question") or "")),
                "intent_kind": str(row.get("intent_kind") or ""),
                "editing_text": str(row.get("answer") or ""),
                "subject_terms": [
                    str(t) for t in (row.get("subject_terms") or []) if str(t).strip()
                ][:12],
                "evidence_refs": refs,
                "attachment_refs": attachments,
                "context_parent_ids": [
                    str(t) for t in (row.get("context_parent_ids") or []) if str(t).strip()
                ][:8],
            }
            if not refs and not attachments:
                # 구형 레코드. 좌표가 없으니 지칭어를 풀 실마리가 문장뿐이다.
                # 짧게 잘라 **지칭 해석 전용**으로만 싣는다.
                turn["legacy_answer"] = _clip(str(row.get("answer") or ""))[:LEGACY_ANSWER_CHARS]
            out.append(turn)
        return out

    def context_for_dm(
        self,
        workspace: str,
        channel_id: str,
        user: str,
        *,
        limit: int = THREAD_TURNS,
        minutes: int = DM_CONTEXT_MINUTES,
    ) -> list[dict]:
        """명시적인 DM 후속 질문에 쓸 최근 **본인 문답 좌표**를 시간순으로 돌려준다.

        일반 채널에는 쓰지 않는다. DM이라도 새 질문에는 호출하지 않고 Slack 계층이
        `방금/이전/그 답변` 같은 지칭어를 확인한 경우에만 호출한다.
        """
        from .evidence_refs import attachment_refs_from_json, refs_from_json

        if not workspace or not channel_id.startswith("D") or not user or limit < 1:
            return []
        cutoff = datetime.now(KST) - timedelta(minutes=max(1, minutes))
        rows: list[dict] = []
        try:
            for path in sorted(self.root.glob("qa-*.jsonl"), reverse=True)[:2]:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        row.get("workspace") != workspace
                        or row.get("channel_id") != channel_id
                        or row.get("user") != user
                    ):
                        continue
                    try:
                        recorded = datetime.fromisoformat(str(row.get("ts") or ""))
                        if recorded.tzinfo is None:
                            recorded = recorded.replace(tzinfo=KST)
                    except ValueError:
                        continue
                    if recorded >= cutoff:
                        rows.append(row)
        except OSError as exc:
            logger.warning("DM 문답 맥락 조회 실패: %s", exc)
            return []
        rows.sort(key=lambda row: str(row.get("ts") or ""))
        out: list[dict] = []
        for row in rows[-limit:]:
            refs = refs_from_json(row.get("evidence_refs"))
            attachments = attachment_refs_from_json(row.get("attachment_refs"))
            turn: dict = {
                "record_id": str(row.get("record_id") or ""),
                "ts": str(row.get("ts") or ""),
                "question": _clip(str(row.get("question") or "")),
                "intent_kind": str(row.get("intent_kind") or ""),
                "editing_text": str(row.get("answer") or ""),
                "subject_terms": [
                    str(term) for term in (row.get("subject_terms") or []) if str(term).strip()
                ][:12],
                "evidence_refs": refs,
                "attachment_refs": attachments,
                "context_parent_ids": [
                    str(value) for value in (row.get("context_parent_ids") or []) if str(value).strip()
                ][:8],
            }
            if not refs and not attachments:
                turn["legacy_answer"] = _clip(str(row.get("answer") or ""))[:LEGACY_ANSWER_CHARS]
            out.append(turn)
        return out

    def find_answer(
        self,
        workspace: str,
        channel_id: str,
        *,
        response_ts: str = "",
        thread_ts: str = "",
    ) -> dict | None:
        """Reaction·정정이 가리키는 최근 QA 레코드를 찾는다.

        답변 본문을 다른 저장소로 복제하지 않기 위해 피드백은 이 레코드의 ID만 참조한다.
        """
        if not channel_id or not (response_ts or thread_ts):
            return None
        try:
            paths = sorted(self.root.glob("qa-*.jsonl"), reverse=True)[:2]
            for path in paths:
                lines = path.read_text(encoding="utf-8").splitlines()
                for line in reversed(lines):
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("workspace") != workspace or row.get("channel_id") != channel_id:
                        continue
                    if response_ts and row.get("response_ts") == response_ts:
                        return row
                    if thread_ts and row.get("thread_ts") == thread_ts:
                        return row
        except OSError as e:
            logger.warning("피드백 대상 QA 기록 조회 실패: %s", e)
        return None

    def by_record_id(self, workspace: str, record_id: str, *, user: str = "") -> dict | None:
        """이 ID 의 QA 기록. 없으면 `None`.

        `근거 보기` 가 **답변 당시 좌표**를 다시 열 때 쓴다(인계 §6.2). 예전에는
        버튼에 검색어를 실어 클릭 시 다시 검색했다 — 그러면 답변이 읽은 것이
        아니라 **지금 그 낱말로 나오는 것**을 보여 주게 된다.

        `user` 를 주면 본인 기록만 돌려준다. DM처럼 소유권이 필요한 호출부에서
        사용한다. 채널 답변은 같은 채널 구성원이 볼 수 있으므로 호출부가 채널 ID와
        현재 ACL을 검증한 뒤 `user` 없이 조회한다.
        """
        key = (record_id or "").strip()
        if not key or not workspace:
            return None
        try:
            # Slack 메시지의 버튼은 QA 일자보다 오래 남는다. 최근 3개 파일만 보면
            # 정상 버튼이 며칠 뒤부터 영구적으로 깨진다. ID 조회는 사용자 동작일
            # 때만 실행되므로 전체 파일을 최신순으로 찾는다.
            for path in sorted(self.root.glob("qa-*.jsonl"), reverse=True):
                for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("record_id") != key or row.get("workspace") != workspace:
                        continue
                    if user and row.get("user") != user:
                        # 남의 답변 근거를 열어 주지 않는다. 버튼 값이 새어도
                        # 다른 사람의 기록으로는 아무것도 안 나온다.
                        return None
                    return row
        except OSError as e:
            logger.warning("QA 기록 조회 실패: %s", e)
        return None

    def last_answer_for_user(self, workspace: str, channel_id: str, user: str) -> dict | None:
        """이 채널에서 **본인이** 마지막으로 받은 답변 기록.

        `/피드백` 이 어느 답변에 대한 신고인지 연결하는 데 쓴다. 리액션·정정과 달리
        슬래시 명령에는 대상 메시지가 없기 때문이다.

        남의 질문 기록은 돌려주지 않는다 - 감사기록 조회는 본인 것만이라는
        `recent_for_user` 와 같은 규칙이다.
        """
        if not channel_id or not user:
            return None
        try:
            for path in sorted(self.root.glob("qa-*.jsonl"), reverse=True)[:2]:
                for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        row.get("workspace") == workspace
                        and row.get("channel_id") == channel_id
                        and row.get("user") == user
                    ):
                        return row
        except OSError as e:
            logger.warning("최근 답변 기록 조회 실패: %s", e)
        return None

    def _append_md(self, rec: QARecord) -> None:
        path = self._md_path(rec.ts)
        new = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            if new:
                f.write(MD_HEADER.format(date=rec.ts[:10]))
            srcs = ", ".join(rec.citations) if rec.citations else "(없음)"
            f.write(
                f"## {rec.ts[11:16]} · {rec.user_name} · {rec.channel}\n\n"
                f"**질문** ({rec.intent_kind}/{rec.intent_source})\n"
                f"> {rec.question.replace(chr(10), chr(10) + '> ')}\n\n"
                f"**답변** ({rec.reason} · 근거 {rec.hits}건 · {rec.model or '-'} · "
                f"${rec.cost_usd:.5f} · {rec.elapsed_ms}ms)\n"
                f"> {rec.answer.replace(chr(10), chr(10) + '> ')}\n\n"
                f"**출처**: {srcs}\n"
                f"**권한범위**: {rec.scope}\n\n---\n\n"
            )
