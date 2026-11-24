"""설치된 systemd 유닛이 저장소(`deploy/`) 판과 같은지 대조한다 (B-99).

## 왜 필요한가
코드는 `update.sh` 로 바뀌는데 유닛 파일은 `install.sh` 를 다시 돌려야 바뀐다. 그 단계가
빠지면 **새 코드가 옛 유닛 아래에서** 돈다.

2026-11-20 실측: Archiver 유닛의 `ReadWritePaths` 에 이미지 알림 대기열
(`/var/lib/tybot/image-description-notify`)이 빠져 있었다. 코드는 그 폴더에 쓰려 했고
`ProtectSystem=strict` 가 막았으며, 알림 루프는 그 실패를 삼켜 **로그 한 줄 없이** 알림이
안 나갔다. 저장소의 유닛 파일에는 그 경로가 이미 있었다.

## 고치지 않는다
다른 것만 말한다. 유닛을 바꾸는 것은 사람이 `install.sh` 나 `install` 로 한다 — 실행 중인
인스턴스는 재시작해야 새 유닛을 쓰고, 그 시점은 운영자가 정한다.

    /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_unit_drift.py
    /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_unit_drift.py --verbose

종료 코드: `0` 일치 · `1` 다르거나 설치 안 됨 · `2` 입력·환경 오류
"""

from __future__ import annotations

import argparse
import pathlib
import sys

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

REPO_DEPLOY = pathlib.Path(__file__).resolve().parent.parent / "deploy"
SYSTEMD_DIR = pathlib.Path("/etc/systemd/system")

#: 반드시 설치돼 있어야 하는 유닛. 없으면 그 기능은 배포되지 않은 것이다.
REQUIRED = ("tybot-archiver@.service", "tybot-image-description@.service")

#: 저장소에서 빠진 옛 유닛. 설치돼 있으면 알린다 — 그 유닛으로 띄우면 옛 코드 경로를 기대한다.
#: (B-99 2단계: `tybot-archiving-shadow@` → `tybot-archiver@`)
RETIRED = ("tybot-archiving-shadow@.service",)


def _meaningful(text: str) -> list[str]:
    """주석·빈 줄을 뺀 줄. 설명만 다른 것은 차이가 아니다."""
    return [
        line.rstrip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    ]


def compare(deploy_dir: pathlib.Path, systemd_dir: pathlib.Path) -> list[tuple[str, str]]:
    """`(유닛 이름, 상태)` 목록.

    상태: `same` · `differs` · `missing` · `not-installed` · `retired-present`.
    """
    results: list[tuple[str, str]] = []
    names = sorted(
        path.name
        for path in deploy_dir.glob("*")
        if path.suffix in (".service", ".timer", ".path") and path.is_file()
    )
    for name in names:
        installed = systemd_dir / name
        if not installed.is_file():
            results.append((name, "missing" if name in REQUIRED else "not-installed"))
            continue
        ours = _meaningful((deploy_dir / name).read_text(encoding="utf-8"))
        theirs = _meaningful(installed.read_text(encoding="utf-8"))
        results.append((name, "same" if ours == theirs else "differs"))
    for name in RETIRED:
        if (systemd_dir / name).is_file():
            results.append((name, "retired-present"))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deploy-dir", type=pathlib.Path, default=REPO_DEPLOY)
    parser.add_argument("--systemd-dir", type=pathlib.Path, default=SYSTEMD_DIR)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not args.deploy_dir.is_dir() or not args.systemd_dir.is_dir():
        print("deploy 또는 systemd 디렉터리를 찾을 수 없습니다", file=sys.stderr)
        return EXIT_ERROR

    results = compare(args.deploy_dir, args.systemd_dir)
    flagged = ("differs", "missing", "retired-present")
    bad = [(name, state) for name, state in results if state in flagged]
    for name, state in results:
        if args.verbose or state in flagged:
            print(f"{state:13} {name}")
    if bad:
        print(
            f"\n{len(bad)}개 유닛이 저장소와 다릅니다. "
            "`sudo install -m 0644 /opt/tybot/deploy/<유닛> /etc/systemd/system/<유닛>` → "
            "`sudo systemctl daemon-reload` → 해당 인스턴스 재시작. "
            "`retired-present` 는 옛 유닛이다 — 인스턴스를 멈추고 새 유닛으로 띄운 뒤 파일을 지운다."
        )
        return EXIT_DRIFT
    print("설치된 유닛이 저장소와 같습니다.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
