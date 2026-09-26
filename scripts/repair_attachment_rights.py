#!/usr/bin/env python3
"""첨부 정본의 **빈 ACL·잘못된 visibility 를 세고, 채널 권한으로 되돌린다.**

    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/repair_attachment_rights.py
    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/repair_attachment_rights.py --apply

기본이 dry-run 이다. `--apply` 를 붙여야 파일을 고친다.

## 왜 있나

옛 `render` 가 ACL 을 대괄호 없이 적어 파서가 주석으로 읽었다 — **ACL 이 통째로
빈 정본**이 남아 있다(`attachment_repair` 머리말). reader 는 이제 그런 문서를
거절하므로 안전하지만, 그 자료는 답변에서 **사라진다.** 사라진 것과 없는 것은
화면에서 같아 보이므로 세어서 고친다.

콘솔이 아니라 스크립트인 이유: 한 번 지나가는 소급 복구이고, 읽어서 세는 동작이
주다(운영 기능 콘솔 규칙의 진단 예외). 고친 결과는 재색인 차단 여부로 콘솔·운영
절차에 드러난다.

## 모르면 안 고친다

채널 문서를 못 찾거나 채널 권한 자체가 비었으면 고칠 값이 없다. 추측해 채우면
권한을 지어내는 것이다. 그런 건이 남아 있는 동안 `reindex_attachments.py` 는
스스로 멈춘다.
"""
from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.archive import attachment_reader, attachment_repair  # noqa: E402
from tybot.archive.store import ArchiveStore  # noqa: E402


def findings_for(store: ArchiveStore) -> list[attachment_repair.Finding]:
    channels = [
        doc for doc in store.audit_docs(dm_scope="*")
        if not attachment_reader.is_attachment_doc(doc)
    ]
    return attachment_repair.survey(store.root, channels)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--archive", default=None, help="기본: ARCHIVE_DIR")
    parser.add_argument("--apply", action="store_true", help="실제로 고친다")
    args = parser.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from tybot.envfile import load_env_file
    from tybot.paths import archive_dir

    load_env_file()
    store = ArchiveStore(args.archive or archive_dir())
    findings = findings_for(store)

    fixable = [f for f in findings if f.fixable]
    blocked = [f for f in findings if not f.fixable]
    print(f"권한 칸이 깨진 정본: {len(findings)}건")
    print(f"  · 채널 권한으로 고칠 수 있음: {len(fixable)}건")
    print(f"  · 고칠 값이 없음(사람 확인): {len(blocked)}건")
    for finding in blocked[:10]:
        print(f"      {finding.path}: {finding.problem}")
    if len(blocked) > 10:
        print(f"      … 외 {len(blocked) - 10}건")

    if not args.apply:
        print("\ndry-run 이었습니다. 실제 복구는 --apply 로 다시 실행하세요.")
        return 0

    fixed = sum(1 for finding in fixable if attachment_repair.apply_fix(finding))
    print(f"\n고친 정본: {fixed}건")
    if blocked:
        print(
            f"{len(blocked)}건은 그대로 두었습니다 — 재색인은 계속 막힙니다."
            " 채널 문서 권한을 먼저 확인하세요."
        )
        return 3
    print("본문은 건드리지 않았습니다. 프론트매터 두 칸만 바꿨습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
