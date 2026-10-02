#!/usr/bin/env python3
"""
사안별 결정을 기록하고 집행한다 — 「이건 넣고, 이건 빼고, 이건 나중에」.

**왜 있나.** doc-archive 는 지금까지 「전부 반영」밖에 없었다. 고를 수 있는 것이
`--channel`(채널 단위)과 `--limit N`(개수)뿐이라, Hermes 가 매일 DM 으로 묻는
「미변환 첨부 N건」에 대해 건별로 답할 자리가 없었다. 빼기로 정한 것을 적는 곳은
`.doc-state.json` 의 `excluded` 하나인데 **JSON 을 손으로 고쳐야 했고**, 게다가
「영구 제외」밖에 없어서 「이번엔 아니고 나중에」를 적을 칸이 없었다.

세 가지를 한다.

  --skip    영구 제외 (`excluded`)      — 등기부등본·초안처럼 앞으로도 안 넣을 것
  --later   만기가 붙은 보류 (`deferred`) — 확정본 대기처럼 지금은 아닌 것
  --undo    이미 md 에 넣은 회차를 물린다 — 커밋 전 점검표에서 「빼」가 나왔을 때

**보류에는 만기가 반드시 붙는다.** 만기 없는 보류는 삭제와 같다 — 다시 가져오는
장치가 없으면 그 일은 사실상 안 하기로 정한 것이다(위키 `[[적어둔-일은-돌아오지-않는다]]`).
만기가 지나면 필터에서 저절로 빠져 미변환 후보로 돌아오고, 그때 09:00 위생 점검이
다시 알린다. 기록 자체는 지우지 않는다 — 언제 왜 미뤘는지가 남아야 한다.

**`--undo` 가 없으면 「빼」라는 결정이 집행되지 않는다.** 되돌리기가 4단계 수기이던
동안은 이미 넣은 것을 빼는 비용이 커서 「일단 두자」로 흐른다. 그래서 결정 기록과
같은 자리에 뒀다.

사용:
  python decide.py --list                                  # 보류·제외 현황
  python decide.py --render                                # excluded.md 만 다시 쓴다
  python decide.py <선택자> --skip  --reason "개인정보"
  python decide.py <선택자> --later 14 --reason "확정본 대기"
  python decide.py <선택자> --clear                        # 결정 취소 → 다시 후보로
  python decide.py --undo --file <md> --date 2026-08-08 --source "260808_보고.pdf"

선택자는 슬랙 파일 ID(`F0ABC…`), 매니페스트의 번호, 또는 파일명 조각이다.
아직 변환 전인 파일을 가리키려면 `--manifest` 로 수집 결과를 함께 준다
(`fetch_slack_files.py --manifest /tmp/doc-manifest.json`).

**후보 목록 자체는 여기서 안 보여준다** — 그건 슬랙을 훑는 일이라
`fetch_slack_files.py --dry-run` 이 한다. 이 스크립트는 슬랙을 부르지 않는다.

**커밋·push 는 사람 승인.** 여기서는 파일만 고치고 멈춘다.

종료코드: 0 성공 / 1 실패
"""

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from paths import DOCS_DIR  # noqa: E402
from fetch_slack_files import active_deferred, today_kst  # noqa: E402
from insert_entry import (  # noqa: E402
    ENTRY_RE, HEADING_RE, read_lines, refresh_meta, section_end, write_lines,
)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT_STATE = DOCS_DIR / ".doc-state.json"

SLACK_ID_RE = re.compile(r"^F[A-Z0-9]{6,}$")

# `_comment`·`_excluded` 와 같은 자리. 다음 사람이 이 칸을 보고 규칙을 알 수 있게 적는다.
DEFERRED_COMMENT = (
    "이번엔 안 넣기로 한 슬랙 첨부. `excluded`(영구 제외)와 달리 **만기(`until`)가 있고**, "
    "만기가 지나면 필터에서 저절로 빠져 미변환 후보로 돌아온다 — 위생 점검이 그때 다시 알린다. "
    "만기 없는 보류는 삭제와 같아서 만기를 필수로 뒀다. decide.py 로 적는다."
)

# 못 찾은 자리마다 같은 안내를 낸다. 대상이 상태 파일에 없는 가장 흔한 이유가
# 「아직 변환 전」이고, 그때 필요한 것은 매니페스트 하나다.
MANIFEST_HINT = (
    "  아직 변환 전인 파일이면 --manifest 로 수집 결과를 함께 주세요\n"
    "  (python fetch_slack_files.py --dry-run --manifest /tmp/doc-manifest.json)"
)


# ── 상태 파일 ────────────────────────────────────────────────────────────

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"ERROR: {path} 를 읽지 못했습니다 ({e})")


def save_state(path: Path, state: dict):
    """2칸 들여쓰기 · LF · UTF-8 — 기존 파일과 같은 모양이어야 diff 가 안 부푼다.

    tmp 에 쓰고 바꿔 끼운다. 이 파일은 **무엇이 변환됐나**의 유일한 기록이라, 쓰다 죽어
    반쪽이 남으면 `load_state` 가 못 읽고 doc-archive 가 통째로 멈춘다. 대화 쪽
    `apply_edits.py` 의 save_state 와 같은 방식이다.
    """
    text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    tmp.replace(path)


# ── 선택자 풀기 ──────────────────────────────────────────────────────────

def load_manifest(path: str | None) -> list:
    if not path:
        return []
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"ERROR: 매니페스트를 읽지 못했습니다 ({e})")
    # 받은 것 + 건너뛴 것 + 실패한 것 전부를 후보로 본다 — 셋 다 결정 대상이 될 수 있다.
    return [r for k in ("downloaded", "skipped", "failed") for r in d.get(k, [])]


def resolve(selector: str, state: dict, manifest: list, prefer_doc: str | None = None,
            required: bool = True):
    """선택자 → {id, channel, name}. 여럿이면 종료한다.

    찾는 순서: 매니페스트 번호 → 슬랙 파일 ID → 이름 조각(매니페스트 → 상태 파일 전부).

    `prefer_doc` 은 `--undo --file` 로 이미 정해진 md 파일명이다. 이름 조각이 여럿에
    걸릴 때 **그 md 를 가리키는 기록으로 좁힌다** — `초안.pdf` 같은 흔한 이름은 사업장마다
    있어서, 이것 없이는 「나중에」 한 줄이 늘 애매하다고 멈춘다.

    **`required=False` 면 0건일 때 죽지 않고 `None` 을 돌려준다.** `--undo` 와 함께
    부를 때 쓴다 — 7.5 관문은 9단계(상태 저장) 전이라 그 파일이 상태 파일에 **없는
    것이 정상**이고, 여기서 죽으면 회차를 물리는 일(`undo`)까지 통째로 안 돈다.
    여럿이 걸리는 경우는 `required` 와 무관하게 여전히 멈춘다 — 짐작으로 고르지 않는다.
    """
    if not selector:
        if not required:
            return None
        sys.exit("ERROR: 대상을 지정하세요 (슬랙 파일 ID · 매니페스트 번호 · 파일명 조각)")

    if manifest and selector.isdigit():
        i = int(selector)
        if not 1 <= i <= len(manifest):
            sys.exit(f"ERROR: 매니페스트에 {i}번이 없습니다 (1~{len(manifest)})")
        r = manifest[i - 1]
        return {"id": r.get("id"), "channel": r.get("channel"), "name": r.get("name")}

    pools = [
        ("매니페스트", {r["id"]: r for r in manifest if r.get("id")}),
        ("slack_files", state.get("slack_files") or {}),
        ("deferred", state.get("deferred") or {}),
        ("excluded", state.get("excluded") or {}),
        # 물림은 `slack_files` 에 없다 — 변환한 적이 없어서다. 여기 없으면
        # 적은 뒤에 되돌릴 길이 매니페스트를 다시 만드는 것뿐이 된다.
        ("superseded", state.get("superseded") or {}),
    ]

    if SLACK_ID_RE.match(selector):
        for _, pool in pools:
            if selector in pool:
                v = pool[selector]
                return {"id": selector, "channel": v.get("channel"), "name": v.get("name")}
        if not required:
            return None
        sys.exit(
            f"ERROR: {selector} 를 매니페스트에도 상태 파일에도 못 찾았습니다.\n"
            f"{MANIFEST_HINT}"
        )

    needle = selector.lower()
    hits, seen = [], set()
    for where, pool in pools:
        for fid, v in pool.items():
            if fid in seen or needle not in str(v.get("name", "")).lower():
                continue
            seen.add(fid)
            hits.append({"id": fid, "channel": v.get("channel"), "name": v.get("name"), "where": where})
    if not hits:
        if not required:
            return None
        sys.exit(f"ERROR: '{selector}' 에 맞는 파일이 없습니다.\n{MANIFEST_HINT}")
    if len(hits) > 1 and prefer_doc:
        pool = {**(state.get("slack_files") or {})}
        narrowed = [h for h in hits
                    if str(pool.get(h["id"], {}).get("doc", "")).replace("\\", "/").endswith(prefer_doc)]
        if len(narrowed) == 1:
            hits = narrowed
    if len(hits) > 1:
        print(f"ERROR: '{selector}' 에 {len(hits)}건이 걸립니다 — 더 좁혀 주세요:", file=sys.stderr)
        for h in hits[:10]:
            print(f"  {h['id']}  #{h['channel']}  {h['name']}  ({h['where']})", file=sys.stderr)
        sys.exit(1)
    return hits[0]


# ── 결정 기록 ────────────────────────────────────────────────────────────

def put(state: dict, bucket: str, target: dict, rec: dict, force: bool = False):
    """`excluded`/`deferred`/`superseded` 에 한 건을 적는다.

    **이미 다른 칸에 결정이 있으면 멈춘다.** 옮기는 것은 앞서 적어 둔 사유와
    「누가 언제 정했나」를 지우는 일이라, 조용히 하면 안 된다 — 영구 제외였던
    등기부등본을 실수로 `--later` 하면 개인정보 때문에 뺐다는 근거가 사라지고,
    만기가 지나는 순간 후보로 돌아와 다음 사람이 그냥 넣는다.
    같은 칸 안에서 다시 적는 것(만기 연장 등)은 그대로 덮는다.
    """
    others = [b for b in ("excluded", "deferred", "superseded") if b != bucket]
    LABEL = {"excluded": "영구 제외", "deferred": "보류", "superseded": "물림"}
    for other in others:
        prev = (state.get(other) or {}).get(target["id"])
        if not prev:
            continue
        if not force:
            sys.exit(
                f"ERROR: 이 파일은 이미 **{LABEL[other]}** 로 정해져 있습니다 — "
                f"#{target['channel']} {target['name']}\n"
                f"  사유: {prev.get('reason') or prev.get('kept') or '(없음)'}\n"
                f"  정한 때: {prev.get('decided') or prev.get('at') or '(없음)'}"
                + (f" · 만기 {prev['until']}" if prev.get("until") else "")
                + "\n  바꾸려면 그 판정을 지운다는 뜻이니 --force 를 붙이거나, 먼저 --clear 하세요."
            )
        print(f"! 앞선 {LABEL[other]} 판정을 덮습니다 — "
              f"{prev.get('reason') or prev.get('kept') or ''}")
        state[other].pop(target["id"])
    state.setdefault(bucket, {})[target["id"]] = rec
    # 주석 칸은 사람이 읽는 자리다. deferred 를 처음 만들 때 함께 넣는다.
    if bucket == "deferred" and "_deferred" not in state:
        state["_deferred"] = DEFERRED_COMMENT
        state = reorder(state)
    return state


def reorder(state: dict) -> dict:
    """`_deferred` 를 `deferred` 바로 앞에 둔다 (`_excluded`·`excluded` 와 같은 모양)."""
    keys = [k for k in state if k not in ("_deferred", "deferred")]
    out = {}
    for k in keys:
        out[k] = state[k]
        if k == "excluded":
            if "_deferred" in state:
                out["_deferred"] = state["_deferred"]
            if "deferred" in state:
                out["deferred"] = state["deferred"]
    for k in ("_deferred", "deferred"):        # excluded 가 없던 경우
        if k in state and k not in out:
            out[k] = state[k]
    return out


# ── 물리기 ───────────────────────────────────────────────────────────────

def remove_entry(path: Path, entry_date: str, source: str):
    """회차 블록과 요약 표 행을 지운다. (사유, 남은 회차 수) 를 돌려준다.

    회차 블록의 범위는 그 헤더 줄부터 **다음 회차 헤더 또는 다음 '## ' 헤딩 직전**까지다.
    `archive.js` 의 splitMessages 가 자르는 단위와 같아야 — 덜 지우면 본문이 앞 회차에
    붙어 엉뚱한 회차의 검색 결과로 나온다.
    """
    lines, nl = read_lines(path)

    start = None
    for i, line in enumerate(lines):
        m = ENTRY_RE.match(line)
        if m and m.group(1) == entry_date and (not source or source in line):
            start = i
            break
    if start is None:
        return f"회차 {entry_date} · {source} 를 찾지 못했습니다", None

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if ENTRY_RE.match(lines[j]) or HEADING_RE.match(lines[j]):
            end = j
            break
    del lines[start:end]

    # 요약 표 행 — 같은 날짜 + 같은 원본 파일명
    if source:
        lines = [
            l for l in lines
            if not (l.lstrip().startswith("|") and l.startswith(f"| {entry_date} ") and source in l)
        ]

    # 빈 월 섹션은 남기지 않는다 — verify_format 이 「회차 헤더가 없습니다」로 잡는다.
    kept = []
    for i, line in enumerate(lines):
        if re.match(r"^##\s+\d{4}-\d{2}\s*$", line):
            nxt = section_end(lines, i)
            if not any(ENTRY_RE.match(lines[j]) for j in range(i + 1, nxt)):
                continue
        kept.append(line)
    lines = kept

    write_lines(path, lines, nl)
    remaining = sum(1 for l in lines if ENTRY_RE.match(l))
    return None, remaining


def undo(args, state: dict):
    """(상태, 상태 파일이 바뀌었나). md 만 고치고 끝나는 경우가 흔하다 —
    7.5 관문은 9단계(상태 저장) 전이라 그때는 지울 키가 아직 없다."""
    md = Path(args.file)
    if not md.is_absolute():
        for base in (Path.cwd(), DOCS_DIR / "projects", DOCS_DIR):
            if (base / md).exists():
                md = base / md
                break
    if not md.exists():
        sys.exit(f"ERROR: md 를 찾지 못했습니다 — {args.file}")

    # 문서 트리 밖의 md 는 **손대기 전에 거절한다.** 예전에는 파일 이름만 남겨 그대로
    # 진행했는데, 상태 파일의 키는 `사업장/파일.md` 라 이름만으로는 어느 회차도 안 맞는다
    # — 즉 md 는 고쳐 놓고 상태 쪽은 아무것도 안 하면서 종료코드 0 으로 끝났다.
    # 조용한 무동작은 「반영됐다」로 읽힌다. 검사는 `remove_entry` **앞**에 둔다 —
    # 뒤에 두면 남의 파일을 고쳐 놓고 거절하게 된다.
    if DOCS_DIR not in md.parents:
        sys.exit(f"ERROR: {DOCS_DIR} 밖의 파일입니다 — {md}")

    why, remaining = remove_entry(md, args.date, args.source)
    if why:
        sys.exit(f"ERROR: {why}")

    touched_state = False
    rel = md.relative_to(DOCS_DIR).as_posix()

    def _norm(p):
        """`projects/` 접두를 뗀 `사업장/파일.md`.

        상태 파일 안에서 `doc` 값은 접두가 붙은 것과 안 붙은 것이 섞여 있었고(336건 중 20건),
        `series` 키는 늘 안 붙어 있다. **2026-08-27 에 41건을 짧은 형태로 통일했지만**
        손으로 적는 자리라 다시 섞일 수 있어 한 모양으로 맞춰 놓고 비교한다.
        """
        p = str(p).replace("\\", "/")
        return p[len("projects/"):] if p.startswith("projects/") else p

    rel_key = _norm(rel)
    if remaining:
        print(f"회차 제거 — {rel} ({args.date} · {args.source})")
        print(f"   {refresh_meta(md)}")
    else:
        md.unlink()
        print(f"회차가 하나도 안 남아 파일을 지웠습니다 — {rel}")
        # 시리즈 기록도 함께. 남겨두면 다음 실행이 없는 파일에 회차를 붙이려 한다.
        for key in [k for k in (state.get("series") or {}) if _norm(k) == rel_key]:
            state["series"].pop(key)
            touched_state = True
            print(f"   series 기록 제거 — {key}")

    # 상태 파일 키. 7.5 관문(9단계 전)에서 부르면 아직 없는 것이 정상이다.
    #
    # **반드시 이 md 를 가리키는 기록만 본다.** 같은 파일명이 채널 둘에 올라오는 일이
    # 흔해서(본부별 주간보고 등) 이름만 맞춰 지우면 남의 사업장 기록이 날아간다.
    # 날아가면 다음 실행이 그 자료를 새것으로 보고 다시 변환해 같은 회차가 두 벌 쌓인다.
    slack = state.get("slack_files") or {}

    def points_here(v):
        # **파일명이 아니라 경로로** 맞춘다. 위 주석이 경고하는 그대로였다 — `md.name` 으로
        # 맞추면 `본부별-주간보고.md` 처럼 여러 사업장에 같은 이름이 있는 자료에서 **남의
        # 사업장 기록**이 지워지고, 다음 실행이 그걸 새것으로 보아 두 벌로 쌓았다 (2026-08-10).
        return _norm(v.get("doc", "")) == rel_key

    dropped = [fid for fid, v in slack.items() if v.get("name") == args.source and points_here(v)]
    if not dropped:
        # 이름이 안 맞는 경우(회차 헤더의 원본명과 상태 파일의 name 이 다를 때)의 대비책.
        # 여기서 여럿이 걸리면 **하나도 지우지 않는다** — 같은 날 같은 md 에 들어간 다른
        # 회차까지 지우면 그것이 다음 실행에서 다시 변환돼 두 벌이 된다. 짐작으로 지우지 않는다.
        by_date = [fid for fid, v in slack.items() if v.get("date") == args.date and points_here(v)]
        if len(by_date) == 1:
            dropped = by_date
        elif len(by_date) > 1:
            print(f"   ! 같은 날짜·같은 md 를 가리키는 기록이 {len(by_date)}건이라 어느 것인지 정할 수 없습니다:")
            for fid in by_date:
                print(f"       {fid}  {slack[fid].get('name')}")
            print("     지우지 않았습니다 — .doc-state.json 에서 직접 확인하세요")

    for fid in dropped:
        slack.pop(fid)
        touched_state = True
        print(f"   slack_files 기록 제거 — {fid}")
    if not dropped:
        print("   slack_files 에서 지운 것 없음 (9단계 전이면 정상)")
    return state, touched_state


# ── 제외 목록 파일 ───────────────────────────────────────────────────────

EXCLUDED_MD_NAME = "excluded.md"

EXCLUDED_MD_HEAD = """# 아카이브에 넣지 않은 첨부

> `.doc-state.json` 의 `excluded` 를 사람이 읽는 표로 옮긴 것. **`decide.py` 가 다시 쓴다** —
> 손으로 고치면 다음 실행이 덮는다. 고칠 것은 상태 파일에 있고, 빼기로 정하는 것은
> `decide.py --skip` 이다. 상태 파일을 손으로 고쳤으면 `decide.py --render` 로 맞춘다.
>
> 여기 실리는 것은 **판단해서 뺀 것**뿐이다. 「최신이 대신한 옛 판」(`superseded`)과
> 「만기가 붙은 보류」(`deferred`)는 성격이 달라 안 싣는다 — `decide.py --list` 로 본다.
>
> **봇은 이 파일을 안 읽는다.** `documents.js` 는 `documents/projects/` 아래만 훑고
> 색인은 각 문서 md 의 메타로 만든다. 그래서 여기에 몇 줄을 적어도 색인 바닥이 안 오른다.
> 뒤집어 말하면 **봇은 여기 적힌 자료를 아예 모른다** — 사람이 물었을 때 「자료가 없다」고
> 오답하면 곤란한 것은 `index.md` 의 「변환하지 못한 것」에도 함께 적는다."""


def _cell(s) -> str:
    """표 한 칸. 줄바꿈·파이프가 그대로 들어가면 표가 깨진다."""
    return re.sub(r"\s+", " ", str(s or "")).replace("|", r"\|").strip()


def render_excluded(state: dict) -> str:
    """`excluded` 를 채널별 표로 그린다.

    **갈래(개인정보·중복·이전 판…)로 묶지 않는다.** 그 분류는 상태 파일에 없어서 사람이
    붙여야 하는데, 그러면 `--skip` 으로 새로 들어온 건이 분류 없이 남아 이 파일이 조용히
    낡는다. 채널·사유·결정일은 상태 파일에 있는 그대로라 다시 그리면 언제나 맞는다.
    """
    ex = state.get("excluded") or {}
    out = EXCLUDED_MD_HEAD.split("\n")
    out += ["", f"총 **{len(ex)}건**. 채널별로 묶었고 사유·결정일은 상태 파일에 적힌 그대로다.", ""]
    if not ex:
        out += ["아직 없다.", ""]
    by_ch: dict[str, list] = {}
    for fid, v in ex.items():
        by_ch.setdefault(v.get("channel") or "(채널 없음)", []).append((fid, v))
    for ch in sorted(by_ch):
        rows = sorted(by_ch[ch], key=lambda kv: _cell(kv[1].get("name")))
        out += [f"## #{ch} — {len(rows)}건", ""]
        out += ["| 파일 | 왜 뺐나 | 결정 | 슬랙 파일 ID |", "|---|---|---|---|"]
        for fid, v in rows:
            out.append(f"| {_cell(v.get('name'))} | {_cell(v.get('reason'))} "
                       f"| {_cell(v.get('decided'))} | `{_cell(fid)}` |")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def write_excluded(state: dict, path: Path) -> bool:
    """제외 목록을 다시 쓴다. 내용이 그대로면 파일을 안 건드린다 (헛 diff 를 안 만든다).

    상태 파일과 **같은 폴더**에 쓴다 — 시험이 임시 폴더의 상태 파일을 쓸 때
    저장소의 목록을 덮지 않게 하려는 것이다.
    """
    text = render_excluded(state)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    tmp.replace(path)
    return True


# ── 보기 ─────────────────────────────────────────────────────────────────

def kept_in_archive(state: dict, kept_name) -> bool:
    """`kept` 이름이 **지금** `slack_files` 에 있나.

    저장된 `kept_archived` 는 `--superseded` 가 적는 순간의 값이라, 그 「최신」이
    나중에 또 물리면(사슬이 한 칸 늘면) 아카이브에서 빠져도 값은 true 로 남는다 —
    화면이 저장값을 읽으면 없는 파일을 「있다」고 말한다(2026-09-22, 실물 사슬 5건).
    그래서 화면(`--list`)은 저장값을 안 읽고 늘 여기서 다시 재고, 저장값은
    「적던 날의 사실」 기록으로만 남는다."""
    return any(v.get("name") == kept_name
               for v in (state.get("slack_files") or {}).values())


def show(state: dict):
    # 만기를 재는 「오늘」은 언제나 KST 다 — 거르는 쪽(active_deferred)과 같은 날짜여야
    # 화면의 D-N 과 실제 걸림이 안 갈린다.
    today = today_kst()
    deferred = state.get("deferred") or {}
    excluded = state.get("excluded") or {}

    live = active_deferred(state, today)
    dead = {k: v for k, v in deferred.items() if k not in live}

    if live:
        print(f"보류 {len(live)}건 — 만기 전이라 후보에서 빠져 있습니다")
        for fid, v in sorted(live.items(), key=lambda x: str(x[1].get("until"))):
            left = (date.fromisoformat(v["until"]) - today).days
            print(f"  {v.get('until')} (D-{left})  #{v.get('channel')}  {v.get('name')}")
            print(f"      {v.get('reason', '')}  [{fid}]")
        print()

    if dead:
        print(f"만기가 지난 보류 {len(dead)}건 — **이미 후보로 돌아와 있습니다**")
        for fid, v in sorted(dead.items(), key=lambda x: str(x[1].get("until"))):
            # 만기가 없거나 못 읽는 것도 여기 온다 — 그 사실을 감추면 왜 후보로 돌아왔는지 모른다.
            until = v.get("until") or "만기 없음(그래서 후보로 돌아왔습니다)"
            print(f"  {until}  #{v.get('channel')}  {v.get('name')}  [{fid}]")
        print("  다시 미루려면 --later, 영구히 빼려면 --skip, 넣을 것이면 doc-archive 를 돌립니다\n")

    superseded = state.get("superseded") or {}
    if superseded:
        print(f"최신이 대신한 것 {len(superseded)}건")
        for fid, v in list(superseded.items())[:10]:
            # 저장된 kept_archived 가 아니라 지금 값 — 이유는 kept_in_archive 주석.
            mark = "" if kept_in_archive(state, v.get("kept")) else "  ! 그 최신이 아카이브에 없습니다"
            print(f"  #{v.get('channel')}  {v.get('name')}")
            print(f"      → {v.get('kept')}  ({v.get('at', '')})  [{fid}]{mark}")
        if len(superseded) > 10:
            print(f"  … 그 밖 {len(superseded) - 10}건")
        print()

    print(f"영구 제외 {len(excluded)}건")
    for fid, v in list(excluded.items())[:10]:
        print(f"  #{v.get('channel')}  {v.get('name')}")
        print(f"      {v.get('reason', '')}  [{fid}]")
    if len(excluded) > 10:
        print(f"  … 그 밖 {len(excluded) - 10}건")

    print("\n후보(아직 안 들어간 첨부)는 슬랙을 훑어야 나옵니다:")
    print("  python fetch_slack_files.py --dry-run --manifest /tmp/doc-manifest.json")


# ── main ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("selector", nargs="?", help="슬랙 파일 ID · 매니페스트 번호 · 파일명 조각")
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--manifest", help="fetch_slack_files.py --manifest 로 만든 JSON")
    ap.add_argument("--list", action="store_true", help="보류·제외 현황만 본다")
    ap.add_argument("--render", action="store_true",
                    help=f"{EXCLUDED_MD_NAME} 를 상태 파일에서 다시 쓴다 (다른 일은 안 한다)")
    ap.add_argument("--skip", action="store_true", help="영구 제외 (excluded)")
    ap.add_argument("--later", type=int, nargs="?", const=14, metavar="일수",
                    help="보류 (deferred). 만기까지의 일수, 생략하면 14")
    ap.add_argument("--clear", action="store_true", help="결정 취소 — 다시 후보로")
    ap.add_argument("--superseded", metavar="최신파일명",
                    help="물림 — 최신 1건이 대신한 옛 판. 값은 그 최신의 파일명")
    ap.add_argument("--force", action="store_true",
                    help="이미 다른 칸(제외↔보류)에 결정이 있어도 덮는다. 앞선 사유가 지워진다")
    ap.add_argument("--reason", help="왜 그렇게 정했나 (--skip·--later 에 필수 · --superseded 에는 선택)")
    ap.add_argument("--undo", action="store_true", help="이미 넣은 회차를 물린다")
    ap.add_argument("--file", help="--undo 대상 md 경로")
    ap.add_argument("--date", help="--undo 대상 회차 날짜 (YYYY-MM-DD)")
    ap.add_argument("--source", default="", help="--undo 대상 원본 파일명")
    args = ap.parse_args()

    state_path = Path(args.state)
    state = load_state(state_path)
    excluded_md = state_path.parent / EXCLUDED_MD_NAME

    if args.render:
        changed = write_excluded(state, excluded_md)
        n = len(state.get("excluded") or {})
        print(f"{excluded_md} {'갱신' if changed else '변화 없음'} — 영구 제외 {n}건")
        return 0

    acted = (args.skip or args.later is not None or args.clear
             or args.undo or args.superseded)
    if args.list or not acted:
        show(state)
        return 0

    # md 를 고친 것과 상태 파일을 고친 것은 다르다. 상태 파일은 **바뀐 것이 있을 때만** 쓴다 —
    # 안 바뀐 것을 다시 쓰면 diff 에 안 나타나도 다른 세션의 쓰기와 겹칠 창이 괜히 생긴다.
    md_changed = state_changed = False
    decide_it = args.skip or args.later is not None or args.clear or bool(args.superseded)

    # **대상을 먼저 확정한다 — undo 보다 앞에서.** `--undo` 는 `slack_files` 의 그 기록을
    # 지우는데, 선택자를 그 뒤에 풀면 방금 지운 파일을 못 찾고 **이름 조각이 걸리는 다른
    # 문서를 골라 조용히 보류한다.** 실제로 그랬다 — `--undo … --source 초안.pdf --later 14`
    # 가 엉뚱한 「사업장나 이사회 안건_초안.pdf」를 보류했고, 화면에는 성공으로 보였다.
    #
    # **`--undo` 와 함께면 못 찾아도 죽지 않는다** (`required=False`). 7.5 관문은
    # 9단계(상태 저장) 전이라 그 파일이 상태 파일에 없는 것이 정상인데, 여기서 죽으면
    # 회차를 물리는 일까지 통째로 안 돌아 **「빼」·「나중에」가 아무것도 하지 않는다.**
    # md 를 물리는 일과 판정을 적는 일을 나눠, 앞의 것은 언제나 하고 뒤의 것만 건너뛴다.
    target = None
    if decide_it:
        if (args.skip or args.later is not None) and not args.reason:
            sys.exit("ERROR: --reason 을 적으세요. 왜 뺐는지가 없으면 다음 사람이 되돌릴 수 없습니다")
        target = resolve(args.selector or args.source, state, load_manifest(args.manifest),
                         prefer_doc=Path(args.file).name if args.file else None,
                         required=not args.undo)

    if args.undo:
        if not (args.file and args.date):
            sys.exit("ERROR: --undo 에는 --file 과 --date 가 필요합니다")
        state, state_changed = undo(args, state)
        md_changed = True

    if decide_it and target is None:
        # 조용히 넘어가지 않는다. 회차는 물렸는데 판정만 안 남은 상태라, 만기가 지나
        # 돌아오는 장치가 통째로 없다 — 다음 실행이 이 파일을 새것으로 보고 다시 넣는다.
        what = ("영구 제외" if args.skip else
                "결정 취소" if args.clear else
                "물림" if args.superseded else "보류")
        sel = args.selector or args.source
        why = (f"'{sel}' 를 상태 파일에서 못 찾았습니다" if sel
               else "대상을 안 주셨습니다 (--source 나 선택자가 필요합니다)")
        print(f"\n! 회차는 물렸지만 **{what} 기록은 못 남겼습니다** — {why}.")
        if sel:
            print("  아직 변환 전인 파일이라 정상입니다. 판정을 남기려면 매니페스트를 함께 주고 다시 부르세요:")
            print(f"{MANIFEST_HINT}")
        print("  안 남기면 다음 실행이 이 첨부를 새것으로 보고 그대로 다시 넣습니다.")
        decide_it = False

    if decide_it:
        today = today_kst()

        if args.clear:
            hit = False
            for bucket in ("excluded", "deferred", "superseded"):
                if target["id"] in (state.get(bucket) or {}):
                    # 무엇을 버리는지 보이고 지운다 — 사유가 곧 판정의 근거다.
                    prev = state[bucket].pop(target["id"])
                    print(f"{bucket} 에서 뺐습니다 — #{target['channel']} {target['name']}")
                    print(f"   버린 판정: {prev.get('reason', '(없음)')} ({prev.get('decided', '')})")
                    hit = True
            if not hit:
                print(f"결정 기록이 없습니다 — #{target['channel']} {target['name']}")
            state_changed = state_changed or hit
        elif args.superseded:
            # `kept` 로 준 이름이 아카이브에 있나. 없으면 **알리되 막지 않는다** —
            # 막으면 「최신이 excluded·deferred 라 어느 판도 아카이브에 없는」 자료를
            # 적을 방법이 아예 없어지고, `excluded` 로 대신 넣는 것은 설계가 금지한
            # 것이다(「판단해서 뺀 것」과 「최신이 대신한 것」의 구별이 사라진다).
            # 조용히 적으면 다음 사람이 「최신은 들어가 있겠거니」로 넘어가는데,
            # 이 시스템에서 그 오해는 에러가 아니라 **오답**으로 나타난다.
            kept_archived = kept_in_archive(state, args.superseded)
            rec = {
                "channel": target["channel"], "name": target["name"],
                "kept": args.superseded, "kept_archived": kept_archived,
                "at": today.isoformat(),
            }
            if args.reason:
                rec["reason"] = args.reason
            state = put(state, "superseded", target, rec, force=args.force)
            print(f"물림 — #{target['channel']} {target['name']}")
            print(f"   최신 「{args.superseded}」 이 대신합니다")
            if not kept_archived:
                where = []
                for b, label in (("excluded", "일부러 뺀 것(excluded)"),
                                 ("deferred", "보류 중(deferred)"),
                                 ("skipped", "변환 실패(skipped)")):
                    if any(v.get("name") == args.superseded
                           for v in (state.get(b) or {}).values()):
                        where.append(label)
                print(f"   ! 그 최신이 **아카이브에 없습니다**"
                      + (f" — {' · '.join(where)}" if where else ""))
                print("     즉 이 자료는 옛 판도 최신도 봇이 못 봅니다. 적기는 했습니다"
                      " (kept_archived: false)")
            state_changed = True
        elif args.skip:
            state = put(state, "excluded", target, {
                "channel": target["channel"], "name": target["name"],
                "reason": args.reason, "decided": f"{today} (WHK 확인)",
            }, force=args.force)
            print(f"영구 제외 — #{target['channel']} {target['name']}")
            print(f"   {args.reason}")
            state_changed = True
        else:
            until = today + timedelta(days=args.later)
            state = put(state, "deferred", target, {
                "channel": target["channel"], "name": target["name"],
                "reason": args.reason, "until": until.isoformat(),
                "decided": f"{today} (WHK 확인)",
            }, force=args.force)
            when = "이미 지난 만기" if until < today else f"만기 {until}"
            print(f"보류 — #{target['channel']} {target['name']} ({when})")
            print(f"   {args.reason}")
            if until < today:
                print("   ! 만기가 과거입니다 — 다음 점검에서 바로 후보로 돌아옵니다")
            state_changed = True

    if state_changed:
        save_state(state_path, state)
        print(f"\n{state_path.name} 갱신.")
        # 상태 파일을 쓴 자리에서 목록도 함께 쓴다. 따로 부르게 두면 「적혔지만 목록에는
        # 없는」 제외가 생기고, 그 어긋남은 에러가 아니라 낡은 문서로만 드러난다.
        if write_excluded(state, excluded_md):
            print(f"{EXCLUDED_MD_NAME} 갱신 — 제외 목록을 다시 썼습니다.")
    if state_changed or md_changed:
        print("커밋·push 는 사람 승인입니다.")
    if md_changed:
        print("검증: python .claude/skills/doc-archive/scripts/verify_format.py --all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
