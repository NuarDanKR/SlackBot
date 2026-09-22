#!/usr/bin/env python3
"""프금팀이 쓴 `health.json` 이 계약을 지키는지 본다.

    sudo /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_pf_health.py \\
        /var/lib/tybot-subbots/pf-hermes/health.json

근거: `docs/design/pf-hermes-owner-plan.md` §7.4,
      `docs/design/pf-hermes-migration-handoff.md` §8

## 왜 필요한가
`/pf/` 화면은 계약 밖의 값을 **읽지 않는다**(allowlist). 그래서 상태 파일에 토큰이
들어와도 화면은 조용하다 — 조용한 것이 목적이었으니 맞다. 그런데 그러면 **계약이
깨진 것을 아무도 모른다.**

화면은 새는 것을 막고, 이 도구는 **깨진 것을 말한다.** 둘은 다른 일이다.

이관 당일(runbook 10:40 단계)과 그 뒤 프금팀 release 를 받을 때마다 돌린다.

## 무엇을 보나
1. 계약 항목이 다 있나 — 없으면 화면이 「정상」 이라고 칠할 근거가 없다
2. 계약에 **없는 칸**이 있나 — 토큰·질문·본문이 섞여 들어왔을 수 있다
3. 값이 시크릿처럼 생겼나 — 이름이 무해해도 값이 토큰이면 잡는다
4. 오류 코드가 정규화돼 있나 — 예외 메시지를 코드 자리에 넣지 않았나
5. 시각이 읽히나, 기록이 최신인가

값 자체는 **출력하지 않는다.** 이 도구의 출력이 새 유출 경로가 되면 안 된다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot_pf import health  # noqa: E402

# 값이 이렇게 생겼으면 시크릿이다. 칸 이름이 무해해도 잡는다 — 이름은 바꿔 붙일 수
# 있지만 값의 모양은 못 숨긴다.
SECRET_SHAPES = (
    (re.compile(r"^xox[bapsr]-"), "Slack 토큰"),
    (re.compile(r"^xapp-"), "Slack 앱 토큰"),
    (re.compile(r"^sk-ant-"), "Anthropic 키"),
    (re.compile(r"^gh[pousr]_"), "GitHub 토큰"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"), "개인 키"),
    (re.compile(r"^[A-Za-z0-9+/]{60,}={0,2}$"), "base64 로 보이는 긴 값"),
)

# 사람이 말한 문장처럼 보이는 긴 값. 질문·답변·문서 본문이 들어온 경우다.
PROSE_MIN = 200


def _shape(value: object) -> str:
    """값의 모양만 말한다. **값은 싣지 않는다.**"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return f"{type(value).__name__}"
    if isinstance(value, str):
        return f"문자열 {len(value)}자"
    if isinstance(value, list):
        return f"목록 {len(value)}개"
    if isinstance(value, dict):
        return f"객체 {len(value)}칸"
    return type(value).__name__


def _secret_kind(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    for pattern, label in SECRET_SHAPES:
        if pattern.search(text):
            return label
    return ""


def inspect(data: dict, *, stale_after: int, now: dt.datetime) -> tuple[list[str], list[str]]:
    """(막아야 할 것, 살펴볼 것). 앞이 비어야 통과다."""
    bad: list[str] = []
    warn: list[str] = []

    # 1. 계약 항목
    missing = [key for key in health.REQUIRED if key not in data]
    if missing:
        bad.append(f"계약 항목이 없습니다: {', '.join(missing)}")

    # 2. 계약에 없는 칸
    known = set(health.FIELDS) | {"errors"}
    extra = sorted(set(data) - known)
    for key in extra:
        warn.append(f"계약에 없는 칸: {key} ({_shape(data[key])})")

    # 3. 시크릿처럼 생긴 값 — 계약 안이든 밖이든 본다
    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
            return
        if isinstance(node, list):
            for index, value in enumerate(node[:50]):
                walk(value, f"{path}[{index}]")
            return
        kind = _secret_kind(node)
        if kind:
            bad.append(f"{path} 값이 {kind} 처럼 보입니다")
        elif isinstance(node, str) and len(node) >= PROSE_MIN:
            bad.append(
                f"{path} 에 {len(node)}자 문자열이 있습니다"
                " — 질문·답변·문서 본문은 상태 파일에 담지 않습니다"
            )

    walk(data, "")

    # 4. 오류 코드 정규화
    raw_errors = data.get("errors")
    if raw_errors is not None and not isinstance(raw_errors, list):
        bad.append("errors 가 목록이 아닙니다")
    elif isinstance(raw_errors, list):
        for index, item in enumerate(raw_errors):
            if not isinstance(item, dict):
                bad.append(f"errors[{index}] 가 객체가 아닙니다")
                continue
            code = str(item.get("code") or "").strip().lower()
            if code not in health.ERROR_CODES:
                bad.append(
                    f"errors[{index}].code 가 정규화되지 않았습니다"
                    f" — 허용: {', '.join(health.ERROR_CODES)}"
                )
            for key in item:
                if key not in {"code", "at"}:
                    warn.append(f"errors[{index}] 에 계약 밖 칸: {key}")

    # 5. 시각
    generated = str(data.get("generated_at") or "")
    if generated:
        try:
            when = dt.datetime.fromisoformat(generated)
        except ValueError:
            bad.append("generated_at 을 ISO 시각으로 읽지 못했습니다")
        else:
            if when.tzinfo is None:
                warn.append("generated_at 에 시간대가 없습니다 — 서버 시간대로 읽힙니다")
                when = when.replace(tzinfo=now.tzinfo)
            age = int((now - when).total_seconds())
            if age > stale_after:
                warn.append(f"마지막 기록이 {age // 60}분 전입니다(기준 {stale_after // 60}분)")
            elif age < -60:
                warn.append(f"generated_at 이 미래입니다({-age // 60}분) — 시계가 어긋났습니다")

    return bad, warn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="health.json 경로")
    parser.add_argument(
        "--stale-seconds", type=int, default=900, help="이만큼 낡으면 알린다(기본 900)"
    )
    args = parser.parse_args()

    path = Path(args.path).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"상태 파일이 없습니다: {path}")
        print("  Hermes 가 아직 안 떴거나 경로가 다릅니다. 이 자체는 오류가 아닙니다.")
        return 4
    except OSError as exc:
        print(f"상태 파일을 읽지 못했습니다({type(exc).__name__}): {path}")
        return 4

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"JSON 형식이 아닙니다: {exc.msg} (줄 {exc.lineno})")
        return 3
    if not isinstance(data, dict):
        print("최상위가 객체가 아닙니다.")
        return 3

    now = dt.datetime.now(dt.UTC)
    bad, warn = inspect(data, stale_after=args.stale_seconds, now=now)

    print(f"상태 파일: {path}  ({len(raw)} bytes, 칸 {len(data)}개)")
    print()
    if bad:
        print("■ 막아야 할 것")
        for item in bad:
            print(f"  ✗ {item}")
        print()
    if warn:
        print("■ 살펴볼 것")
        for item in warn:
            print(f"  · {item}")
        print()

    # 화면이 실제로 무엇을 보여 줄지도 같이 낸다. 계약 점검과 화면 판정이 갈리면
    # 「검사는 통과했는데 화면은 비었다」 가 되고, 그때 어느 쪽이 맞는지 모른다.
    got = health.read(
        "pf-hermes", path.parent, stale_after=args.stale_seconds, now=now
    )
    print(f"■ /pf/ 화면 판정: {got.status}" + (f" — {got.reason}" if got.reason else ""))
    print(f"  화면에 오르는 칸 {len(got.fields)}개, 오류 {len(got.errors)}건")

    if bad:
        print()
        print("계약 위반이 있습니다. 프금팀에 알리고 고쳐진 release 로 다시 확인하세요.")
        return 1
    print()
    print("계약 위반 없음." + (" 살펴볼 것은 위에 있습니다." if warn else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
