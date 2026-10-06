"""이미 끝낸 검토를 TYBot DM 에서 생략할지 — **좌표와 해시로** 판정한다.

설계: `docs/design/hermes-write-entrypoints.md` §6

## 무엇을 판정하나

Hermes 쪽 스킬(`archive-inbox`·`archive-run`)에서 사람이 이미 「반영」 하거나 「뺌」
으로 끝낸 항목을, TYBot 검토 DM 에서 다시 묻지 않는 것이 목적이다.

## 생략의 근거는 「실행했다」 가 아니다

로컬 스킬이 돌았는지로 판정하면 안 된다. 스킬이 돌았다는 것은 **어떤 항목**을
**어떤 원문으로** 끝냈는지 아무것도 말해 주지 않는다. 같은 채널에서 다른 줄이
어긋났을 수도 있고, 끝낸 뒤에 원문이 바뀌었을 수도 있다. 그때 생략하면 사람은
**묻지도 않은 채 넘어간 사실**을 알 방법이 없다.

그래서 보는 것은 둘이다 — **같은 원문 좌표**인가, **그 좌표의 해시가 그대로**인가.
그리고 그 위에 **확정된 결정**(승인·거절)만 근거로 쓴다.

## 막는 쪽이 기본값

아래 중 하나라도 걸리면 **생략하지 않는다.** 묻는 쪽으로 틀리면 사람이 한 번 더
볼 뿐이지만, 생략하는 쪽으로 틀리면 **아무도 모른다.**

| 사유 | 왜 생략하지 않나 |
|---|---|
| `records_unreadable` | 기록을 못 읽었다. 없는 것과 못 읽은 것은 다르다 |
| `no_match` | 그 좌표에 대한 결정이 없다 |
| `not_final` | 보류(`deferred`)다. 아직 끝난 것이 아니다 |
| `source_mismatch` | 워크스페이스·채널이 다르다. 같은 파일명이어도 다른 자료다 |
| `evidence_changed` | 결정 이후 원문이 바뀌었다. 사람이 본 것과 지금 것이 다르다 |
| `no_coordinate` | 기록에 좌표·해시가 없다 — 대조할 수가 없다 |

마지막 항목이 지금 현실이다. Hermes 의 결정 기록(`.sync-state.json` 의 `applied`·
`dismissed`)은 **항목 id 와 요약 섹션 해시**만 담는다. 원문 줄 좌표도, 그 줄의
해시도 없다. 그래서 이 모듈을 붙여도 **당분간은 아무것도 생략되지 않는다.**

그건 결함이 아니라 설계다. 생략하려면 Hermes 기록이 §요구 형식을 채워야 하고,
채우기 전까지는 묻는 쪽으로 틀린다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("tybot.summary_review_reconcile")

# 확정된 결정만 생략 근거가 된다. 보류는 끝난 것이 아니다.
FINAL_STATES = frozenset({"approved", "rejected"})
# 기록에서 그대로 쓰는 상태 이름. Hermes 의 말과 우리 말을 한 자리에서 잇는다.
STATE_ALIASES = {
    "applied": "approved",    # 반영
    "approved": "approved",
    "dismissed": "rejected",  # 뺌
    "rejected": "rejected",
    "deferred": "deferred",   # 나중에 — 확정이 아니다
}

SKIP = "skip"
RECORDS_UNREADABLE = "records_unreadable"
NO_MATCH = "no_match"
NOT_FINAL = "not_final"
SOURCE_MISMATCH = "source_mismatch"
EVIDENCE_CHANGED = "evidence_changed"
NO_COORDINATE = "no_coordinate"


@dataclass(frozen=True)
class ExternalDecision:
    """다른 도구에서 끝난 결정 하나.

    **좌표와 해시가 없으면 쓸 수 없다.** 그 둘이 「같은 것을 봤다」 는 유일한 증거다.
    """

    workspace: str
    channel_id: str
    evidence_locator: str
    evidence_hash: str
    state: str
    decided_at: str = ""
    decided_by: str = ""
    source: str = ""

    @property
    def usable(self) -> bool:
        return bool(
            self.workspace
            and self.channel_id
            and self.evidence_locator
            and self.evidence_hash
            and self.state in FINAL_STATES
        )


def _row(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def parse_decisions(payload: Any, *, source: str = "") -> list[ExternalDecision]:
    """기록 한 덩이 → 결정 목록. **모양이 틀린 줄은 버린다.**

    예외를 올리지 않는다 — 한 줄이 깨졌다고 전체를 못 읽는 것으로 만들면, 그
    순간 「못 읽음」 이 되어 생략이 아예 멈춘다. 멈추는 방향은 안전하지만, 왜
    멈췄는지가 한 줄 때문이라는 사실은 드러나야 한다.
    """
    rows = payload.get("decisions") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    out: list[ExternalDecision] = []
    for raw in rows:
        row = _row(raw)
        state = STATE_ALIASES.get(str(row.get("state") or "").strip().lower(), "")
        if not state:
            continue
        out.append(
            ExternalDecision(
                workspace=str(row.get("workspace") or ""),
                channel_id=str(row.get("channel_id") or ""),
                evidence_locator=str(row.get("evidence_locator") or ""),
                evidence_hash=str(row.get("evidence_hash") or ""),
                state=state,
                decided_at=str(row.get("decided_at") or ""),
                decided_by=str(row.get("decided_by") or ""),
                source=source or str(row.get("source") or ""),
            )
        )
    return out


def load_decisions(path: Path | str) -> list[ExternalDecision] | None:
    """기록 파일을 읽는다. **못 읽으면 `None`** — 빈 목록과 구별해야 한다.

    빈 목록은 「끝낸 것이 없다」, `None` 은 「확인하지 못했다」 다. 둘을 섞으면
    파일이 사라진 날 모든 후보가 조용히 생략된다.
    """
    file = Path(path)
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # 없는 것은 「끝낸 것이 없다」 다. 그건 읽은 것이다.
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("외부 검토 기록을 읽지 못했습니다 %s: %s", file, exc)
        return None
    return parse_decisions(payload, source=file.name)


def _candidate_field(candidate: Any, name: str) -> str:
    if isinstance(candidate, dict):
        return str(candidate.get(name) or "")
    return str(getattr(candidate, name, "") or "")


def decide(candidate: Any, decisions: list[ExternalDecision] | None) -> tuple[bool, str]:
    """이 후보를 DM 에서 생략해도 되는가. `(생략할까, 사유)`.

    사유는 **생략할 때도** 돌려준다. 로그에 「왜 안 물었는지」 가 남아야 나중에
    사람이 「왜 그 건은 못 봤지」 를 추적할 수 있다.
    """
    if decisions is None:
        return False, RECORDS_UNREADABLE

    workspace = _candidate_field(candidate, "workspace")
    channel_id = _candidate_field(candidate, "channel_id")
    locator = _candidate_field(candidate, "evidence_locator")
    digest = _candidate_field(candidate, "evidence_hash")

    # 우리 쪽 후보에 좌표·해시가 없으면 대조 자체가 불가능하다.
    if not locator or not digest:
        return False, NO_COORDINATE

    matched = [d for d in decisions if d.evidence_locator == locator]
    if not matched:
        return False, NO_MATCH

    # 좌표가 같아도 **출처가 다르면 다른 자료다.** 날짜 파일명은 채널마다 겹친다.
    same_source = [
        d for d in matched if d.workspace == workspace and d.channel_id == channel_id
    ]
    if not same_source:
        return False, SOURCE_MISMATCH

    # 좌표·출처가 같은데 해시가 다르면 **결정 이후 원문이 바뀐 것**이다.
    same_evidence = [d for d in same_source if d.evidence_hash == digest]
    if not same_evidence:
        return False, EVIDENCE_CHANGED

    # 남은 것 중 확정된 결정이 하나라도 있어야 한다. 보류만 있으면 끝난 것이 아니다.
    final = [d for d in same_evidence if d.state in FINAL_STATES and d.usable]
    if not final:
        return False, NOT_FINAL

    return True, SKIP


def filter_candidates(
    rows: list[Any], decisions: list[ExternalDecision] | None
) -> tuple[list[Any], dict[str, int]]:
    """DM 에 실을 후보만 남긴다. 함께 돌려주는 것은 **사유별 건수**다.

    건수를 안 남기면 「오늘 후보가 없다」 와 「전부 생략됐다」 가 화면에서 같아
    보인다. 둘은 사람이 할 일이 완전히 다르다.
    """
    kept: list[Any] = []
    reasons: dict[str, int] = {}
    for row in rows:
        skip, reason = decide(row, decisions)
        reasons[reason] = reasons.get(reason, 0) + 1
        if not skip:
            kept.append(row)
    if reasons.get(SKIP):
        logger.info(
            "외부 검토로 생략 %d건 · 남긴 사유 %s",
            reasons[SKIP],
            ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()) if k != SKIP) or "-",
        )
    return kept, reasons


__all__ = [
    "EVIDENCE_CHANGED",
    "EXPORTED_STATES",
    "EXPORT_SCHEMA",
    "FINAL_STATES",
    "NOT_FINAL",
    "NO_COORDINATE",
    "NO_MATCH",
    "RECORDS_UNREADABLE",
    "SKIP",
    "SOURCE_MISMATCH",
    "SOURCE_TYBOT_DM",
    "ExternalDecision",
    "build_export",
    "decide",
    "filter_candidates",
    "load_decisions",
    "parse_decisions",
    "to_record",
    "write_export",
]


# --- 내보내기 ----------------------------------------------------------------
#
# 읽는 쪽만 있으면 대조는 늘 「끝낸 것이 없다」 로 끝난다. 쓰는 쪽이 있어야
# 승인 인터페이스를 갈아 끼워도(TYBot DM ↔ 로컬 스킬) 서로의 결정을 본다.
#
# ## 본문을 싣지 않는다
#
# 처음 설계에는 `proposed_text` 가 있었다. 뺐다 — 이 파일은 **다른 쪽이 읽는 것**이고,
# 사내 요약 문장이 PF 로, PF 문장이 사내로 건너가면 그건 크로스 워크스페이스 노출이다
# (원칙 4). 대조에 필요한 것은 좌표·해시·상태뿐이고, 본문은 각자 자기 쪽에서 본다.
#
# 같은 이유로 `evidence_quote` 도 없다. 해시가 「같은 줄인가」 를 말해 주므로 인용문을
# 옮길 이유가 없다.
EXPORT_SCHEMA = "summary-review-decisions/v1"
SOURCE_TYBOT_DM = "tybot-dm"

# 내보낼 상태. **확정과 보류만** 보낸다.
#
# `pending` 은 아직 아무 결정이 아니고, `expired`(미응답 폐기)·`superseded`(대체됨)는
# 사람이 내린 판단이 아니다. 그것들을 보내면 받는 쪽이 「끝났다」 로 읽을 수 있다 —
# 읽는 쪽이 조심하는 것보다 **애초에 안 보내는 쪽**이 안전하다.
EXPORTED_STATES = frozenset({"approved", "rejected", "deferred"})


def to_record(row: Any, *, source: str = SOURCE_TYBOT_DM, generation: int = 1) -> dict | None:
    """후보 한 건 → 내보낼 기록. 좌표가 없으면 `None`.

    좌표 없는 기록은 받는 쪽에서 `no_coordinate` 로 떨어질 뿐이라, 보내지 않는 편이
    파일도 작고 읽는 쪽도 헷갈리지 않는다.
    """
    state = STATE_ALIASES.get(str(_candidate_field(row, "state")).strip().lower(), "")
    if state not in EXPORTED_STATES:
        return None
    locator = _candidate_field(row, "evidence_locator")
    digest = _candidate_field(row, "evidence_hash")
    if not locator or not digest:
        return None
    return {
        "candidate_id": _candidate_field(row, "id") or _candidate_field(row, "candidate_id"),
        "workspace": _candidate_field(row, "workspace"),
        "channel_id": _candidate_field(row, "channel_id"),
        "evidence_locator": locator,
        "evidence_hash": digest,
        "evidence_message_ts": _candidate_field(row, "evidence_message_ts"),
        "kind": _candidate_field(row, "kind"),
        "state": state,
        "generation": int(generation),
        "decided_at": _candidate_field(row, "decided_at"),
        "decided_by": _candidate_field(row, "decided_by"),
        "source": source,
    }


def build_export(rows, *, source: str = SOURCE_TYBOT_DM) -> dict:
    """후보 목록 → 내보낼 덩이. 좌표 없는 건은 조용히 빠진다."""
    out = [r for r in (to_record(row, source=source) for row in rows or ()) if r]
    return {"schema": EXPORT_SCHEMA, "source": source, "decisions": out}


def write_export(path: Path | str, rows, *, source: str = SOURCE_TYBOT_DM) -> int:
    """원자적으로 쓴다. 읽는 쪽이 **반쯤 쓰인 파일**을 보면 안 된다 —
    그때 JSON 파싱이 깨지고, 깨짐은 「못 읽음」 이라 대조가 통째로 멈춘다.
    """
    payload = build_export(rows, source=source)
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(file.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(file)
    logger.info("검토 결정 %d건을 %s 로 내보냈습니다", len(payload["decisions"]), file)
    return len(payload["decisions"])
