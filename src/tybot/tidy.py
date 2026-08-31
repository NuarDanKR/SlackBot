"""정리 잡 — 아카이브 건강 상태를 점검하고 리포트만 남긴다.

`python -m tybot.tidy` (systemd 타이머가 15분마다 실행)

## 이 잡이 존재하는 이유
아카이브는 **조용히 고장난다.** 봇은 정상 응답하는데 원문이 안 쌓이거나, 스키마가 깨져
검색에서 통째로 빠지거나, 채널 하나만 며칠째 멈춰 있어도 아무도 모른다.
사람이 `상태` 를 물어봐야 드러나는 구조라 사고를 늦게 발견한다.

## 절대 하지 않는 것
- **원문을 수정하지 않는다.** 읽기만 한다([agent-architecture.md](../../docs/design/agent-architecture.md) 2절).
  틀린 내용을 고치는 것과 왜곡하는 것을 코드로 구별할 수 없기 때문이다.
- 리포트는 `archive/` **밖**에 쓴다. 아카이브 안에 쓰면 그게 다시 근거로 검색된다(요약 재귀).
- LLM 을 호출하지 않는다. 전부 결정론적 검사다.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .archive.store import RAW_LINE_RE, ArchiveStore, SchemaError, load_doc
from .archive.writer import KST
from .envfile import load_env_file

log = logging.getLogger("tybot.tidy")

STALE_DAYS = 3  # 이 기간 수집이 없으면 '밀림'으로 본다
ISO_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


@dataclass
class Finding:
    """리포트 한 줄. 심각도별로 묶어서 보여준다."""

    level: str  # error | warn | info
    channel: str
    detail: str


@dataclass
class TidyReport:
    generated_at: str
    docs: int = 0
    raw_lines: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warns(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    def summary_line(self) -> str:
        """journald 한 줄 — 이것만 봐도 상태를 안다."""
        return (
            f"tidy docs={self.docs} lines={self.raw_lines} "
            f"errors={len(self.errors)} warns={len(self.warns)}"
        )

    def to_markdown(self) -> str:
        out = [
            f"# 아카이브 점검 {self.generated_at[:10]}",
            "",
            "> 이 파일은 **점검 리포트**다. 아카이브 원문이 아니며 봇 답변의 근거로 쓰이지 않는다.",
            "",
            f"- 생성: {self.generated_at}",
            f"- 문서 {self.docs}건 · 원문 {self.raw_lines}줄",
            f"- 오류 {len(self.errors)}건 · 경고 {len(self.warns)}건",
            "",
        ]
        if not self.findings:
            out += ["## 이상 없음", ""]
            return "\n".join(out)

        for level, title in (("error", "## 오류 (검색에서 빠진다)"), ("warn", "## 경고")):
            rows = [f for f in self.findings if f.level == level]
            if not rows:
                continue
            out += [title, ""]
            out += [f"- **{f.channel}** — {f.detail}" for f in rows]
            out.append("")
        return "\n".join(out)


def _stale_days(last_ingested: str | None, now: datetime) -> float | None:
    """프론트매터의 last_ingested 로부터 며칠 지났나. 파싱 불가면 None."""
    if not last_ingested or not ISO_TS_RE.match(last_ingested):
        return None
    try:
        ts = datetime.fromisoformat(last_ingested)
    except ValueError:
        try:
            ts = datetime.strptime(last_ingested[:10], "%Y-%m-%d").replace(tzinfo=KST)
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=KST)
    return (now - ts).total_seconds() / 86400


def inspect(archive_dir: str | Path, *, stale_days: int = STALE_DAYS) -> TidyReport:
    """아카이브를 읽기만 하며 점검한다."""
    now = datetime.now(KST)
    report = TidyReport(generated_at=now.strftime("%Y-%m-%dT%H:%M:%S+09:00"))
    store = ArchiveStore(archive_dir)

    # 1) 스키마 위반 - 이 파일들은 검색에서 통째로 빠진다. 가장 위험한 조용한 고장.
    for path, why in store.broken():
        report.findings.append(Finding("error", path.name, f"스키마 위반: {why}"))

    for path in store.source_files():
        try:
            doc = load_doc(path)
        except SchemaError:
            continue  # 위에서 이미 오류로 잡았다
        report.docs += 1
        report.raw_lines += len(doc.raw_lines)

        # 2) 수집 밀림
        age = _stale_days(doc.last_ingested, now)
        if age is None:
            report.findings.append(
                Finding("warn", doc.channel, "last_ingested 를 읽을 수 없다(형식 확인 필요)")
            )
        elif age >= stale_days:
            report.findings.append(
                Finding("warn", doc.channel, f"{age:.1f}일째 수집 없음 — 봇 초대·권한 확인")
            )

        # 3) 원문 0줄 - 파일은 있는데 내용이 없다
        if not doc.raw_lines:
            report.findings.append(Finding("warn", doc.channel, "원문 0줄"))

        # 4) 형식이 깨져 파싱되지 않는 원문 줄 (검색에서 조용히 누락된다)
        text = path.read_text(encoding="utf-8")
        raw_section = text.split("## 원문", 1)[-1] if "## 원문" in text else ""
        malformed = [
            ln for ln in raw_section.splitlines()
            if ln.strip().startswith(">") and not RAW_LINE_RE.match(ln.strip())
        ]
        if malformed:
            report.findings.append(
                Finding("error", doc.channel, f"파싱 안 되는 원문 줄 {len(malformed)}개")
            )

        # 5) 중복 라인 - 수집 멱등성이 깨졌다는 신호
        seen: set[str] = set()
        dups = 0
        for ln in doc.raw_lines:
            key = f"{ln.ts}|{ln.speaker}|{ln.text}"
            if key in seen:
                dups += 1
            seen.add(key)
        if dups:
            report.findings.append(
                Finding("warn", doc.channel, f"중복 원문 {dups}줄 — 수집 멱등성 확인")
            )

    return report


def write_report(report: TidyReport, reports_dir: str | Path) -> Path | None:
    """리포트를 아카이브 **밖**에 쓴다. 실패해도 점검 결과는 로그로 남는다."""
    path = Path(reports_dir) / f"tidy-{report.generated_at[:10]}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.to_markdown(), encoding="utf-8")
        return path
    except OSError as e:
        log.error("리포트 기록 실패(%s): %s", path, e)
        return None


def prune_reports(reports_dir: str | Path, *, keep_days: int = 30) -> int:
    """오래된 리포트 정리. 리포트는 재생성 가능하므로 지워도 된다."""
    root = Path(reports_dir)
    if not root.is_dir():
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed = 0
    for p in root.glob("tidy-*.md"):
        if p.stem[5:15] < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("환경설정 출처: %s", load_env_file())
    archive_dir = os.getenv("ARCHIVE_DIR", "./archive")
    reports_dir = os.getenv("REPORTS_DIR") or str(Path(archive_dir).parent / "reports")

    report = inspect(archive_dir, stale_days=int(os.getenv("TIDY_STALE_DAYS", STALE_DAYS)))
    log.info("%s", report.summary_line())
    for f in report.errors:
        log.error("tidy %s: %s", f.channel, f.detail)
    for f in report.warns:
        log.warning("tidy %s: %s", f.channel, f.detail)

    path = write_report(report, reports_dir)
    if path:
        log.info("리포트 기록: %s", path)
    prune_reports(reports_dir)

    # 오류가 있어도 잡 자체는 성공으로 둔다 - 타이머가 실패로 뜨면 진짜 장애를 가린다.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
