"""질의응답 감사 기록 — 요청 1건마다 JSONL 1줄 + 일자별 MD 1블록.

환각방지 4겹의 마지막 겹("사람이 잡아낼 수 있게")을 파일로 남긴다.
질문·의도·권한범위·근거·모델·비용을 모두 적어서 사고를 역추적할 수 있게 한다.

**중요: 이 기록은 아카이브가 아니다.**
- 저장 위치는 `archive/channels/` **밖**이다. ArchiveStore 는 이 파일을 절대 읽지 않는다.
- 봇 답변을 근거로 재사용하면 요약 재귀가 발생한다(원칙 1). 그래서 물리적으로 분리한다.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("tybot.audit")

KST = timezone(timedelta(hours=9))
MAX_TEXT = 4000  # 한 건이 로그를 잡아먹지 않게 상한

MD_HEADER = """# 질의응답 기록 {date}

> 이 파일은 **감사 기록**이다. 아카이브 원문이 아니며 봇 답변의 근거로 쓰이지 않는다.
> 원문 아카이브는 `archive/channels/` 에 있다.

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
            with self._jsonl_path(rec.ts).open("a", encoding="utf-8") as f:
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
