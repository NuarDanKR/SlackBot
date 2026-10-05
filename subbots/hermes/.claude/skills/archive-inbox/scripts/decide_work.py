#!/usr/bin/env python3
"""
07:00 목록의 항목을 **건별로** 정한다 — 반영 / 빼 / 나중에.

사용:
  python decide_work.py --only 1,2 --apply                      # 반영하기로 정함
  python decide_work.py --only 3 --drop --reason "원문과 다름"    # 뺀다
  python decide_work.py --only 4 --later 14 --reason "판정 보류"  # 나중에
  python decide_work.py --clear 영업                            # 보류 취소 (번호 아님 — id 조각)
  python decide_work.py --show                                  # 편집을 끝낸 뒤 (보이고 찍는다)

종료코드: 0 성공 / 1 실패·못 맞춘 토큰

── 지키는 것 ──

  · **`--only` 를 빼면 거절한다.** 목록만 찍고 0 으로 끝나면 미룬 줄 알고 넘어가게 된다
    (수정·삭제 쪽이 2026-08-10 에 그래서 고쳐졌다).
  · **못 맞춘 토큰을 조용히 넘기지 않는다** — `apply_edits.py` 의 `select` 를 그대로 쓴다.
    오타 하나로 안 정해진 건이 생기면 그 건은 아무 표시 없이 남는다.
  · **「나중에」에는 만기가 붙는다.** 만기 없는 보류는 삭제와 같다
    (위키 `[[적어둔-일은-돌아오지-않는다]]`). 만기가 지나면 저절로 목록으로 돌아온다.
  · **「빼」에는 사유와 그때의 요약 해시가 함께 적힌다.** 요약이 나중에 고쳐지면 판정의
    전제가 사라지므로 후보로 돌아온다. 단 **그 회차에 사람 앞에 오지도 않았던 「빼」는
    `--show` 가 새 판으로 옮긴다** — 안 옮기면 같은 채널 요약을 다른 이유로 한 글자만
    고쳐도 되살아난다 (2026-09-03, 경계는 `carry_dismissals`). 옮긴 것은 화면에 적는다.
  · **이 스크립트는 md 를 고치지 않는다.** 요약 문장을 새로 쓰는 것은 판단이라 사람의
    일이다. 여기가 하는 것은 「정했다」를 기록하는 것뿐이다.
  · **도장은 「보여준 내용」이 만든다.** `--show` 가 바뀐 요약을 화면에 내고 그 내용에
    찍는다. 승인 기록(`applied`)은 도장과 무관하다 — 그걸로 찍던 때는 한 번 승인된
    채널이 영구히 열렸다 (2026-08-15).

── 기록이 두 자리로 나뉘는 이유 ──

  `.sync-state.json`     — **결정 셋 전부**(「반영·빼·나중에」). 저장소에 들어가 VM 도 읽는다(다음 07:00 에 거른다)
  `.decision-stamp.json` — 화면에 보인 요약의 해시(도장). **저장소에 안 들어간다**(.gitignore)

가르는 선은 「결정이냐 목격이냐」다. 결정은 다음 07:00 에 VM 이 걸러야 하므로 저장소로
가고, 도장은 「이 PC 에서 사람이 화면으로 봤다」는 사실이라 아카이브 자료가 아니다 —
저장소에 넣으면 VM·다른 PC 의 기록과 섞여 그 뜻이 사라진다 (doc-archive 의
`.review-stamp.json` 과 같은 이유).

**2026-09-07 전에는 「반영」만 뒤엣것에 있었다.** 그래도 굴러간 것은 `pending-work.js` 의
`mergeItems` 가 「대조한 회차에 다시 안 잡힌 요약 항목」을 치워 그 자리를 메웠기
때문인데, 그 전제가 틀린 것이었다 — 대조가 보는 원문은 그 회차의 새 메시지뿐이라
「다시 안 잡힘」은 「해소됨」이 아니라 「그 채널에 그 얘기가 안 왔음」이다. 자동 치움을
걷어내면서 「반영」을 결정 쪽으로 옮겼다.

`.pending-work.json` 에 적지 않는 이유는 그 파일을 다음 07:00 이 통째로 다시 쓰기 때문이다.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "slack-sync" / "scripts"))
sys.path.insert(0, str(_HERE.parents[1] / "_shared"))

import review_work as R  # noqa: E402
from apply_edits import select  # noqa: E402
import tz as _tz  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 기록의 자리·모양·git 호출은 **review_work 한 곳에 둔다.** 여기서 다시 정의하면
# 관문이 보는 기록과 여기가 쓰는 기록이 갈릴 수 있고, 갈리면 에러 없이 통과한다.
ROOT = R.ROOT
STATE = R.STATE
STAMP = R.STAMP
git = R.git
fingerprint = R.fingerprint
DEFAULT_LATER_DAYS = 14

DEFERRED_COMMENT = (
    "정하기를 미룬 07:00 할 일. `until` 이 만기이고, 만기가 지나면 저절로 목록으로 "
    "돌아와 09:00 위생 점검이 다시 알린다. 만기 없는 보류는 삭제와 같아서 필수로 뒀다."
)
DISMISSED_COMMENT = (
    "「빼」로 정한 07:00 할 일. `summaryHash` 는 정할 때의 그 채널 요약 자리 해시로, "
    "요약이 나중에 고쳐지면 판정의 전제가 사라지므로 후보로 돌아온다. 해시가 없는 것"
    "(요약이 아닌 항목)은 돌아올 근거가 없어 계속 빠진다. `carried_at` 이 붙은 것은 "
    "그 회차 목록에 안 떠 있던 「빼」를 `decide_work.py --show` 가 새 요약 판으로 옮긴 "
    "것이다 — 안 옮기면 같은 채널 요약을 다른 이유로 고칠 때마다 되살아난다."
)
APPLIED_COMMENT = (
    "「반영」으로 정한 07:00 할 일. `at` 이 정한 시각이고, 항목을 **마지막으로 다시 본 "
    "시각**(`.pending-work.json` 의 `lastSeen`)보다 뒤면 목록에서 뺀다 — 반영 뒤에 새 "
    "증거로 다시 잡힌 것은 되살아난다. 2026-09-07 전에는 이 기록이 로컬 전용 "
    "`.decision-stamp.json` 에만 있어 VM 이 못 봤다."
)


# ── 파일 ────────────────────────────────────────────────────────────────

def _read(p: Path, fallback):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write(p: Path, data) -> None:
    """원자적으로 쓴다 — 쓰는 도중 죽어도 상태 파일이 깨지지 않게."""
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def load_stamp() -> dict:
    """망가졌으면 빈 것으로 본다 — 기록이 없으면 막는 쪽이 안전한 방향이다.
    읽는 쪽(관문)과 같은 함수를 쓴다."""
    return R.load_stamp()


def now_iso() -> str:
    """결정 시각 기록. **기계 로컬 시간대로 재지 않는다 (2026-09-03 이전에는 그랬다).**

    `datetime.now().astimezone()` 은 이 스크립트를 돌리는 기계의 시스템 시간대를 쓴다 —
    문자열 자체는 오프셋이 붙어 있어 시각으로는 맞지만, 옆의 `until`(`R.today_kst()` 로
    KST 날짜만 계산)과 날짜만 비교하는 자리가 있으면 자정 언저리에서 하루 어긋난다.
    `_shared/tz.py` 로 통일한다 — config.json 의 timezone(Asia/Seoul)을 읽는다.
    """
    return _tz.now().isoformat(timespec="seconds")


# ── 작업 트리 ───────────────────────────────────────────────────────────

def changed_channel_mds() -> list:
    """작업 트리에서 이번에 고쳐진 채널 md.

    관문은 **스테이징**을 보고(`review_work.staged_channel_mds`) 여기는 **작업 트리**를
    본다 — 편집 직후 아직 `git add` 하기 전에 찍어야 하기 때문이다.
    (시험이 갈아끼울 수 있게 함수로 둔다.)"""
    out = []
    for line in git("status", "--porcelain", "-uall", "--", R.CHANNELS_REL).splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:                       # 이름 변경은 `R  옛 -> 새`
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if not path.endswith(".md"):
            continue
        md = ROOT / path
        if md.exists():
            out.append(md)
    return sorted(set(out))


# ── 결정 ────────────────────────────────────────────────────────────────

def approve(items: list) -> int:
    """「반영」으로 정했다는 기록. **md 는 여기서 안 고친다** — 문장은 사람이 쓴다.

    **기록은 `.sync-state.json` 이다** — 「빼·나중에」와 같은 자리이고, 저장소에 들어가
    VM 의 `suppress()` 도 읽는다. 2026-09-07 전에는 로컬 전용 `.decision-stamp.json` 에만
    적혔고, 그래도 굴러간 것은 `mergeItems` 의 자동 치움이 그 자리를 메웠기 때문이다 —
    그 전제(「대조 회차에 다시 안 잡힘 = 해소됨」)가 틀린 것이어서 걷어냈으므로, 안 옮기면
    반영한 항목이 VM 목록에서 영영 안 사라진다.

    도장(`--show` 가 찍는 `files`·`shown_at`)은 그대로 로컬이다 — 그건 「이 내용을 사람이
    화면에서 봤다」는 커밋 관문의 값이라 아카이브 자료가 아니다.

    **요약 계열에만 쓸 수 있다.** 「반영」은 「요약 문장을 고쳤다」는 뜻인데, 요약이 아닌 것
    (새 채널·개명·note·파생값)에는 고칠 요약 줄이 없어 그 말이 성립하지 않는다. 이 종류의
    종결 수단은 「빼」다 — `DISMISSED_COMMENT` 가 「해시가 없는 것(요약이 아닌 항목)은
    돌아올 근거가 없어 계속 빠진다」고 이미 정의하고 있다.
    """
    bad = [it for it in items if it.get("kind") not in R.SUMMARY_KINDS]
    if bad:
        print("! 「반영」은 요약 계열(요약 불일치·근거 못 찾아 뺀 후보)에만 씁니다.",
              file=sys.stderr)
        for it in bad:
            label = R.KIND_LABEL.get(it.get("kind"), it.get("kind"))
            print(f"    [{label}] {it.get('channel') or it.get('where') or ''}  {it.get('detail','')}".rstrip(),
                  file=sys.stderr)
        print("  이 종류는 고칠 요약 문장이 없어 「반영」으로 끝나지 않습니다 —"
              " 이 PC 목록에서만 사라지고 07:00 DM 은 계속 옵니다.", file=sys.stderr)
        print('  처리했으면 「빼」로 정하세요:  --drop --reason "…"'
              '  (저장소에 들어가 VM 도 다음 07:00 부터 거릅니다)', file=sys.stderr)
        print("  아무것도 하지 않았습니다.", file=sys.stderr)
        return 1

    state = _read_state_mut()
    if state is None:
        return 2
    applied = state.get("applied") or {}
    for it in items:
        md = R.md_path(it)
        applied[it["id"]] = {
            "file": md.relative_to(ROOT).as_posix(),
            "channel": it.get("channel", ""),
            "at": now_iso(),
        }
        print(f"  반영하기로: #{it.get('channel','')} {it.get('where','') or it.get('kind','')}")
    state["applied"] = applied
    state["_applied_comment"] = APPLIED_COMMENT
    _write(STATE, state)
    print("\n이제 채널 md 의 요약을 고치세요. 고친 뒤 `--show` 를 돌려야 커밋이 됩니다.")
    return 0


def _read_state_mut():
    """상태를 **고쳐 쓸 목적으로** 읽는다. 깨져 있으면 사유를 찍고 None — fail-closed.

    `_read(STATE, {})` 처럼 `{}` 로 물러서면 아래 `_write` 가 그 빈 값 위에 결정만 얹어
    파일을 통째로 다시 쓴다 — 한 번의 읽기 실패(대표: pull 충돌 표식)로 전 채널의
    동기화 기준점(`last_ts`)이 사라지고, 자동 반영이 영영 멈춘다. slack-sync 의
    `apply_edits.load_state` 가 2026-08-10 에 같은 사고를 겪고 fail-closed 로 바꿨는데
    이 파일이 같은 패턴을 다시 들여왔었다 (2026-09-04 리뷰).

    **없는 파일은 깨진 파일이 아니다** — 잃을 것이 없으니 빈 것으로 시작한다.
    읽기만 하는 자리(`carry_dismissals`·화면)는 `_read` 그대로다 — 못 읽어도
    덮어쓸 일이 없고, 빈 값으로 물러서는 쪽이 화면을 막지 않는다."""
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"! 상태 파일을 읽지 못해 멈춥니다 — {STATE}\n  {e}", file=sys.stderr)
        print("  이 파일에는 채널별 동기화 기준점이 들어 있어 빈 값으로 덮어쓰면 "
              "되돌릴 수 없습니다. 아무것도 하지 않았습니다.", file=sys.stderr)
        return None


def drop(items: list, reason: str) -> int:
    """「빼」 — 이 후보는 틀렸다. 그때의 요약 해시를 함께 남긴다."""
    state = _read_state_mut()
    if state is None:
        return 2
    dismissed = state.get("dismissed") or {}
    for it in items:
        # 요약 계열만 해시를 낸다. 나머지는 되돌아올 근거가 없다.
        h = None
        if it.get("kind") in R.SUMMARY_KINDS:
            h = R.summary_hash(it.get("file") or it.get("channel") or "")
        dismissed[it["id"]] = {"reason": reason, "at": now_iso(), "summaryHash": h}
        print(f"  뺌: #{it.get('channel','')} {it.get('where','') or it.get('kind','')} — {reason}")
        if it.get("kind") in R.SUMMARY_KINDS and not h:
            # 채널 md 가 없으면 해시가 빈 문자열로 나오고, suppress(JS)·_still_dismissed
            # (파이썬)는 「해시 없음 = 계속 뺌」으로 읽는다 — 요약이 바뀌어도 안 돌아온다.
            # 그 판정은 두 언어에 걸친 계약이라 여기서 안 바꾸고, 영구가 된다는 사실만
            # 그 자리에서 알린다 (2026-09-04).
            print("      요약 해시를 못 냈습니다(채널 md 없음) — 요약이 고쳐져도 이 「빼」는 "
                  "저절로 안 돌아오고 --clear 로만 풀립니다.")
    state["dismissed"] = dismissed
    state["_dismissed_comment"] = DISMISSED_COMMENT
    _write(STATE, state)
    return 0


def later(items: list, days: int, reason: str) -> int:
    """「나중에」 — 만기가 지나면 저절로 돌아온다."""
    until = str(R.today_kst() + timedelta(days=days))
    state = _read_state_mut()
    if state is None:
        return 2
    deferred = state.get("deferred") or {}
    for it in items:
        deferred[it["id"]] = {"until": until, "reason": reason, "at": now_iso()}
        print(f"  나중에({until}): #{it.get('channel','')} {it.get('where','') or it.get('kind','')}")
    state["deferred"] = deferred
    state["_deferred_comment"] = DEFERRED_COMMENT
    _write(STATE, state)
    return 0


def clear(tokens: list) -> int:
    """보류·뺌·반영 취소. 목록에서 사라진 뒤라 id 조각으로 고른다.

    **「반영」도 여기서 취소된다.** 반영한 항목은 목록에서 사라지므로, 이 문이 없으면
    되돌릴 길이 아예 없다 — 「빼」로 바꾸려 해도 목록이 비어 고를 수가 없다.
    2026-09-04 에 실물 2건이 그 상태였고 손으로 기록 파일을 고치는 것 말고는 나갈 문이
    없었다.

    **도장(`--show` 가 찍는 `files`·`shown_at`)은 안 건드린다** — 그건 「이 내용을 사람이
    화면에서 봤다」는 커밋 관문의 값이라 결정과 별개다 (2026-08-15).
    """
    state = _read_state_mut()
    if state is None:
        return 2
    toks = [t.strip() for raw in tokens for t in str(raw).split(",") if t.strip()]

    # **숫자만인 토큰은 거절한다.** 여기의 토큰은 화면 번호가 아니라 id 조각인데,
    # id 에는 16자리 16진수 해시가 들어 있어(예: `…|note|9f6b91b7a57e25c5`) 숫자
    # 한 자리가 무관한 기록에 부분 일치로 걸린다 — 번호인 줄 알고 4 를 넘기면
    # 남의 결정이 조용히 취소되고 0 으로 끝났다 (2026-09-04 리뷰).
    numeric = [t for t in toks if t.isdigit()]
    if numeric:
        print(f"! --clear 는 화면 번호가 아니라 id 조각을 받습니다: {', '.join(numeric)}",
              file=sys.stderr)
        print("  id 에는 16자리 해시가 들어 있어 숫자는 엉뚱한 기록에 걸립니다. "
              "채널명 같은 조각으로 고르세요 (예: --clear 영업). 아무것도 하지 않았습니다.",
              file=sys.stderr)
        return 1

    # 토큰별로 먼저 맞춰 보고, **하나라도 못 맞추면 통째로 그만둔다** — `select` 와
    # 같은 전부-아니면-전무다. 오타 하나가 조용히 넘어가면 그 취소는 안 된 채 남는다.
    # (한 조각이 여러 건에 걸리는 것은 그대로 둔다 — 한 채널의 결정을 한꺼번에 푸는
    # 정상 용법이고, 지운 id 는 전부 화면에 적는다.)
    per_tok = {t: [] for t in toks}
    for bucket in ("deferred", "dismissed", "applied"):
        for k in (state.get(bucket) or {}):
            for t in toks:
                if t in k:
                    per_tok[t].append((bucket, k))

    missed = [t for t in toks if not per_tok[t]]
    if missed:
        print(f"! 못 맞춘 것: {', '.join(missed)} — 아무것도 하지 않았습니다.", file=sys.stderr)
        return 1
    if not toks:
        print("! 취소할 것을 찾지 못했습니다.", file=sys.stderr)
        return 1

    LABEL = {"applied": "반영", "dismissed": "뺌", "deferred": "나중에"}
    state_hit = 0
    seen = set()
    for t in toks:
        for bucket, k in per_tok[t]:
            if (bucket, k) in seen:
                continue
            seen.add((bucket, k))
            del state[bucket][k]
            print(f"  취소({LABEL.get(bucket, bucket)}): {k}")
            state_hit += 1

    if state_hit:
        _write(STATE, state)
    return 0


def _summary_lines(text: str) -> list:
    """요약 자리만 잘라낸다. 범위 판정은 `review_work` 것을 그대로 쓴다."""
    lines = text.split("\n")
    return lines[: R._summary_end(lines)]


def _channel_to_file(state: dict, work: dict) -> dict:
    """채널 이름 → 그 채널의 md 파일명.

    `dismissed` 의 id 는 `<채널>|<종류>|…` 이라 **채널 이름**만 들어 있는데, 개명된
    채널은 md 파일명이 그대로 남는다(`review_work.md_path` 의 주석). 이름으로 md 를
    찾으면 개명된 채널의 「빼」가 영영 안 옮겨진다.

    **`.sync-state.json` 의 `channels` 를 원본으로 쓴다** — 자동 반영이 채널마다
    `name`·`file` 을 적어 두는 자리라 목록에 안 뜬 채널까지 덮는다. 아침 목록의
    `channel`→`file` 짝은 그것을 못 읽었을 때의 보조다. 둘 다 없으면 이름이 곧
    파일명이라고 본다(개명이 없었다는 뜻).
    """
    out = {}
    for it in (work or {}).get("items", []) or []:
        ch = it.get("channel")
        if ch:
            out[ch] = it.get("file") or ch
    for rec in ((state or {}).get("channels") or {}).values():
        if isinstance(rec, dict) and rec.get("name"):
            out[rec["name"]] = rec.get("file") or rec["name"]
    return out


def carry_dismissals(shown_stems: set) -> list:
    """**「빼」로 정한 것을 이번에 고친 요약 판으로 옮긴다.** [(id, 기록, 새 해시)]

    ── 무엇이 문제였나 ──

    `drop()` 은 그때의 **채널 요약 전체** 해시를 박아 두고(`summaryHash` 는
    `extractSummary` 가 뽑은 요약 자리 전체를 센다), `suppress`(JS)·`_still_dismissed`
    (파이썬)는 그 해시가 지금과 같을 때만 계속 뺀다. 그래서 **같은 채널 요약을 다른
    이유로 한 글자만 고쳐도** 해시가 달라지고, 뺐던 항목이 다음 07:00 아침 보고에
    아무 소리 없이 다시 올라온다. 자료 저장소의 `.sync-state.json` 에 그 일이 실제로
    일어난 기록이 남아 있다(2026-08-31 자 사유에 「같은 요약 블록의 … 줄을 고쳐 앞서의
    뺌이 해시로 무효화돼 다시 적는다」).

    ── 어디까지 옮기고 어디부터 안 옮기나 ──

    옮기는 것:  **이 회차 목록에 안 떠 있던 「빼」** — 즉 07:00 회차의 `suppress` 가
                이미 걸러 놓아 사람 앞에 오지도 않은 것. 사람이 오늘 본 목록에는 없던
                항목이고, 오늘의 편집은 그것에 대한 답이 아니다.
    안 옮기는 것: **이 회차 목록에 떠 있는 「빼」** — 해시가 이미 어긋나 후보로 돌아온
                것이다. 그건 아직 답 안 한 질문이라, 여기서 해시를 새로 찍으면 사람이
                한 번도 안 본 채 조용히 묻힌다. **그게 이 수선이 뚫으면 안 되는 방향이다.**
                단, 그 항목을 **오늘 다시 `--drop` 한 것**은 옮긴다 — 기록 시각이 목록
                생성 시각보다 뒤면 오늘 사람이 보고 정한 것이다(`applied_after` 와 같은
                판정을 그대로 쓴다).

    그리고 옮긴 것은 **전부 화면에 적는다.** 이 파일의 규칙이 「도장은 보여준 내용이
    만든다」인데, 옮기기만 조용히 하면 같은 규칙을 여기서 어기는 것이 된다. 편집이
    그 판정을 무효로 만들었다면 사람이 보고 `--clear` 로 되돌린다.
    """
    try:
        work = R.read_pending_work()
    except R.PendingWorkUnreadable:
        # **목록을 못 읽으면 아무것도 안 옮긴다.** 「목록에 없다」를 못 세는 상태라,
        # 옮기면 답 안 한 질문까지 함께 묻을 수 있다. 안 옮기면 옛 동작 그대로다.
        return []
    generated = (work or {}).get("generated", "")
    listed = {it.get("id") for it in (work or {}).get("items", []) or []}

    state = _read(STATE, {})
    dismissed = state.get("dismissed") or {}
    file_of = _channel_to_file(state, work)

    carried = []
    for did, rec in dismissed.items():
        if not isinstance(rec, dict) or not rec.get("summaryHash"):
            continue                       # 해시가 없는 것은 애초에 안 돌아온다
        channel = str(did).split("|", 1)[0]
        name = file_of.get(channel, channel)
        if name not in shown_stems:
            continue                       # 이번에 안 보인 채널은 안 건드린다
        if did in listed and not R.applied_after(rec, generated):
            continue                       # 아직 답 안 한 질문 — 묻으면 안 된다
        new_hash = R.summary_hash(name)
        if new_hash and new_hash != rec["summaryHash"]:
            carried.append((did, rec, new_hash))
    return carried


def show_edits() -> int:
    """**바뀐 요약을 화면에 내고 그 내용에 도장을 찍는다.**

    전에는 `--stamp` 가 「승인 기록이 있는 파일」을 찍었다. 승인은 항목별로 쌓이는데
    찍는 쪽이 그걸 파일 경로로 뭉갰고 승인 기록은 지워지지 않아서, **한 번 승인된
    채널은 그 뒤 아무 승인 없이 요약을 고쳐도 통과했다** (2026-08-15, 43개 중 10개가
    그 상태였다). 승인과 실제로 쓴 문장 사이에 아무 연결이 없었다.

    그래서 `doc-archive` 7.5 와 같은 모양으로 맞췄다 — **도장을 받으려면 내용이
    반드시 화면을 거친다.** `applied` 는 여기서 읽지도 쓰지도 않는다(목록 필터가
    쓰는 기록이라 지우면 처리한 항목이 되살아난다).

    막을 수 있는 선은 「출력됐다」까지다. WHK 가 실제로 봤는지는 발신 승인 규칙이 맡는다.

    **여기서 「빼」의 요약 해시도 함께 옮긴다** (2026-09-03). 안 옮기면 요약을 한 글자만
    고쳐도 뺐던 항목이 다음 아침에 되살아난다 — 경계와 근거는 `carry_dismissals`."""
    import difflib

    stamp = load_stamp()
    files = dict(stamp.get("files") or {})

    changed = changed_channel_mds()
    targets = [md for md in changed if R.summary_changed_vs_head(md)]
    if not targets:
        if changed:
            print(f"채널 md {len(changed)}건이 고쳐졌지만 **요약 자리는 안 바뀌었습니다** — "
                  "관문이 보는 자리가 아니라 찍을 것이 없습니다.")
        else:
            print("작업 트리에 고쳐진 채널 md 가 없습니다 — 보일 것이 없습니다.")
        return 0

    for md in targets:
        rel = md.relative_to(ROOT).as_posix()
        before = _summary_lines(R.head_text(md))
        after = _summary_lines(md.read_text(encoding="utf-8"))
        visible = R.bot_visible_lines(md)

        print(f"\n── {rel} ─ 요약 자리 ────────────────────────────────")
        for line in difflib.unified_diff(before, after, lineterm="", n=2):
            if line.startswith(("---", "+++")):
                continue
            mark = ""
            if line[:1] in "+ " and R.norm_summary_line(line[1:]) in visible:
                mark = "   ← 봇 프롬프트에 실림"
            print(f"  {line}{mark}")

        files[rel] = fingerprint(md)
        print(f"\n  찍음: {rel}")

    stamp["files"] = files
    stamp["shown_at"] = now_iso()
    _write(STAMP, stamp)

    # 「빼」의 요약 해시를 이 판으로 옮긴다. **옮긴 것은 전부 적는다.**
    carried = carry_dismissals({md.stem for md in targets})
    if carried:
        state = _read(STATE, {})
        dismissed = state.get("dismissed") or {}
        print("\n── 「빼」로 정해 둔 것을 이 판으로 옮깁니다 ─────────────────")
        for did, rec, new_hash in carried:
            if did not in dismissed:
                continue
            dismissed[did]["summaryHash"] = new_hash
            dismissed[did]["carried_at"] = now_iso()
            print(f"  · {did}\n      사유: {str(rec.get('reason', '')).strip()[:100]}")
        state["dismissed"] = dismissed
        state["_dismissed_comment"] = DISMISSED_COMMENT
        _write(STATE, state)
        print("  이번 편집이 위 판정을 무효로 만들었다면 `--clear <id 조각>` 으로 되돌리세요.")

    print(f"\n{len(targets)}건 보이고 찍었습니다. 이 내용 그대로 커밋하면 관문을 통과합니다.")
    print("고치면 도장이 안 맞아 다시 막힙니다 — 그때는 --show 를 다시 돌리세요.")
    return 0


# ── 입구 ────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    # 연동 모드에서는 원문을 쓰지 않는다. **맨 앞이어야** 한다.
    from mode import exit_if_blocked
    exit_if_blocked("수집 작업 판정(decide_work)")
    ap = argparse.ArgumentParser(description="07:00 할 일을 건별로 정한다")
    ap.add_argument("--only", action="append", default=[], help="번호 또는 채널명 조각 (쉼표 가능)")
    ap.add_argument("--apply", action="store_true", help="반영하기로 정함")
    ap.add_argument("--drop", action="store_true", help="뺀다 (--reason 필수)")
    ap.add_argument("--later", type=int, nargs="?", const=DEFAULT_LATER_DAYS,
                    help=f"나중에 (기본 {DEFAULT_LATER_DAYS}일)")
    ap.add_argument("--reason", default="", help="사유")
    ap.add_argument("--clear", action="append", default=[], help="보류·뺌 취소")
    ap.add_argument("--show", action="store_true",
                    help="바뀐 요약을 화면에 내고 그 내용에 도장을 찍는다 (편집을 끝낸 뒤)")
    args = ap.parse_args(argv)

    if args.show:
        return show_edits()
    if args.clear:
        return clear(args.clear)

    modes = [args.apply, args.drop, args.later is not None]
    if sum(1 for m in modes if m) != 1:
        print("! --apply / --drop / --later 중 하나를 고르세요.", file=sys.stderr)
        return 1

    # **`--only` 를 빼면 거절한다.** 목록만 찍고 0 으로 끝나면 미룬 줄 알고 넘어간다.
    if not args.only:
        print("! --only 로 어느 건인지 고르세요. 목록은 review_work.py 로 봅니다.", file=sys.stderr)
        return 1
    if (args.drop or args.later is not None) and not args.reason.strip():
        print("! --reason 이 필요합니다 — 왜 그렇게 정했는지가 없으면 나중에 되짚을 수 없습니다.",
              file=sys.stderr)
        return 1

    # **「없다」와 「못 읽는다」를 가른다.** 목록 파일이 깨져 있으면 `review_work.py` 는 사유
    # 한 줄로 멈추는데(그쪽 `main()`), 여기만 파이썬 트레이스백이 났다 — 같은 원인에 두 얼굴이
    # 나오면 사람은 자기가 뭘 잘못 쳤나부터 찾는다. `main()` 안에서 잡는 이유는 종료코드가
    # 이 함수의 계약이고 시험이 `main()` 을 직접 부르기 때문이다 (2026-08-13 리뷰).
    try:
        items = R.load_items()
    except R.PendingWorkUnreadable as e:
        print(f"! 아침 목록을 읽지 못해 멈춥니다: {e}", file=sys.stderr)
        print("  파일을 고치거나 지우세요 — 다음 07:00 회차가 다시 만듭니다.", file=sys.stderr)
        return 2
    if not items:
        print("! 목록이 비었습니다.", file=sys.stderr)
        return 1

    picked, unmatched = select(items, args.only)
    if unmatched:
        print(f"! 못 맞춘 것: {', '.join(unmatched)} — 아무것도 하지 않았습니다.", file=sys.stderr)
        return 1
    if not picked:
        print("! 고른 것이 없습니다.", file=sys.stderr)
        return 1

    if args.apply:
        return approve(picked)
    if args.drop:
        return drop(picked, args.reason.strip())
    return later(picked, args.later, args.reason.strip())


if __name__ == "__main__":
    # `git = R.git` 은 실패하면 예외를 던진다 (2026-08-12 부터 — 전에는 빈 문자열을
    # 돌려줘서 「고쳐진 채널 md 가 없다」와 구별되지 않았다). 트레이스백 대신 사유
    # 한 줄로 멈춘다 — **아무것도 못 본 채 결정을 집행하지 않는 것**이 요점이다.
    # (목록 파일을 못 읽는 경우는 `main()` 안에서 잡는다 — 위 `load_items` 자리.)
    try:
        sys.exit(main())
    except R.GitError as _e:
        print(f"저장소를 읽지 못해 멈춥니다.\n  · {_e}", file=sys.stderr)
        sys.exit(1)
