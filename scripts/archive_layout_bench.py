#!/usr/bin/env python3
"""두 저장 구조의 **우리 쪽 비용**을 잰다 — 설계 §6·§7.

    python scripts/archive_layout_bench.py --a <lab>/a --b <lab>/b --out <lab>/reports

Hermes 쪽은 이미 동등함이 증명돼 있다(`tests/test_archive_layout_equivalence.py`).
그러므로 여기서 재는 것은 **우리가 치르는 비용**뿐이고, §6 의 판정 규칙에 따라
이 숫자들은 **구조를 고르는 근거가 아니다.** 고칠 수 있는 약점(1·2·4)은
「구조 1 을 고르면 먼저 무엇을 고쳐야 하는가」 의 목록으로 쓴다.

## 재는 것

| # | 항목 | §6 |
|---|---|---|
| M1 | 메시지 1건 수집 | 1 |
| M2 | 검색 1회 — warm, 쓰기 중 | 2 |
| M3 | 검색 1회 — cold | 3 |
| M4 | 증분 재색인 | 4 |
| M5 | 용량·파일 수 | — |

## 공정하게 재기 위한 규칙

**입력 배치를 건드리지 않는다.** 주어진 `--a`·`--b` 를 작업 사본으로 복사하고
거기에 쓴다. 수집을 재려면 써야 하는데, 원본에 쓰면 그 배치는 더 이상 동등성
시험을 통과한 그 배치가 아니다.

**로직은 같게, 배치만 다르게.** 구조 2 는 `writer.ingest` 를 그대로 쓴다.
구조 1 은 아직 writer 가 없어 이 파일 안에서 만드는데, `screen`·`format_line`·
`dedupe_line` 을 **writer 의 것을 그대로 불러** 쓴다. 안 그러면 재는 것이 구조
차이가 아니라 두 구현의 차이가 된다.

**cold 는 반쪽이다.** `ArchiveStore` 를 새로 만들면 파싱 캐시는 비지만 OS 페이지
캐시는 그대로다. 그래서 M3 은 「파싱 비용」 을 재는 것이고 「디스크에서 읽는
비용」 은 아니다. 보고서에 그대로 적는다 — 반쪽인 줄 모르고 쓰면 구조 1 의
cold 이점이 실제보다 작게 보인다.

## 하지 않는 것
- 운영 아카이브·운영 DB 를 쓰지 않는다(§8). 경로를 주면 거부한다
- 모델을 부르지 않는다. 토큰 비교는 §5 에서 이미 증명으로 끝났다
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from archive_layout_convert import (  # noqa: E402
    PROTECTED,
    _refuse_operational,
    _utf8_console,
)

from tybot.access import RequestContext  # noqa: E402
from tybot.archive import writer  # noqa: E402
from tybot.archive.store import ArchiveStore  # noqa: E402
from tybot.lock import archive_write_lock  # noqa: E402

LAYOUTS = ("a", "b")
DEFAULT_ROUNDS = 30
# 준비 회차. 첫 회차는 import·JIT·디렉터리 생성이 섞여 들어와 늘 느리다.
WARMUP = 3


# ---------------------------------------------------------------------------
# 측정값
# ---------------------------------------------------------------------------

@dataclass
class Samples:
    """한 항목의 회차별 값(ms). **중위값으로 말한다.**

    평균을 쓰면 GC 한 번이 결론을 바꾼다. 편차를 함께 적는 이유는, 편차가 두
    구조의 차이보다 크면 그 비교는 아무 말도 못 하기 때문이다.
    """

    label: str
    values: list[float] = field(default_factory=list)

    @property
    def median(self) -> float:
        return statistics.median(self.values) if self.values else float("nan")

    @property
    def p90(self) -> float:
        if not self.values:
            return float("nan")
        ordered = sorted(self.values)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]

    @property
    def spread(self) -> float:
        return statistics.pstdev(self.values) if len(self.values) > 1 else 0.0

    def as_json(self) -> dict:
        return {
            "label": self.label,
            "rounds": len(self.values),
            "median_ms": round(self.median, 4),
            "p90_ms": round(self.p90, 4),
            "stdev_ms": round(self.spread, 4),
        }


@contextlib.contextmanager
def timed(into: Samples):
    start = time.perf_counter_ns()
    try:
        yield
    finally:
        into.values.append((time.perf_counter_ns() - start) / 1e6)


# ---------------------------------------------------------------------------
# 구조 1 수집 — 아직 writer 가 없다
# ---------------------------------------------------------------------------

def ingest_layout_a(root: Path, channel_file: Path, message: writer.IncomingMessage) -> None:
    """채널당 파일 하나에 한 줄 더한다.

    **`writer` 의 함수를 그대로 쓴다.** 여기서 검사나 형식을 새로 짜면 재는 것이
    구조 차이가 아니라 두 구현의 차이가 된다.

    지금 writer 가 하는 것과 같은 모양으로 한다 — 파일을 통째로 읽고, 중복을
    보고, 프론트매터를 고치고, 통째로 다시 쓴다. 이게 구조 1 의 「지금 상태」 다.
    append writer 를 만들면 이 비용은 사라지고, 그래서 §6 이 이 항목을 결정 근거로
    쓰지 말라고 한 것이다.

    **쓰기 잠금을 똑같이 잡는다.** 처음 쟀을 때 구조 1 이 두 배 빨랐는데, 그중
    상당 부분이 「구조 2 만 잠금을 잡는다」 였다. 구조가 아니라 **한쪽만 안전
    장치를 달고 잰 것**이라 그 숫자로는 아무 말도 할 수 없었다.
    """
    with archive_write_lock(root):
        text = channel_file.read_text(encoding="utf-8") if channel_file.exists() else ""
        seen = {writer.dedupe_line(ln) for ln in text.splitlines()}
        line = writer.format_line(message)
        if writer.dedupe_line(line) in seen:
            return
        if writer.screen(message.text):
            return
        body = text.rstrip("\n") + "\n" + line + "\n"
        # 프론트매터도 같이 고친다. 안 고치면 구조 1 만 일을 덜 하고 빨라진다.
        body = re.sub(
            r"^last_ingested:.*$",
            f"last_ingested: {datetime.now(UTC).isoformat(timespec='seconds')}",
            body, count=1, flags=re.MULTILINE,
        )
        counted = re.search(r"^doc_count:\s*(\d+)\s*$", body, flags=re.MULTILINE)
        if counted:
            body = (
                body[: counted.start()]
                + f"doc_count: {int(counted.group(1)) + 1}"
                + body[counted.end():]
            )
        channel_file.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# 대상 고르기
# ---------------------------------------------------------------------------

@dataclass
class Target:
    """한 채널을 두 배치에서 가리킨다. **같은 채널을 재야 비교가 된다.**"""

    channel: str
    channel_id: str
    workspace: str
    lines: int
    file_a: Path


def pick_targets(store_a: ArchiveStore, root_a: Path) -> list[Target]:
    """줄 수가 가장 적은·중간·가장 많은 채널 셋.

    한 채널만 재면 그 채널의 크기가 결론이 된다. 구조 1 의 약점은 **큰 채널에서**
    나타나므로(파일을 통째로 다시 쓴다), 큰 것이 반드시 들어가야 한다.
    """
    docs = [d for d in store_a.docs() if d.channel and not getattr(d, "dm_user", None)]
    if not docs:
        return []
    ranked = sorted(docs, key=lambda d: len(d.raw_lines))
    picks = {0, len(ranked) // 2, len(ranked) - 1}
    out: list[Target] = []
    for idx in sorted(picks):
        doc = ranked[idx]
        out.append(Target(
            channel=doc.channel,
            channel_id=doc.channel_id or "",
            workspace=doc.workspace,
            lines=len(doc.raw_lines),
            file_a=Path(doc.path),
        ))
    return out


def _message(n: int) -> writer.IncomingMessage:
    """잴 때마다 **다른** 줄이어야 한다. 같으면 중복으로 걸러져 쓰기가 안 일어난다."""
    return writer.IncomingMessage(
        ts=datetime.now(UTC),
        speaker="실측",
        text=f"실측용 한 줄 {n}",
    )


# ---------------------------------------------------------------------------
# M1 — 메시지 1건 수집
# ---------------------------------------------------------------------------

def bench_ingest(work: dict[str, Path], targets: list[Target], rounds: int) -> list[Samples]:
    out: list[Samples] = []
    for target in targets:
        label = f"{target.channel} ({target.lines}줄)"
        sa = Samples(f"M1 수집 · 구조1 · {label}")
        sb = Samples(f"M1 수집 · 구조2 · {label}")
        file_a = work["a"] / target.file_a.relative_to(_batch_root(target.file_a))
        for n in range(rounds + WARMUP):
            msg = _message(n)
            with timed(sa):
                ingest_layout_a(work["a"], file_a, msg)
            with timed(sb):
                writer.ingest(
                    work["b"],
                    workspace=target.workspace,
                    channel=target.channel,
                    channel_id=target.channel_id or None,
                    messages=[_message(n)],
                    visibility="private",
                    acl=[target.channel],
                )
        del sa.values[:WARMUP]
        del sb.values[:WARMUP]
        out += [sa, sb]
    return out


def _batch_root(path: Path) -> Path:
    """`…/workspaces/<ws>/channels/x.md` 에서 배치 뿌리를 되찾는다."""
    for parent in path.parents:
        if parent.name == "workspaces":
            return parent.parent
    return path.parent


# ---------------------------------------------------------------------------
# M2·M3 — 검색
# ---------------------------------------------------------------------------

def pick_queries(store: ArchiveStore) -> list[str]:
    """실제 본문에서 뽑는다. **지어내지 않는다.**

    2글자 한국어를 반드시 넣는다 — pg_bigm 을 고른 이유이자 우리 쓰임에서 가장
    흔한 모양이다(기성·타설·결재·예산). 그리고 0건이 나오는 것도 하나 넣는다.
    0건 경로는 낱말별 재검색을 돌리므로 **가장 비싼 경로**다.
    """
    words: dict[str, int] = {}
    for doc in store.docs():
        for line in doc.raw_lines:
            for word in str(line.text).split():
                token = "".join(ch for ch in word if ch.isalnum())
                if len(token) >= 2:
                    words[token] = words.get(token, 0) + 1
    ranked = sorted(words.items(), key=lambda kv: (-kv[1], kv[0]))
    two = next((w for w, _ in ranked if len(w) == 2), "")
    four = next((w for w, _ in ranked if len(w) >= 4), "")
    return [q for q in (two, four, "존재하지않는낱말zzz") if q]


def bench_search_cold(root: Path, queries: list[str], rounds: int, layout: str) -> list[Samples]:
    """회차마다 `ArchiveStore` 를 새로 만든다 — 파싱 캐시가 빈 상태.

    OS 페이지 캐시는 못 비운다(머리말). 그래서 이 숫자는 **파싱 비용**이다.
    """
    out = []
    for query in queries:
        sample = Samples(f"M3 검색 cold · 구조{layout} · 「{query}」")
        ctx = RequestContext(workspace="", role="exec")
        for _ in range(rounds + WARMUP):
            store = ArchiveStore(root)
            with timed(sample):
                store.search(query, ctx, limit=20)
        del sample.values[:WARMUP]
        out.append(sample)
    return out


def bench_search_warm(
    root: Path, target: Target, queries: list[str], rounds: int, layout: str
) -> list[Samples]:
    """쓰면서 검색한다. **평소 상태가 이것이다.**

    수집이 멈춘 아카이브에서 재면 구조 2 의 이점(무효화가 오늘 파일로 한정)이
    아예 안 나타난다. 그건 우리가 쓰는 상황이 아니다.
    """
    out = []
    store = ArchiveStore(root)
    ctx = RequestContext(workspace="", role="exec")
    file_a = root / target.file_a.relative_to(_batch_root(target.file_a))
    for query in queries:
        sample = Samples(f"M2 검색 warm(쓰기중) · 구조{layout} · 「{query}」")
        for n in range(rounds + WARMUP):
            if layout == "a":
                ingest_layout_a(root, file_a, _message(10_000 + n))
            else:
                writer.ingest(
                    root,
                    workspace=target.workspace,
                    channel=target.channel,
                    channel_id=target.channel_id or None,
                    messages=[_message(10_000 + n)],
                    visibility="private",
                    acl=[target.channel],
                )
            with timed(sample):
                store.search(query, ctx, limit=20)
        del sample.values[:WARMUP]
        out.append(sample)
    return out


# ---------------------------------------------------------------------------
# M4 — 증분 재색인
# ---------------------------------------------------------------------------

def bench_reindex(root: Path, layout: str, rounds: int) -> tuple[list[Samples], str]:
    """색인을 다시 세운다. **운영 DB 는 쓰지 않는다**(§8).

    DB 가 없으면 재지 않고 **왜 못 쟀는지**를 돌려준다. 빈 표를 내면 나중에 보는
    사람이 「0이었나 안 쟀나」 를 구분하지 못한다.
    """
    try:
        from tybot import search_index
    except Exception as exc:  # noqa: BLE001
        return [], f"색인 모듈을 불러오지 못했다({type(exc).__name__})"
    if not os.environ.get("TYBOT_BENCH_INDEX_DSN"):
        return [], (
            "TYBOT_BENCH_INDEX_DSN 이 없어 건너뛴다 — 운영 DB 를 쓰지 않기로 했으므로"
            "(§8) 실측 전용 DSN 을 주어야 잰다"
        )
    if not hasattr(search_index, "rebuild"):
        return [], "search_index.rebuild 이 없다 — 이 항목은 서버에서 잰다"

    sample = Samples(f"M4 증분 재색인 · 구조{layout}")
    store = ArchiveStore(root)
    for _ in range(rounds):
        with timed(sample):
            search_index.rebuild(store)
    return [sample], ""


# ---------------------------------------------------------------------------
# M5 — 용량·파일 수
# ---------------------------------------------------------------------------

def measure_size(root: Path) -> dict:
    files = [p for p in root.rglob("*") if p.is_file()]
    raw = [p for p in files if p.suffix == ".md"]
    return {
        "files": len(files),
        "raw_files": len(raw),
        "bytes": sum(p.stat().st_size for p in files),
        "raw_bytes": sum(p.stat().st_size for p in raw),
        "dirs": sum(1 for p in root.rglob("*") if p.is_dir()),
    }


# ---------------------------------------------------------------------------
# 보고
# ---------------------------------------------------------------------------

def render(result: dict) -> str:
    lines = [
        "# 저장 구조 비용 실측 — 원자료",
        "",
        f"> 잰 때: {result['measured_at']}",
        f"> 회차: {result['rounds']} (준비 {WARMUP}회 버림)",
        "",
        "**이 숫자는 구조를 고르는 근거가 아니다**(설계 §6). 고칠 수 있는 약점은",
        "「구조 1 을 고르면 먼저 무엇을 고쳐야 하는가」 의 목록으로 쓴다.",
        "",
        "## M5 용량·파일 수",
        "",
        "| | 파일 | 원문 파일 | 디렉터리 | 크기 |",
        "|---|---|---|---|---|",
    ]
    for layout in LAYOUTS:
        size = result["size"][layout]
        lines.append(
            f"| 구조 {'1' if layout == 'a' else '2'} | {size['files']} | "
            f"{size['raw_files']} | {size['dirs']} | {size['bytes'] / 1024:.0f} KB |"
        )
    lines += ["", "## M1~M4 시간 (ms)", "",
              "| 항목 | 중위값 | p90 | 편차 | 회차 |", "|---|---|---|---|---|"]
    for row in result["samples"]:
        lines.append(
            f"| {row['label']} | {row['median_ms']:.3f} | {row['p90_ms']:.3f} | "
            f"{row['stdev_ms']:.3f} | {row['rounds']} |"
        )
    if result["skipped"]:
        lines += ["", "## 재지 못한 것", ""]
        lines += [f"- {item}" for item in result["skipped"]]
    lines += [
        "",
        "## 읽을 때 주의",
        "",
        "- **cold 는 반쪽이다.** `ArchiveStore` 파싱 캐시만 비었고 OS 페이지 캐시는",
        "  그대로다. 구조 1 의 cold 이점이 실제보다 작게 보인다",
        "- **편차가 두 구조의 차이보다 크면** 그 비교는 아무 말도 못 한다. 그런 항목은",
        "  결론에 쓰지 않는다",
        "- 구조 1 수집은 이 스크립트 안의 구현이다. `writer` 의 검사·형식 함수를 그대로",
        "  쓰지만 **append writer 가 아니다** — 파일을 통째로 다시 쓴다",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="두 저장 구조의 우리 쪽 비용을 잰다. 설명은 이 파일 머리말.",
    )
    parser.add_argument("--a", required=True, help="구조 1 배치")
    parser.add_argument("--b", required=True, help="구조 2 배치")
    parser.add_argument("--out", required=True, help="보고서를 쓸 경로")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--work", default="", help="작업 사본 경로(기본: --out 아래)")
    _utf8_console()
    args = parser.parse_args()

    roots = {"a": Path(args.a).expanduser(), "b": Path(args.b).expanduser()}
    out_dir = Path(args.out).expanduser()
    for path, what in ((roots["a"], "구조 1"), (roots["b"], "구조 2"), (out_dir, "출력")):
        refusal = _refuse_operational(path, what)
        if refusal:
            print(refusal)
            print(f"  운영 경로에서 재지 않습니다: {', '.join(PROTECTED)}")
            return 2
    for layout, path in roots.items():
        if not path.is_dir():
            print(f"배치 {layout} 가 없습니다: {path}")
            return 2

    # **원본에 쓰지 않는다.** 수집을 재려면 써야 하고, 쓰면 그 배치는 더 이상
    # 동등성 시험을 통과한 그 배치가 아니다.
    work_root = Path(args.work).expanduser() if args.work else out_dir / "work"
    if work_root.exists():
        shutil.rmtree(work_root)
    work = {layout: work_root / layout for layout in LAYOUTS}
    for layout, path in roots.items():
        shutil.copytree(path, work[layout])

    store_a = ArchiveStore(work["a"])
    targets = pick_targets(store_a, work["a"])
    if not targets:
        print(f"채널을 하나도 읽지 못했습니다: {work['a']}")
        return 1
    queries = pick_queries(store_a)
    print(f"대상 채널 {len(targets)}개, 검색어 {len(queries)}개: {', '.join(queries)}")

    samples: list[Samples] = []
    skipped: list[str] = []

    samples += bench_ingest(work, targets, args.rounds)
    biggest = max(targets, key=lambda t: t.lines)
    for layout in LAYOUTS:
        samples += bench_search_cold(work[layout], queries, args.rounds, layout)
        samples += bench_search_warm(work[layout], biggest, queries, args.rounds, layout)
        got, why = bench_reindex(work[layout], layout, max(1, args.rounds // 10))
        samples += got
        if why:
            skipped.append(f"M4 증분 재색인 · 구조{layout}: {why}")

    result = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rounds": args.rounds,
        "targets": [
            {"channel": t.channel, "lines": t.lines, "id": t.channel_id} for t in targets
        ],
        "queries": queries,
        "size": {layout: measure_size(work[layout]) for layout in LAYOUTS},
        "samples": [s.as_json() for s in samples],
        "skipped": skipped,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (out_dir / f"bench-{stamp}.json").write_bytes(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    )
    report = render(result)
    (out_dir / f"bench-{stamp}.md").write_bytes(report.encode("utf-8"))
    print(report)
    print(f"→ {out_dir}/bench-{stamp}.md")
    if skipped:
        # 못 잰 것이 있으면 **0 으로 끝내지 않는다.** 보고서만 보고 「다 쟀다」 로
        # 읽는 일을 막는다.
        print(f"\n재지 못한 항목 {len(skipped)}개가 있습니다. 보고서 「재지 못한 것」 참조.")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
