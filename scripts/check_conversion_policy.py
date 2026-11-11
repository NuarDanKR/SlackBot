#!/usr/bin/env python3
"""변환 정책 readiness — rhwp 위임과 야간 재점검을 **켜기 전에** 돌리는 읽기 전용 점검 (B-89 후속).

    sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \\
        /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_conversion_policy.py
    … --json      # 콘솔·자동화에 물린다

## 무엇을 보나

- DB 정본(`conversion_runtime_policy`)에 행이 있고 값이 바른가
- 없으면 무엇을 읽고 있나 — 옛 설정(전환 기간 fallback)인가, 아무것도 모르나
- 그래서 지금 **HWP/HWPX 직접 변환**과 **야간 apply** 가 열려 있나

## 안 하는 것

- 고치지 않는다. 행을 넣지 않고, 타이머를 켜지 않는다. 값은 콘솔에서 사람이 저장한다
- 시크릿을 출력하지 않는다. 앱 ID(공개 식별자)·숫자·출처 이름만 적는다

종료 코드: `0` DB 정본 확인(야간 apply 가능) · `3` 옛 설정으로 동작 중(전환 기간,
야간 apply 거절) · `1` 모름(직접 변환·야간 apply 모두 막힘)
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot.envfile import load_env_file

EXIT_READY = 0
EXIT_UNKNOWN = 1
EXIT_LEGACY = 3

_REASON_HINT = {
    "db_unconfigured": "DATABASE_URL 이 없습니다. TYBOT_ENV_FILE 을 넘겼는지 확인하세요.",
    "db_unavailable": "DB 에 접속하지 못했습니다. PostgreSQL 상태와 접속 정보를 확인하세요.",
    "table_missing": "표가 없거나 권한이 없습니다. `sudo /opt/tybot/deploy/apply-schema.sh` 를 실행하세요.",
    "row_missing": "정본 행이 없습니다. 콘솔 > 환경변수 설정 > 변환 정책에서 값을 저장하세요.",
    "invalid_value": "DB 값이 틀렸습니다(손으로 고친 흔적). 콘솔에서 다시 저장하세요.",
}


def exit_code(policy) -> int:
    if policy.nightly_apply_allowed:
        return EXIT_READY
    return EXIT_LEGACY if policy.known else EXIT_UNKNOWN


def report(policy) -> dict:
    return {
        "policy": policy.to_json(),
        "ready": policy.nightly_apply_allowed,
        "hint": _REASON_HINT.get(policy.reason, ""),
        "exitCode": exit_code(policy),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="JSON 한 줄로 출력한다")
    args = ap.parse_args(argv)
    # 다른 운영 스크립트와 같은 순서로 설정을 읽는다. DATABASE_URL 이 여기서 온다.
    load_env_file()

    from tybot import conversion_policy

    policy = conversion_policy.resolve()
    result = report(policy)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return result["exitCode"]

    data = result["policy"]
    print(f"상태: {data['state']}" + (f" (사유 {data['reason']})" if data["reason"] else ""))
    print(f"rhwp 위임: {data['delegation']} · 앱 ID {', '.join(data['appIds']) or '없음'}")
    print(
        f"한 밤 소급 상한: {data['recheckLimit'] if data['recheckLimit'] is not None else '모름'}"
        f" · rhwp 대기: {data['rhwpGraceHours'] if data['rhwpGraceHours'] is not None else '모름'}시간"
    )
    print("출처: " + ", ".join(f"{k}={v}" for k, v in sorted(data["sources"].items())))
    print(f"정본 판: {data['revision'] or '-'}")
    print(f"HWP/HWPX 직접 변환: {'허용' if data['hwpDirectConversion'] else '막힘'}")
    print(f"야간 apply: {'가능' if data['nightlyApplyAllowed'] else '거절'}")
    if result["hint"]:
        print(f"할 일: {result['hint']}")
    if policy.state == conversion_policy.STATE_LEGACY:
        print("할 일: 옛 설정으로 동작 중입니다. 콘솔에서 같은 값을 저장해 DB 정본으로 옮기세요.")
    if result["ready"]:
        print(
            "야간 타이머는 이 점검이 끝났다고 저절로 켜지지 않습니다. 확인 뒤 사람이 켭니다:\n"
            "  sudo systemctl enable --now tybot-convert-nightly.timer"
        )
    return result["exitCode"]


if __name__ == "__main__":
    raise SystemExit(main())
