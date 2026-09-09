#!/usr/bin/env python3
"""전문가 답변을 마스터 답변과 대조한다 (B-27b/B-28).

    python scripts/measure_specialist.py --days 14 --limit 20
    python scripts/measure_specialist.py --days 14 --limit 20 --yes   # 실제 호출

## 무엇을 재는가 — 그리고 무엇을 안 재는가

**"어느 답이 더 좋은가" 는 재지 않는다.** 기계가 판정할 수 없고, 판정한 척하면
그 숫자를 보고 잘못된 결정을 하게 된다. 대신 **확인할 수 있는 것**만 본다.

| 재는 것 | 왜 |
|---|---|
| **근거에 없는 숫자** | 값은 원문에서 그대로 옮겨야 한다. 지어낸 금액·날짜가 가장 위험하다 |
| 출처가 붙었는가 | 원칙 2. 전문가가 답해도 우리가 붙여야 한다 |
| 근거 건수가 같은가 | 검색은 공유하므로 같아야 한다. 다르면 재생 범위가 좁았다는 뜻 |
| 어느 전문가로 갔나 · 신뢰도 | 라우팅이 실제 질문에서 맞는지 |
| 시간 · 비용 · 길이 | 싼 모델로 충분한지 |

## 이 검사가 못 잡는 것

값의 **존재만** 본다. 문맥은 보지 않는다. 그래서 「3억 2천만원」 을 「약 3억」 으로
줄여 적으면 잡히지 않는다 — `3억` 이 근거에 실제로 있기 때문이다.

잡으려면 「이 값이 저 값의 일부인가」 를 판정해야 하고, 그건 기계가 틀리기 쉽다.
잘못 잡으면 멀쩡한 답이 「지어냈다」 로 보고되고, 그 오탐이 잦으면 사람이 이
보고서를 안 본다. **못 잡는 쪽을 골랐다.** 환산(`320,000,000원`)은 잡힌다.

## 질문은 지어내지 않는다

`qa-log` 의 **실제 질문**을 다시 돌린다. 지어낸 질문으로 만든 골든셋은 우리 상상을
측정한다. 그래서 실사용이 없으면 이 스크립트는 아무것도 할 수 없고, 그게 맞다.

## 권한을 넓히지 않는다

`qa-log` 의 `scope` 는 「채널 N개」 같은 요약이라 **원래 권한을 복원할 수 없다.**
그래서 그때 실제로 인용된 워크스페이스·채널 쌍만 별도 읽기 뷰에 넣는다. 원래 범위의
부분집합이므로 넓어지는 일이 없다. 대신 근거가 그때보다 줄 수 있고 그 차이를 함께 본다.

## 돈이 든다

질문 하나에 모델을 **최소 세 번** 부른다(라우팅 + 전문가 + 마스터).
`--yes` 없이는 견적만 내고 멈춘다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive.store import ArchiveStore
from tybot.envfile import load_env_file
from tybot.gateway.router import Router
from tybot.paths import archive_dir

# 값으로 볼 토큰. 단위가 붙은 숫자와 두 자리 이상 숫자만 본다 —
# 한 자리 숫자는 문장에 흔해서 잡으면 잡음만 는다.
VALUE_RE = re.compile(
    r"[0-9][0-9,\.]*\s*(?:억|천만|만원|만|원|%|퍼센트|개|건|일|월|년|평|㎡|㎥)"
    r"|[0-9][0-9,\.]{1,}"
)

# 질문 하나에 드는 모델 호출 수(라우팅 + 전문가 + 마스터).
CALLS_PER_QUESTION = 3


def normalize(value: str) -> str:
    """비교용. 쉼표·공백을 지운다 — 모델이 `1,000억` 을 `1000억` 으로 적는다."""
    return re.sub(r"[\s,]", "", value)


def values_in(text: str) -> set[str]:
    return {normalize(m.group(0)) for m in VALUE_RE.finditer(text or "")}


def unsupported_values(answer: str, evidence: str) -> list[str]:
    """답변에 있는데 근거에 없는 값. **이것이 이 스크립트의 핵심 신호다.**

    지어낸 금액·날짜가 답변에 들어가고 거기에 우리 출처가 붙는 것이 가장 위험하다.
    반올림·환산도 여기 걸린다 — 그것도 원문이 아니다.
    """
    supported = values_in(evidence)
    return sorted(v for v in values_in(answer) if v not in supported)


@dataclass
class Row:
    question: str
    workspace: str
    channels: list[str]
    recorded_hits: int
    scope: dict[str, list[str]] = field(default_factory=dict)
    hits: int = 0
    master_text: str = ""
    special_text: str = ""
    specialist: str = ""
    confidence: float | None = None
    routing_reason: str = ""
    master_ms: int = 0
    special_ms: int = 0
    master_cost: float = 0.0
    special_cost: float = 0.0
    master_unsupported: list[str] = field(default_factory=list)
    special_unsupported: list[str] = field(default_factory=list)
    master_has_source: bool = False
    special_has_source: bool = False
    note: str = ""

    @property
    def question_id(self) -> str:
        value = f"{self.workspace}\0{self.question}".encode()
        return hashlib.sha256(value).hexdigest()[:16]


def load_questions(days: int, limit: int) -> list[dict]:
    """`qa-log` 에서 다시 돌릴 질문. 근거를 찾은 것만 고른다.

    근거 0건이던 질문은 전문가에게도 묻지 않으므로(그게 설계다) 비교할 것이 없다.
    """
    from tybot.console import reader

    records = reader._read_qa_records(days)
    out = []
    seen: set[tuple[str, str]] = set()
    for rec in records:
        question = str(rec.get("question") or "").strip()
        citations = list(rec.get("citations") or [])
        key = (str(rec.get("workspace") or ""), question)
        if not question or key in seen:
            continue
        if str(rec.get("reason")) != "answered" or int(rec.get("hits") or 0) <= 0:
            continue
        if not citations:
            # 인용이 없으면 재생 범위를 만들 수 없다. **짐작하지 않는다** —
            # 범위를 넓게 잡으면 그 사람이 볼 수 없던 자료로 답을 만든다.
            continue
        seen.add(key)
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def channels_from(citations: list[str]) -> list[str]:
    """인용 문자열에서 채널명만. `[ws] #채널, 📄문서(날짜)` 모양이다."""
    out: list[str] = []
    for text in citations:
        head = str(text).split(",")[0].strip()
        head = re.sub(r"^\[[^\]]*\]\s*", "", head)
        if head.startswith("#") and head not in out:
            out.append(head)
    return out


def scope_from(citations: list[str], default_workspace: str) -> dict[str, list[str]]:
    """Build the exact workspace/channel allowlist represented by citations."""
    out: dict[str, list[str]] = {}
    for text in citations:
        head = str(text).split(",")[0].strip()
        match = re.match(r"^\[([^\]]+)\]\s*(.*)$", head)
        workspace = match.group(1).strip() if match else default_workspace
        channel = match.group(2).strip() if match else head
        if channel.startswith("#") and channel not in out.setdefault(workspace, []):
            out[workspace].append(channel)
    return out


class CitationScopedStore:
    """Archive view restricted to the channels proven by recorded citations."""

    def __init__(self, store: ArchiveStore, scope: dict[str, list[str]]) -> None:
        self._store = store
        self._scope = {
            (workspace, channel)
            for workspace, channels in scope.items()
            for channel in channels
        }
        self.root = store.root

    def visible_docs(self, _ctx: RequestContext):
        return [
            doc
            for doc in self._store.docs()
            if (doc.workspace, doc.channel) in self._scope
        ]

    def titles(self, ctx: RequestContext) -> list[str]:
        return [doc.title for doc in self.visible_docs(ctx)]

    def search(self, query: str, ctx: RequestContext, *, limit: int = 20):
        from tybot import search_index

        tokens = search_index.tokens_of(query)
        if not tokens:
            return []
        return self._store._scan(query, tokens, self.visible_docs(ctx), limit)


class Probe:
    """측정용 전문가 훅. 판정을 붙잡아 두어 보고에 쓴다.

    운영 훅(`slack/pilot.specialist_hook`)을 쓰지 않는 이유는 하나다 —
    그쪽은 `specialist_call` 에 기록한다. **재생한 질문이 운영 통계에 섞이면**
    「전문가가 실제로 몇 번 답했나」 가 부풀고 그 표를 믿을 수 없게 된다.
    """

    def __init__(self, router) -> None:
        self._router = router
        self.last_specialist = ""
        self.last_confidence: float | None = None
        self.last_reason = ""

    def __call__(self, question, ctx, evidence):
        from tybot import specialist_router as sr

        self.last_specialist = ""
        self.last_confidence = None
        self.last_reason = ""

        decision = sr.route(question, ctx.workspace, self._router)
        self.last_confidence = decision.confidence
        self.last_reason = decision.reason
        if decision.went_to_master:
            return None
        self.last_specialist = decision.specialist.key
        return sr.ask(
            decision,
            question=question,
            workspace=ctx.workspace,
            evidence=[evidence],
            router=self._router,
            fallback=lambda: "",
            authorization_id=f"measure:{ctx.workspace}",
            record_call_row=False,
        )


def run(rows: list[Row], store, router, hook) -> None:
    for row in rows:
        scoped_store = CitationScopedStore(store, row.scope)
        master = AnswerEngine(scoped_store, router)
        special = AnswerEngine(scoped_store, router, specialist=hook)
        ctx = RequestContext(
            workspace=row.workspace, channels=frozenset(row.channels)
        )
        started = time.monotonic()
        try:
            answer = master.answer(row.question, ctx)
        except Exception as exc:  # noqa: BLE001 - 한 건 실패가 측정을 멈추면 안 된다
            row.note = f"마스터 실패: {type(exc).__name__}"
            continue
        row.master_ms = int((time.monotonic() - started) * 1000)
        row.hits = answer.hit_count
        row.master_text = answer.text
        row.master_cost = answer.cost_usd
        row.master_has_source = "출처:" in answer.to_slack()

        evidence = _evidence_for(store, row.question, ctx)
        row.master_unsupported = unsupported_values(answer.text, evidence)

        started = time.monotonic()
        try:
            answer2 = special.answer(row.question, ctx)
        except Exception as exc:  # noqa: BLE001
            row.note = (row.note + f" 전문가 실패: {type(exc).__name__}").strip()
            continue
        row.special_ms = int((time.monotonic() - started) * 1000)
        row.special_text = answer2.text
        row.special_cost = answer2.cost_usd
        row.special_has_source = "출처:" in answer2.to_slack()
        row.special_unsupported = unsupported_values(answer2.text, evidence)
        # `getattr` 기본값을 쓰지 않는다. 훅은 이 스크립트가 만든 객체이고,
        # 속성 이름이 바뀌면 **조용히 「전문가 미호출」 로 보고**된다 — 측정이
        # 거짓이 되는 것이 터지는 것보다 나쁘다.
        row.specialist = hook.last_specialist
        row.confidence = hook.last_confidence
        row.routing_reason = hook.last_reason
        if not row.specialist:
            # 라우터가 마스터로 접었다. 답이 같은지로 판정하지 않는다 —
            # 우연히 같은 문장이 나올 수도 있고, 그러면 판정이 뒤집힌다.
            row.note = (row.note + " 전문가 미호출").strip()


def _evidence_for(store, question: str, ctx: RequestContext) -> str:
    """근거 블록. 값 대조의 기준이므로 답변 경로와 **같은 검색**을 쓴다."""
    from tybot.answer import _evidence_block

    return _evidence_block(store.search(question, ctx))


def report(rows: list[Row]) -> dict:
    done = [r for r in rows if r.master_text and r.special_text]
    routed = [r for r in done if "전문가 미호출" not in r.note]
    summary = {
        "questions": len(rows),
        "compared": len(done),
        "routedToSpecialist": len(routed),
        "masterUnsupported": sum(1 for r in done if r.master_unsupported),
        "specialistUnsupported": sum(1 for r in routed if r.special_unsupported),
        "masterMissingSource": sum(1 for r in done if not r.master_has_source),
        "specialistMissingSource": sum(1 for r in routed if not r.special_has_source),
        "masterAvgMs": _avg(r.master_ms for r in done),
        "specialistAvgMs": _avg(r.special_ms for r in routed),
        "masterCost": round(sum(r.master_cost for r in done), 4),
        "specialistCost": round(sum(r.special_cost for r in routed), 4),
    }
    return summary


def _avg(values) -> int:
    items = list(values)
    return int(sum(items) / len(items)) if items else 0


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=14, help="qa-log 조회 기간(기본 14일)")
    ap.add_argument("--limit", type=int, default=20, help="질문 수 상한(기본 20)")
    ap.add_argument("--out", default="", help="결과 JSON 경로(선택)")
    ap.add_argument(
        "--yes", action="store_true", help="실제로 모델을 호출한다(없으면 견적만)"
    )
    args = ap.parse_args(argv)

    records = load_questions(args.days, args.limit)
    if not records:
        print(
            f"최근 {args.days}일 qa-log 에 다시 돌릴 질문이 없습니다.\n"
            "근거를 찾아 답한 질문(인용이 있는 것)만 고릅니다 — "
            "실사용이 쌓이기 전에는 비교할 것이 없습니다."
        )
        return 0

    rows = [
        Row(
            question=str(rec["question"]),
            workspace=str(rec.get("workspace") or ""),
            channels=channels_from(list(rec.get("citations") or [])),
            recorded_hits=int(rec.get("hits") or 0),
            scope=scope_from(
                list(rec.get("citations") or []), str(rec.get("workspace") or "")
            ),
        )
        for rec in records
    ]
    rows = [r for r in rows if r.workspace and r.channels]

    print(f"질문 {len(rows)}건 · 모델 호출 약 {len(rows) * CALLS_PER_QUESTION}회")
    if not args.yes:
        print(
            "\n견적만 냈습니다. 실제로 돌리려면 `--yes` 를 붙이세요.\n"
            "질문 하나에 라우팅·전문가·마스터로 최소 3번 부릅니다 — 실제 비용이 듭니다."
        )
        for row in rows[:10]:
            print(f"  [{row.workspace}] {row.question_id} · 채널 {len(row.channels)}개")
        return 0

    router = Router.from_default_registry(
        daily_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
        default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
    )
    store = ArchiveStore(archive_dir())

    probe = Probe(router)
    run(rows, store, router, probe)

    summary = report(rows)
    print("\n=== 요약")
    for key, value in summary.items():
        print(f"  {key}: {value}")

    diff = [r for r in rows if r.hits != r.recorded_hits]
    if diff:
        print(
            f"\n근거 건수가 그때와 다른 질문 {len(diff)}건 — 재생 범위가 인용된 채널로"
            "만 좁혀졌기 때문입니다(권한을 넓히지 않습니다)."
        )

    bad = [r for r in rows if r.special_unsupported]
    if bad:
        print("\n=== 근거에 없는 값이 전문가 답변에 들어간 건")
        for row in bad:
            print(f"  {row.question_id} → {row.special_unsupported}")

    if args.out:
        payload = {
            "summary": summary,
            "rows": [
                {
                    "questionId": r.question_id,
                    "workspace": r.workspace,
                    "hits": r.hits,
                    "recordedHits": r.recorded_hits,
                    "specialist": r.specialist,
                    "confidence": r.confidence,
                    "routingReason": r.routing_reason,
                    "masterUnsupported": r.master_unsupported,
                    "specialistUnsupported": r.special_unsupported,
                    "masterMs": r.master_ms,
                    "specialistMs": r.special_ms,
                    "note": r.note,
                }
                for r in rows
            ],
        }
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n결과를 적었습니다: {args.out}")
        print("**질문·답변 본문은 담지 않습니다** — 업무 내용이 파일로 새지 않게.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
