"""수집 진단 — 왜 이 워크스페이스는 수집이 안 되나. 그리고 v1 을 지워도 되나.

## 왜 이 스크립트가 필요한가
아카이브 디렉터리가 있다는 것은 **수집되고 있다는 뜻이 아니다.** 워크스페이스 디렉터리는
첫 원문이 쓰일 때 생기지만, 그 뒤로 조용히 멈춰 있어도 디렉터리는 그대로 남는다.
`ls` 로는 "있다/없다" 만 보이고 "언제 멈췄나" 가 안 보인다.

그래서 세 가지를 한 화면에서 맞춰 본다.

| 확인 | 무엇이 틀어질 수 있나 |
|---|---|
| 설정된 워크스페이스 (DB/환경변수) | 앱을 안 만들었거나 토큰이 안 들어갔다 |
| 아카이브에 실제로 쌓인 원문 | 봇이 채널에 없거나 채널명이 규칙 밖이다 |
| 마지막 원문 시각 | 언젠가 됐다가 멈췄다 — 이게 `ls` 로 안 보이는 경우다 |

## v1(`channels/`) 삭제 판정
`ArchiveStore` 는 v1 평면 파일과 v2 를 **둘 다** 읽는다(store.py `_files`). v1 은 죽은
디렉터리가 아니라 살아 있는 답변 근거다. 그래서 삭제는 **v1 의 모든 원문 줄이 v2 에도
있다** 를 확인한 뒤에만 안전하다 — 마이그레이션은 비파괴 복사이므로 확인만 하면 된다.

한 줄이라도 v2 에 없으면 지우면 안 된다(원칙 1: 원문 보존). 이 스크립트는 지우지
않는다. 지워도 되는지만 답한다.

    python scripts/diagnose_collection.py --archive /var/lib/tybot/archive
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict
from pathlib import Path


def _load_workspaces() -> tuple[list, str]:
    """설정된 워크스페이스. 실패해도 진단을 멈추지 않는다 — 아카이브 쪽은 볼 수 있다."""
    try:
        from tybot.envfile import load_env_file
        from tybot.workspaces import load_workspaces

        load_env_file()
        return list(load_workspaces()), ""
    except Exception as e:  # noqa: BLE001 - 설정 오류 자체가 진단 결과다
        return [], f"{type(e).__name__}: {e}"


def _raw_key(workspace: str, line) -> tuple:
    """원문 줄의 신원. 경로가 달라도 같은 발언이면 같은 키."""
    return (workspace, line.ts, line.speaker.strip(), line.text.strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="수집 진단 · v1 삭제 가능 여부")
    ap.add_argument("--archive", default=os.getenv("ARCHIVE_DIR", "./archive"))
    args = ap.parse_args(argv)

    from tybot.archive.store import ArchiveStore

    root = Path(args.archive)
    store = ArchiveStore(root)
    docs = store.source_docs()

    # --- 1. 설정 vs 실제 -------------------------------------------------
    configured, config_error = _load_workspaces()
    if config_error:
        print(f"⚠️  워크스페이스 설정을 읽지 못했다 — {config_error}")
        print("    이것 자체가 원인일 수 있다. 봇이 뜨지 못하면 아무것도 수집되지 않는다.")
        if "복호화" in config_error:
            print("    콘솔에 토큰이 O 로 보이더라도 소용없다 — 등록 당시의 "
                  "WORKSPACE_SECRET_KEY 와 지금 키가 같은지 확인하라.")
        print()

    by_ws: dict[str, list] = defaultdict(list)
    for doc in docs:
        by_ws[doc.workspace].append(doc)

    dirs = sorted(p.name for p in (root / "workspaces").glob("*") if p.is_dir())
    keys = sorted({*(w.key for w in configured), *by_ws, *dirs})

    print("=== 워크스페이스별 수집 상태 ===")
    print(f"{'워크스페이스':<14} {'설정':<6} {'채널':>5} {'원문줄':>7}  마지막 원문")
    for key in keys:
        ws_docs = by_ws.get(key, [])
        lines = sum(len(d.raw_lines) for d in ws_docs)
        last = max((ln.ts for d in ws_docs for ln in d.raw_lines), default="")
        cfg = "있음" if any(w.key == key for w in configured) else "없음"
        print(f"{key:<14} {cfg:<6} {len(ws_docs):>5} {lines:>7}  {last or '—'}")

    for key in keys:
        ws_docs = by_ws.get(key, [])
        lines = sum(len(d.raw_lines) for d in ws_docs)
        configured_here = any(w.key == key for w in configured)
        if not configured_here:
            print(f"\n🔴 {key}: 아카이브에는 있는데 **설정에 없다.** 봇이 안 뜨고 있다 "
                  "— 콘솔 워크스페이스 등록/토큰을 확인하라.")
        elif not ws_docs:
            print(f"\n🔴 {key}: 봇은 설정됐는데 **원문이 0건이다.** 이 순서로 좁혀라 — "
                  "(1) 채널명이 표준 규칙인가 (2) 봇이 그 채널 멤버인가 "
                  "(비공개는 `/invite` 필수) (3) 앱에 message.channels 이벤트 구독과 "
                  "channels:history 스코프가 있는가.")
        elif not lines:
            print(f"\n🟡 {key}: 문서는 {len(ws_docs)}개인데 **원문 줄이 0이다.** "
                  "문서는 만들어졌고 대화만 안 쌓였다 — 이벤트 구독 누락이 가장 흔하다.")

    if not configured and not config_error:
        # 설정 오류가 이미 원인을 밝혔으면 여기서 다른 조치를 겹쳐 말하지 않는다.
        # 조치가 둘이면 담당자는 틀린 쪽을 먼저 시도한다.
        print("\n🔴 설정된 워크스페이스가 0개다. 워크스페이스마다 Slack 앱을 따로 만들고 "
              "봇/앱 토큰 두 개를 각각 등록해야 한다(docs/multi-workspace.md).")

    # --- 2. v1 삭제 판정 -------------------------------------------------
    legacy_dir = root / "channels"
    print("\n=== v1 `channels/` 삭제 가능 여부 ===")
    if not legacy_dir.is_dir():
        print("v1 디렉터리가 없다. 판정할 것이 없다.")
        return 0

    # 형식 위반 파일은 `source_docs()` 에 안 나온다. 그 파일도 원문을 담고 있으므로
    # **파싱 실패를 "원문 없음" 으로 읽으면 안 된다** — 지워도 된다는 오판이 된다.
    broken_v1 = [
        (path, why) for path, why in store.broken()
        if path.relative_to(root).parts[0] == "channels"
    ]
    v1 = [d for d in docs if d.path.relative_to(root).parts[0] == "channels"]
    v2_keys = {
        _raw_key(d.workspace, ln)
        for d in docs
        if d.path.relative_to(root).parts[0] == "workspaces"
        for ln in d.raw_lines
    }

    missing: dict[Path, int] = {}
    v1_lines = 0
    for doc in v1:
        v1_lines += len(doc.raw_lines)
        gone = sum(1 for ln in doc.raw_lines if _raw_key(doc.workspace, ln) not in v2_keys)
        if gone:
            missing[doc.path] = gone

    print(f"v1 문서 {len(v1)}개 · 원문 {v1_lines}줄")

    if broken_v1:
        print(f"🔴 지우면 안 된다. v1 파일 {len(broken_v1)}개가 형식 검사를 통과하지 못해 "
              "**내용을 확인할 수 없다.** 읽지 못한 파일을 지우는 것은 원문을 버리는 것이다:")
        for path, why in broken_v1[:20]:
            reason = why.split(": ", 1)[-1] if str(path) in why else why
            print(f"   {path.relative_to(root)} — {reason}")
        if len(broken_v1) > 20:
            print(f"   … 그 외 {len(broken_v1) - 20}개")
        print("\n조치: 프론트매터를 고쳐 검사를 통과시킨 뒤 다시 판정하라.")
        return 1

    if not v1_lines:
        print("✅ v1 에 원문 줄이 없다. 지워도 잃는 근거가 없다.")
        return 0
    if not missing:
        print("✅ v1 의 모든 원문 줄이 v2 에도 있다. `channels/` 를 지워도 근거가 줄지 않는다.")
        print("   지우기 전에 백업을 한 번 뜨는 편이 좋다 — 되돌릴 수 없는 삭제다.")
        return 0

    print(f"🔴 지우면 안 된다. v2 에 없는 원문이 {sum(missing.values())}줄 남아 있다:")
    for path, count in sorted(missing.items(), key=lambda kv: -kv[1])[:20]:
        print(f"   {count:>6}줄  {path.relative_to(root)}")
    if len(missing) > 20:
        print(f"   … 그 외 {len(missing) - 20}개 문서")
    print("\n조치: `python scripts/migrate_archive_v2.py --channel-map <검토한 JSON> --apply` "
          "로 먼저 옮기고, 이 스크립트를 다시 돌려 ✅ 를 확인한 뒤 지워라.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
