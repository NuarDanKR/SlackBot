#!/usr/bin/env python3
"""archive-inbox 의 「반영·빼·나중에」를 **다른 인터페이스가 읽을 모양**으로 내보낸다.

사용:
  python decision_export.py --show      # 내보낼 것을 세어 보고만 한다 (쓰지 않는다)
  python decision_export.py             # 쓴다 (경로는 SUMMARY_DECISION_DIR)

종료코드: 0 성공 / 1 경로 미설정·쓰기 실패

── 무엇을 푸는가 ──

승인 인터페이스가 둘이다. PF 는 이 스킬로 끝내고, 사내는 TYBot DM 으로 끝낸다.
둘이 서로의 결정을 못 보면 **같은 것을 두 번 묻는다.** 그래서 결정을 공통 모양으로
적어 두고, 후보를 보일 때 상대편 것을 읽는다.

형식은 `summary-review-decisions/v1` — 정본 설명은 TYBot 쪽
`src/tybot/summary_review_reconcile.py` 와 `docs/design/summary-approval-ports.md`.
**양쪽이 같은 키를 써야 한다.** 한 글자만 달라도 대조는 조용히 0건이 되고, 그건
「상대편이 아직 아무것도 안 했다」와 화면에서 구별되지 않는다.

── 무엇을 안 담는가 ──

요약 본문·`evidence` 인용·Slack 원문은 **안 담는다.** 이 파일은 다른 조직이 읽는
것이고, PF 문장이 사내로 또는 그 반대로 건너가면 그건 권한 밖 노출이다. 대조에
필요한 것은 좌표·해시·상태뿐이고 본문은 각자 자기 쪽에서 본다.

── 좌표가 없으면 안 보낸다 ──

Hermes 의 결정 기록(`.sync-state.json`)은 원래 **항목 id 와 요약 자리 해시**만 담았다.
그건 원문 좌표가 아니다 — 요약이 고쳐지면 달라지는 값이라 「사람이 어느 메시지를
보고 정했나」를 말해 주지 않는다. 여기서는 그 항목의 `evidence` 인용을 채널 md 에서
**다시 찾아** 메시지 블록의 줄 범위와 그 블록의 지문을 좌표로 쓴다.

못 찾거나, 인용이 **여러 메시지에 걸쳐** 있으면 좌표를 적지 않는다. 받는 쪽은
`no_coordinate` 로 떨어져 **다시 묻는다.** 틀린 좌표를 적는 것보다 그쪽이 낫다 —
좌표가 어긋나면 사람이 승인하지 않은 것을 승인된 것으로 읽는다.

── 좌표계가 다르다는 사실 ──

Hermes 의 좌표는 `slack-export/channels/<채널>.md` 의 **메시지 블록**이고, TYBot 의
좌표는 `workspaces/<ws>/channels/<id>__<name>/raw/<날짜>.md` 의 **줄**이다. 그래서
지금은 두 체계 사이에서 **아무것도 안 맞는다** — 맞지 않으면 `no_match` 가 되어 다시
묻는다. 그게 맞는 동작이다. 두 좌표를 변환해 「같은 것으로 보기」는 **하지 않는다**:
변환이 어긋난 날 다른 메시지를 같은 것으로 보게 되고, 그건 조용히 틀린다. 좌표가
하나로 합쳐지는 것은 아카이브가 Archiver 아래로 모일 때다.

같은 체계 안에서는 지금도 쓸모가 있다 — 한 PC 에서 정한 것을 다른 PC·VM 이 읽는다.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "slack-sync" / "scripts"))
sys.path.insert(0, str(_HERE.parents[1] / "_shared"))

import review_work as R  # noqa: E402
from paths import CONFIG  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

#: 공통 결정 기록 형식. TYBot 의 `summary_review_reconcile.EXPORT_SCHEMA` 와 같아야 한다.
EXPORT_SCHEMA = "summary-review-decisions/v1"
#: 이 스킬의 출처 이름. TYBot 의 `SOURCE_HERMES_INBOX` 와 같아야 한다.
SOURCE = "hermes-archive-inbox"
#: 결정 파일 뿌리. 양쪽이 **같은 환경변수**를 읽는다.
EXPORT_DIR_ENV = "SUMMARY_DECISION_DIR"

#: `.sync-state.json` 의 통 이름 → 공통 상태 이름. 반영=승인, 뺌=거절, 나중에=보류.
BUCKET_STATES = {"applied": "approved", "dismissed": "rejected", "deferred": "deferred"}

#: 내보내는 기록의 키. **TYBot 의 `to_record()` 와 같은 집합이어야 한다.**
RECORD_KEYS = (
    "candidate_id", "workspace", "channel_id", "evidence_locator", "evidence_hash",
    "evidence_message_ts", "kind", "state", "generation", "decided_at", "decided_by",
    "source",
)


class UnsafeExportTarget(ValueError):
    """경로 조각이 경로로 읽힐 수 있다. 걸러내지 않고 **던진다.**

    걸러서 만든 이름은 다른 워크스페이스의 이름과 같아질 수 있고, 그러면 둘이 같은
    파일을 쓴다.
    """


def _safe_segment(value: str, *, what: str) -> str:
    name = str(value or "").strip()
    if not name or name in {".", ".."}:
        raise UnsafeExportTarget(f"{what} 이 비었거나 경로 자리표시자입니다: {value!r}")
    if any(sep in name for sep in ("/", "\\", os.sep)) or "\x00" in name:
        raise UnsafeExportTarget(f"{what} 에 경로 구분자가 있습니다: {value!r}")
    return name


def export_root(env: str | None = None) -> Path | None:
    """설정된 뿌리. 안 정했으면 `None` — 그 설치에서는 내보내기가 꺼진 것이다."""
    raw = (env if env is not None else os.getenv(EXPORT_DIR_ENV, "")).strip()
    return Path(raw) if raw else None


def export_path(root: Path | str, *, workspace: str, source: str = SOURCE) -> Path:
    """`<root>/<workspace>/<source>.json`. 출처마다 **파일이 다르다.**

    한 파일을 둘이 쓰면 마지막 쓰기가 앞 결정을 통째로 덮는다 — 담는 것이 한 출처의
    전체 스냅샷이라 부분 병합이 성립하지 않는다.
    """
    return (
        Path(root)
        / _safe_segment(workspace, what="워크스페이스")
        / f"{_safe_segment(source, what='출처')}.json"
    )


# ── Archiver 정본 (HERMES_MODE=pf-archiver) ──────────────────────────────
#
# 그 모드에서 채널 본문은 디스크에 Hermes 모양으로 없고 정본에서 **투영**된 것이다
# (`src/archive-reader/archiver.js`). 그래서 좌표도 그쪽에서 받는다.
#
# **파이썬에서 정본을 다시 파싱하지 않는다.** 그러면 투영이 두 벌이 되고, 두 벌은
# 갈린다 — 갈리면 화면이 보여 준 원문과 기록의 좌표가 다른 메시지를 가리킬 수 있고
# 그건 조용히 틀린다. `summary_hash` 가 node 를 부르는 것과 같은 자리다.
#
# 얻는 것이 하나 더 있다. 정본 좌표는 TYBot 의 `_source_rows` 와 **같은 모양**
# (`<상대경로>:<줄번호>`)이고 해시도 같은 공식(`evidence_refs.content_hash`)이다.
# 즉 이 모드에서는 PF 와 사내가 **같은 좌표로** 대조된다 — 좌표계가 둘이던 동안
# 불가능했던 것이다(`docs/design/summary-approval-ports.md` §3.2).
_PROJECTION: dict = {}


def _node_json(args: list) -> dict | None:
    """`scripts/archiver-channel.js` 를 불러 JSON 을 받는다. 못 받으면 `None`.

    **좌표를 지어내지 않는다.** node 가 없거나 모드가 아니면 `None` 이고, 그러면
    좌표 없이 기록되어 받는 쪽이 다시 묻는다.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return None
    script = R.CODE_ROOT / "scripts" / "archiver-channel.js"
    if not script.is_file():
        return None
    r = subprocess.run(
        [node, str(script), *args], capture_output=True, text=True, encoding="utf-8",
        cwd=str(R.CODE_ROOT),
    )
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def archiver_mode() -> bool:
    try:
        import mode as M

        return M.mode() == M.PF_ARCHIVER
    except Exception:  # noqa: BLE001 - 모드를 못 읽으면 그 모드가 아닌 것으로 본다
        return False


def projection(channel: str) -> dict | None:
    """그 채널의 투영 본문과 좌표표. 프로세스 수명 동안 캐시한다."""
    if channel in _PROJECTION:
        return _PROJECTION[channel]
    got = _node_json([channel])
    _PROJECTION[channel] = got
    return got


def archiver_coordinate(item: dict) -> tuple:
    """정본 기준 좌표. `(locator, hash, workspace, channel_id, message_ts, 사유)`.

    인용을 찾는 것은 **투영 본문 위에서** `review_work.evidence_block()` 이 한다 —
    `pf` 와 같은 함수다. 찾은 줄 범위를 reader 의 좌표표에 대본다.
    """
    name = str(item.get("file") or item.get("channel") or "")
    got = projection(name)
    if not got:
        return "", "", "", "", "", "Archiver 투영을 받지 못했다(node·모드 확인)"
    block = R.evidence_block(R.md_path(item), item.get("evidence") or "", text=got["text"])
    if not block.found:
        return "", "", "", "", "", block.note
    if block.missing:
        return "", "", "", "", "", f"원문에 없는 인용 조각이 있다({len(block.missing)}건)"
    # `evidence_block` 의 `start`/`end` 는 0-based·끝 제외. 좌표표는 1-based·양끝 포함.
    start, end = block.start + 1, block.end
    hits = [c for c in got["coords"] if start <= c["endLine"] and end >= c["startLine"]]
    if len(hits) != 1:
        return "", "", "", "", "", f"근거가 메시지 {len(hits)}개에 걸쳐 있다"
    hit = hits[0]
    if start < hit["startLine"] or end > hit["endLine"]:
        return "", "", "", "", "", "근거 범위가 메시지 블록과 어긋난다"
    if not hit.get("messageTs"):
        return "", "", "", "", "", "정본에 message_ts 가 없는 옛 줄이다"
    return (
        hit["locator"], hit["evidenceHash"], got["workspace"], got["channelId"],
        hit["messageTs"], "",
    )


def workspace_label() -> str:
    """config.json 의 `workspace`. 못 읽으면 빈 문자열.

    **이 값은 Slack team ID 가 아니다** — config 주석이 「사람이 알아보려고 적는 것」
    이라고 적고 있다. 그래도 이것을 쓰는 이유는 Hermes 가 가진 유일한 식별자이기
    때문이고, 비어 있으면 내보내기를 **아예 안 한다**: 빈 워크스페이스로 적으면 받는
    쪽에서 `source_mismatch` 가 아니라 아무 데도 안 걸리는 행이 되어, 사람은 파일에
    결정이 쌓이는데 왜 하나도 안 맞는지를 알 수가 없다.
    """
    # **`pf-archiver` 에서는 정본의 workspace 키를 쓴다.** 사람이 읽는 이름을 쓰면
    # TYBot 이 쓰는 키와 달라서 같은 자료의 결정이 `source_mismatch` 로 떨어진다 —
    # 좌표와 해시가 같은데 안 걸리고, 그 상태는 「상대편이 아무것도 안 했다」 로 보인다.
    if archiver_mode():
        listing = _node_json(["--list"])
        return str((listing or {}).get("workspace") or "")
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    name = str((data or {}).get("workspace") or "").strip()
    # config.example.json 의 자리표시자를 그대로 둔 설치가 있다. `<…>` 는 이름이 아니다.
    return "" if name.startswith("<") else name


def channel_ids(state: dict) -> dict:
    """채널 md 파일명 → Slack 채널 ID. **한 파일에 ID 가 둘이면 담지 않는다.**

    `.sync-state.json` 의 `channels[<id>] = {name, file, …}` 를 뒤집는다. 개명하면
    같은 `file` 을 여러 ID 가 거쳐 갈 수 있는데, 그때 아무거나 고르면 **다른 채널의
    ID 로 결정을 적는다.** 애매하면 비우고 받는 쪽이 다시 묻게 한다.
    """
    # `pf-archiver` 에는 `.sync-state.json` 이 없다 — 채널 ID 는 정본 디렉터리 이름에
    # 박혀 있고 그것이 정체성이다(개명해도 같은 채널). 옛 지도를 읽으면 Hermes writer
    # 시절의 ID 로 적게 되고, 그 지도는 안 바뀌므로 오류도 안 난다.
    if archiver_mode():
        listing = _node_json(["--list"]) or {}
        out: dict = {}
        for row in listing.get("channels") or []:
            for alias in (row.get("firstName"), row.get("name")):
                if alias:
                    out.setdefault(str(alias), str(row.get("channelId") or ""))
        return {k: v for k, v in out.items() if v}
    seen: dict = {}
    for cid, info in ((state or {}).get("channels") or {}).items():
        if not isinstance(info, dict):
            continue
        for key in (info.get("file"), info.get("name")):
            key = str(key or "")
            if not key:
                continue
            if key in seen and seen[key] != cid:
                seen[key] = ""          # 애매하다 — 못 쓴다
            elif key not in seen:
                seen[key] = str(cid)
    return {k: v for k, v in seen.items() if v}


def _block_digest(lines: list, start: int, end: int) -> str:
    """메시지 블록의 지문. **줄끝은 지문에 넣지 않는다.**

    같은 내용이 개발 PC(CRLF)와 VM(LF)에서 다른 지문을 내면, 반입한 결정 파일이
    「원문이 바뀌었다」로 거부된다 — 아무것도 안 바뀌었는데도 그렇다.
    """
    body = "\n".join(line.rstrip("\r") for line in lines[start:end])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def coordinate_for(item: dict) -> tuple:
    """그 항목의 **원문 좌표**. `(locator, hash, 사유)` — 못 내면 앞 둘이 빈 문자열.

    좌표는 `slack-export/channels/<파일>.md#L<첫줄>-L<끝줄>` (1-based, 양끝 포함)이고
    해시는 그 블록의 지문이다. 화면(`context_for`)과 **같은 `evidence_block()`** 을
    쓴다 — 따로 찾으면 사람이 본 원문과 기록의 좌표가 다른 메시지를 가리킬 수 있다.
    """
    if item.get("kind") not in R.SUMMARY_KINDS:
        # 요약 계열이 아닌 것(새 채널·개명·note·파생값)에는 근거 인용이 없다.
        return "", "", "근거 인용이 없는 종류"
    if archiver_mode():
        # 정본 좌표는 TYBot 과 **같은 모양·같은 해시 공식**이다. 그래서 이 모드에서는
        # 블록 지문이 아니라 **그 줄의 지문**을 쓴다 — 양쪽이 대조되려면 그래야 한다.
        locator, digest, _ws, _cid, _ts, why = archiver_coordinate(item)
        return locator, digest, why
    md = R.md_path(item)
    block = R.evidence_block(md, item.get("evidence") or "")
    if not block.found:
        return "", "", block.note
    # **여러 메시지에 걸친 근거는 좌표로 쓰지 않는다.** 블록은 하나인데 근거가 둘 이상의
    # 메시지에 있으면, 그 하나를 좌표로 적는 순간 사람이 본 근거의 일부만 가리킨다.
    outside = [i for i in block.hits if not (block.start <= i < block.end)]
    if outside:
        return "", "", f"근거가 여러 메시지에 걸쳐 있다({len(outside)}건 밖)"
    if block.missing:
        # 관문이 부분 일치로 통과시켰고 화면은 경고를 찍는다. 그 상태의 좌표를 적으면
        # 「원문에 없는 조각이 있다」는 사실이 기록에서 사라진다.
        return "", "", f"원문에 없는 인용 조각이 있다({len(block.missing)}건)"
    rel = md.relative_to(R.ROOT).as_posix()
    locator = f"{rel}#L{block.start + 1}-L{block.end}"
    return locator, _block_digest(block.lines, block.start, block.end), ""


def records(state: dict, *, workspace: str, items_by_id: dict | None = None) -> tuple:
    """`.sync-state.json` → 공통 기록 목록. `(기록, 좌표 못 낸 사유별 건수)`.

    항목의 본문(`evidence`·요약 문장)은 **기록에 안 들어간다.** 쓰는 것은 좌표를 내는
    동안뿐이다.
    """
    out: list = []
    skipped: dict = {}
    ids = channel_ids(state)
    for bucket, common in BUCKET_STATES.items():
        for item_id, rec in ((state or {}).get(bucket) or {}).items():
            if not isinstance(rec, dict):
                continue
            # 결정할 때 함께 적어 둔 좌표가 있으면 **그것을 쓴다.** 지금 다시 재면 그
            # 뒤에 바뀐 원문의 지문이 나오고, 그러면 사람이 본 것이 아닌 것에 승인이
            # 붙는다. 없는 옛 기록만 지금 재어 본다.
            locator = str(rec.get("evidence_locator") or "")
            digest = str(rec.get("evidence_hash") or "")
            channel_id = str(rec.get("channel_id") or "")
            why = ""
            if not (locator and digest):
                item = (items_by_id or {}).get(item_id)
                if item is None:
                    why = "좌표 없는 옛 기록(항목이 목록에 없다)"
                else:
                    locator, digest, why = coordinate_for(item)
                    channel_id = channel_id or ids.get(
                        str(item.get("file") or item.get("channel") or ""), ""
                    )
            if not (locator and digest and channel_id and workspace):
                why = why or ("채널 ID 를 못 냈다" if not channel_id else "워크스페이스 미설정")
                skipped[why] = skipped.get(why, 0) + 1
                continue
            out.append({
                "candidate_id": item_id,
                "workspace": workspace,
                "channel_id": channel_id,
                "evidence_locator": locator,
                "evidence_hash": digest,
                "evidence_message_ts": str(rec.get("evidence_message_ts") or ""),
                "kind": str(rec.get("kind") or ""),
                "state": common,
                "generation": 1,
                "decided_at": str(rec.get("at") or ""),
                "decided_by": str(rec.get("decided_by") or ""),
                "source": SOURCE,
            })
    out.sort(key=lambda r: (r["decided_at"], r["candidate_id"]))
    return out, skipped


def write(path: Path, rows: list) -> int:
    """원자적으로 **통째로** 쓴다. 담는 것은 한 출처의 전체 스냅샷이다.

    증분으로 쓰면 `--clear` 로 취소한 결정이 파일에 남아, 상대편이 이미 풀린 결정으로
    후보를 생략한다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": EXPORT_SCHEMA, "source": SOURCE, "decisions": rows}
    fd, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        tmp.replace(path)
    except BaseException:
        # 실패해도 찌꺼기를 남기지 않는다 — 남으면 다음 사람이 그것을 기록으로 읽는다.
        tmp.unlink(missing_ok=True)
        raise
    return len(rows)


def load_decisions(path: Path) -> list | None:
    """결정 파일 하나를 읽는다. **못 읽으면 `None`** — 빈 목록과 구별해야 한다.

    없는 파일은 「끝낸 것이 없다」이고 그건 읽은 것이다. 형식이 다른 파일은
    「결정 없음」이 아니라 **「못 읽음」**이다 — 미래 형식을 짐작해 읽으면 검토를
    잘못 생략한다.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != EXPORT_SCHEMA:
        return None
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        return None
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        state = str(raw.get("state") or "")
        if state not in {"approved", "rejected", "deferred"}:
            continue
        out.append({
            "workspace": str(raw.get("workspace") or ""),
            "channel_id": str(raw.get("channel_id") or ""),
            "evidence_locator": str(raw.get("evidence_locator") or ""),
            "evidence_hash": str(raw.get("evidence_hash") or ""),
            "state": state,
        })
    return out


def counterpart(root: Path | str, *, workspace: str, source: str = SOURCE) -> list | None:
    """**상대편**이 끝낸 결정 전부. 하나라도 못 읽으면 `None`.

    자기 파일은 뺀다 — 안 빼면 자기 결정으로 자기 목록을 비운다.

    **못 읽은 파일이 하나라도 있으면 전부 `None` 이다.** 읽은 것만으로 판정하면,
    상대편의 「빼」 기록이 깨진 날 그 건이 「결정 없음」으로 보여 다른 파일의 승인으로
    판정이 넘어갈 수 있다.
    """
    mine = export_path(root, workspace=workspace, source=source)
    folder = mine.parent
    if not folder.is_dir():
        return []
    out: list = []
    for path in sorted(folder.glob("*.json")):
        if path.name == mine.name:
            continue
        rows = load_decisions(path)
        if rows is None:
            return None
        out.extend(rows)
    return out


# 생략하지 않는 사유. TYBot 쪽 `summary_review_reconcile` 과 같은 말을 쓴다.
SKIP = "skip"
RECORDS_UNREADABLE = "records_unreadable"
NO_MATCH = "no_match"
NOT_FINAL = "not_final"
SOURCE_MISMATCH = "source_mismatch"
EVIDENCE_CHANGED = "evidence_changed"
NO_COORDINATE = "no_coordinate"
CONFLICT = "conflict"
FINAL_STATES = {"approved", "rejected"}


def decide(*, workspace: str, channel_id: str, locator: str, digest: str,
           decisions: list | None) -> tuple:
    """이 항목을 목록에서 빼도 되는가. `(생략할까, 사유)`.

    판정의 축은 TYBot 쪽과 **같다** — 같은 좌표인가, 그 좌표의 해시가 그대로인가,
    그리고 확정된 결정인가. 보류·모순·못 읽음·좌표 없음은 전부 생략하지 않는다.
    """
    if decisions is None:
        return False, RECORDS_UNREADABLE
    if not locator or not digest:
        return False, NO_COORDINATE
    matched = [d for d in decisions if d["evidence_locator"] == locator]
    if not matched:
        return False, NO_MATCH
    same_source = [
        d for d in matched if d["workspace"] == workspace and d["channel_id"] == channel_id
    ]
    if not same_source:
        return False, SOURCE_MISMATCH
    same_evidence = [d for d in same_source if d["evidence_hash"] == digest]
    if not same_evidence:
        return False, EVIDENCE_CHANGED
    final = {d["state"] for d in same_evidence if d["state"] in FINAL_STATES}
    if not final:
        return False, NOT_FINAL
    if len(final) > 1:
        # 승인과 거절이 함께 있다. 먼저 온 것도 나중 것도 고르지 않는다 — 둘이 다른
        # 답을 냈다는 사실 자체가 사람이 봐야 할 신호다.
        return False, CONFLICT
    return True, SKIP


# ── CLI ────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="archive-inbox 결정을 공통 모양으로 내보낸다")
    ap.add_argument("--show", action="store_true", help="세어 보고만 한다 (쓰지 않는다)")
    args = ap.parse_args(argv)

    workspace = workspace_label()
    if not workspace:
        print("! config.json 의 workspace 가 비어 있어 내보낼 수 없습니다.", file=sys.stderr)
        print("  빈 워크스페이스로 적으면 받는 쪽에서 아무 결정에도 안 걸립니다 —"
              " 그 상태는 「상대편이 아무것도 안 했다」와 화면에서 같아 보입니다.",
              file=sys.stderr)
        return 1

    root = export_root()
    if root is None and not args.show:
        print(f"! {EXPORT_DIR_ENV} 가 설정되지 않아 내보낼 자리가 없습니다.", file=sys.stderr)
        return 1

    state = R._read_json(R.STATE) or {}
    try:
        # `counterpart_filter=False` — 내보낼 기록을 자기 필터로 지우면 안 된다.
        items = R.load_items(counterpart_filter=False)
    except R.PendingWorkUnreadable as exc:
        # 목록을 못 읽으면 **좌표 없는 옛 기록을 지금 재어 볼 수가 없다.** 이미 좌표가
        # 적힌 기록은 그대로 내보낼 수 있으므로 멈추지 않고, 못 읽었다는 사실만 적는다.
        print(f"! 할 일 목록을 읽지 못했습니다 — {exc}", file=sys.stderr)
        print("  좌표가 이미 적힌 결정만 내보냅니다.", file=sys.stderr)
        items = []
    items_by_id = {it.get("id"): it for it in items}
    rows, skipped = records(state, workspace=workspace, items_by_id=items_by_id)

    print(f"내보낼 결정 {len(rows)}건 (워크스페이스 {workspace})")
    if skipped:
        print("좌표를 못 내 빠진 것:")
        for why, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
            print(f"  {count}건 — {why}")
        print("  이 건들은 받는 쪽에서 **다시 묻습니다.** 틀린 좌표보다 다시 묻는 쪽이 낫습니다.")
    if args.show:
        return 0

    path = export_path(root, workspace=workspace)
    try:
        write(path, rows)
    except OSError as exc:
        print(f"! 내보내기 실패 — {path}\n  {exc}", file=sys.stderr)
        return 1
    print(f"썼습니다: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
