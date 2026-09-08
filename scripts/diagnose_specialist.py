#!/usr/bin/env python3
"""전문 봇이 왜 답하지 않는지 판정한다.

    python scripts/diagnose_specialist.py

## 왜 필요한가

2026-09-08 실측: 콘솔의 「최근 호출」 이 `hermes · 신뢰도 95% · 마스터 폴백 /
adapter-error` 를 보였다. **라우팅은 맞았다** — 전문가를 골랐고 확신도 높았다.
실패는 그 다음, 전문가를 실제로 부르는 자리였다. 그런데 `adapter-error` 는
서로 완전히 다른 원인 셋을 한 덩어리로 뭉갠 이름이었다.

| 원인 | 사람이 할 일 |
|---|---|
| 모델 이름이 게이트웨이 레지스트리에 없다 | 콘솔에서 모델을 고쳐 등록 |
| 그 모델의 프로바이더가 안 붙어 있다(키·SDK) | 서버에 키를 넣는다 |
| 전문가 프롬프트가 없다 | 콘솔에 규칙을 넣거나 프롬프트 파일 배포 |
| 모델이 오류를 냈다 | 다시 시도 · 상태 확인 |

**$0.000 · 500ms 이하**로 실패했다면 API 를 부르기도 전에 터진 것이다 — 위의
앞 세 가지다. 이 스크립트는 그 셋을 갈라 보인다.

## 아무것도 부르지 않는다

모델을 호출하지 않는다(비용 0). 설정이 서로 맞물리는지만 본다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tybot import specialist_adapters, specialist_router
from tybot.envfile import load_env_file
from tybot.gateway.base import Sensitivity
from tybot.gateway.router import DEFAULT_REGISTRY


def _workspaces(explicit: str) -> list[str]:
    """볼 워크스페이스. 전문가 등록은 **워크스페이스마다** 다르다.

    하나만 보고 「등록됨」 이라 결론 내면, 정작 질문이 오는 워크스페이스에는
    등록이 안 돼 있는 경우를 놓친다.
    """
    if explicit:
        return [explicit]
    try:
        from tybot.workspaces import load_workspaces

        return [c.key for c in load_workspaces()]
    except Exception as exc:  # noqa: BLE001 - 설정이 없으면 그 사실이 답이다
        print(f"  워크스페이스 설정을 읽지 못했습니다: {type(exc).__name__}: {exc}")
        return []


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="전문 봇이 왜 답하지 않는지 판정한다")
    ap.add_argument("--workspace", default="", help="한 워크스페이스만 본다")
    ap.add_argument(
        "--probe", action="store_true",
        help="그 모델로 **최소 호출 한 번**을 실제로 해 본다(사내 근거는 안 보낸다). "
             "400/403 의 실제 이유가 여기서 그대로 나온다.",
    )
    args = ap.parse_args(argv)

    load_env_file()

    print("=== 게이트웨이가 아는 모델")
    print("  콘솔에서 고를 수 있는 값은 **이 목록뿐**이다. 밖의 값을 넣으면")
    print("  호출 직전에 `unknown-model` 로 터지고 마스터가 답한다.")
    try:
        from tybot.gateway.providers import build_default_providers

        providers = set(build_default_providers())
    except Exception as exc:  # noqa: BLE001 - 키가 없어도 목록은 보여야 한다
        print(f"  프로바이더 구성 실패: {type(exc).__name__}: {exc}")
        providers = set()

    for name, spec in sorted(DEFAULT_REGISTRY.items()):
        mark = "OK  " if spec.provider in providers else "프로바이더 없음"
        conf = "" if spec.max_sensitivity == Sensitivity.CONFIDENTIAL else \
            f"  (민감도 최대 {spec.max_sensitivity.value} — 사내 근거는 못 싣는다)"
        print(f"  {mark} {name} [{spec.provider}]{conf}")

    print()
    print("=== 프롬프트가 배포된 전문가 (저장소 파일)")
    keys = sorted(specialist_adapters.available_keys())
    print(f"  {', '.join(keys) if keys else '(없음)'}")

    problems = 0
    found = 0
    for workspace in _workspaces(args.workspace):
        print()
        print(f"=== 등록된 전문가 — 워크스페이스 {workspace}")
        # `available()` 은 읽기 실패를 **빈 목록**으로 돌려준다(막는 쪽이 기본값).
        # 그래서 「등록 없음」 과 「DB 를 못 읽음」 이 겉으로 같다. 캐시를 비우고
        # 한 번 더 부르는 것으로는 안 갈리므로, 그 사실을 문구로 말한다.
        rows = specialist_router.available(workspace)
        if not rows:
            print("  (없음) — 콘솔에서 등록·승인해야 라우팅 후보가 된다.")
            print("  DB 를 읽지 못한 경우도 같은 모양으로 보인다:")
            print("    journalctl -u tybot | grep '전문가 목록'")
            continue
        found += len(rows)
        problems += _check_rows(rows, providers, probe=args.probe)

    print()
    print("=== 판정")
    if not found:
        print("  등록된 전문가가 없다. 라우팅이 전문가를 골라도 부를 대상이 없어")
        print("  마스터가 답한다(콘솔에는 `no-adapter` 로 기록된다).")
        return 0
    if problems:
        print(f"  막힌 설정 {problems}건. 위의 🔴 를 고치면 전문가가 답한다.")
        print("  고치기 전까지는 마스터가 답하고, 콘솔에는 폴백으로 기록된다.")
    else:
        print("  설정은 맞물려 있다. 그래도 폴백이 나오면 콘솔의 오류 코드를 본다:")
        print("    unknown-model / model-not-allowed / cost-limit → 설정")
        print("    timeout / invalid-output → 모델 응답")
        print("    adapter-error → journalctl -u tybot | grep '전문가 호출 실패'")
    return 0


def _check_rows(rows, providers: set[str], *, probe: bool = False) -> int:
    """전문가 한 줄씩 설정을 맞춰 본다. 막힌 건수를 돌려준다."""
    problems = 0
    for row in rows:
        print()
        print(f"  [{row.key}] 모델={row.model or '(게이트웨이 기본)'}")
        print(f"    최소 신뢰도 {row.min_confidence} · 어댑터 {row.adapter}")

        # 1) 규칙이 있는가
        try:
            specialist_adapters.build(row.key, None, model=row.model, rules=row.rules)
            source = "콘솔" if (row.rules or "").strip() else "파일"
            print(f"    규칙: OK ({source})")
        except specialist_adapters.AdapterError as exc:
            problems += 1
            print(f"    규칙: 🔴 {exc}")
            print("      → 콘솔에서 답변 규칙을 넣거나 프롬프트 파일을 배포한다.")

        # 2) 모델을 게이트웨이가 풀 수 있는가
        if not row.model:
            print("    모델: OK (기본 모델을 쓴다)")
            continue
        spec = DEFAULT_REGISTRY.get(row.model)
        if spec is None:
            problems += 1
            print("    모델: 🔴 레지스트리에 없다 — `unknown-model` 로 터진다")
            print("      → 위 목록의 이름으로 콘솔에서 바꾼다.")
            near = [n for n in DEFAULT_REGISTRY if n.split("-")[1:2] == row.model.split("-")[1:2]]
            if near:
                print(f"      비슷한 이름: {', '.join(sorted(near))}")
        elif spec.provider not in providers:
            problems += 1
            print(f"    모델: 🔴 프로바이더 `{spec.provider}` 가 안 붙어 있다")
            print("      → 서버에 그 벤더 API 키를 넣는다(콘솔 환경변수).")
        elif spec.max_sensitivity.rank() < Sensitivity.CONFIDENTIAL.rank():
            problems += 1
            print(f"    모델: 🔴 민감도 상한이 `{spec.max_sensitivity.value}` 다")
            print("      전문가에게는 사내 근거가 실린다 — `model-not-allowed` 로 터진다.")
            print("      → confidential 을 허용하는 모델로 바꾼다.")
        else:
            print(f"    모델: OK ({spec.provider})")
        if probe and spec is not None:
            _probe(row.model)
    return problems


def _request_shape() -> str:
    """**이 서버에 깔린 코드**가 요청을 어떤 모양으로 만드는지 본다.

    2026-09-08: `system` 을 배열로 보내도록 고친 뒤에도 같은 400 이 났다.
    원인은 코드가 아니라 **배포**였다 — 진단 로그 개선분은 올라갔고 그 뒤 커밋인
    수정분은 안 올라갔다. 그런데 진단은 그 차이를 말할 방법이 없어서, 같은 400 을
    한 번 더 보고서야 알았다.

    벤더를 부르지 않고 확인한다. 가짜 클라이언트에 넣어 보고 모양만 읽는다.
    """
    import contextlib

    from tybot.gateway.base import Message, ModelSpec, Sensitivity
    from tybot.gateway.providers.anthropic_provider import AnthropicProvider

    captured: dict = {}

    class _Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            raise _Captured

    class _Client:
        messages = _Messages()

    provider = AnthropicProvider(api_key="probe")
    provider._client = _Client()
    spec = ModelSpec("probe", "anthropic", 0.0, 0.0, Sensitivity.CONFIDENTIAL)

    def _shape(messages) -> object:
        captured.clear()
        with contextlib.suppress(_Captured):
            provider.complete(spec, messages, max_tokens=8)
        return captured

    try:
        with_system = _shape(
            [Message("system", "규칙"), Message("user", "질문")]
        ).copy()
        without = _shape([Message("user", "질문")]).copy()
    except Exception as exc:  # noqa: BLE001 - 모양을 못 읽으면 그 사실이 답이다
        return f"확인 불가 ({type(exc).__name__}: {exc})"

    system = with_system.get("system")
    if not isinstance(system, list):
        return (
            f"🔴 system 을 {type(system).__name__} 으로 보낸다 — "
            "`system: Input should be a valid array` 로 400 이 난다"
        )
    # **없을 때 `None` 을 보내는 것도 같은 문구로 거부당한다.** SDK 기본값은
    # `Omit` 인데 `None` 을 넣으면 그 기본값을 덮어 `"system": null` 이 나간다.
    if "system" in without:
        return (
            f"🔴 system 이 없을 때 {type(without['system']).__name__} 을 실어 보낸다 — "
            "같은 400 이 난다. 키를 빼야 한다"
        )
    return "OK (배열로 보내고, 없을 때는 키를 뺀다)"


def _loaded_from() -> list[str]:
    """**실행 중인 인터프리터가 실제로 읽은 파일**을 보고한다.

    2026-09-08: 배포된 커밋에는 수정이 들어 있는데 동작은 옛 것이었다.
    「저장소에 있다」 와 「이 프로세스가 그것을 읽었다」 는 다른 사실이고,
    그 둘이 갈리면 소스를 몇 번 봐도 답이 안 나온다.

    갈릴 수 있는 자리 셋: `sys.path` 우선순위(src vs site-packages),
    남아 있는 `__pycache__`, 그리고 SDK 버전.
    """
    import inspect

    from tybot.gateway.providers import anthropic_provider as mod

    out: list[str] = []
    path = getattr(mod, "__file__", "?")
    out.append(f"읽은 파일: {path}")
    if "site-packages" in str(path):
        out.append(
            "  🔴 저장소가 아니라 **설치된 패키지**를 읽었다. 배포는 src/ 를 "
            "갱신하지만 이 경로는 pip 가 갱신한다 — 재설치가 필요하다:"
        )
        out.append("     sudo /opt/tybot/.venv/bin/pip install -e /opt/tybot")

    cached = getattr(mod, "__cached__", "")
    if cached and Path(cached).is_file():
        src_mtime = Path(path).stat().st_mtime if Path(path).is_file() else 0
        pyc_mtime = Path(cached).stat().st_mtime
        if pyc_mtime < src_mtime:
            out.append(f"  (바이트코드가 소스보다 오래됨: {cached})")

    try:
        source = inspect.getsource(mod.AnthropicProvider.complete)
        has_array = '"type": "text"' in source
        out.append(f"  system 배열 수정 포함: {'예' if has_array else '🔴 아니오'}")
        if not has_array:
            out.append("  → 이 파일이 옛 것이다. 이 경로를 갱신해야 한다.")
    except OSError:
        out.append("  (소스를 읽지 못했다 — 바이트코드만 배포된 상태일 수 있다)")

    try:
        import anthropic

        out.append(f"anthropic SDK: {getattr(anthropic, '__version__', '?')}")
    except ImportError:
        out.append("anthropic SDK: 🔴 미설치")
    return out


class _Captured(Exception):
    """가짜 클라이언트가 요청을 잡았다는 신호. 실제 호출은 하지 않는다."""


def _probe(model: str) -> None:
    """그 모델로 최소 호출 한 번. **사내 근거는 싣지 않는다.**

    설정이 다 맞는데도 폴백이 나오는 경우가 있다 — 벤더가 그 모델에 400/403 을
    돌려주는 것이다. 이유는 응답 본문에 있고, 그것을 보는 가장 짧은 길이 이 호출이다.
    질문은 상수 한 줄이라 비용이 사실상 0 이고, 아카이브 내용이 나가지 않는다.
    """
    from tybot.gateway.base import Message, Sensitivity, error_reason
    from tybot.gateway.router import Router

    # 벤더를 부르기 전에 **우리 코드 모양**을 본다. 배포가 뒤처져 있으면 같은
    # 400 을 또 보게 되는데, 그건 조사 시간을 두 배로 쓰는 일이다.
    shape = _request_shape()
    print(f"      요청 모양: {shape}")
    for line in _loaded_from():
        print(f"      {line}")
    if shape.startswith("🔴"):
        print("      → 벤더를 부를 필요가 없다. 위의 「읽은 파일」 을 갱신해야 한다.")
        print("        배포된 커밋: cat /opt/tybot/.deployed-commit")
        return

    try:
        router = Router.from_default_registry()
    except Exception as exc:  # noqa: BLE001 - 구성 실패도 답이다
        print(f"      호출 시험: 🔴 라우터를 만들지 못했다 — {error_reason(exc)}")
        return
    try:
        resp = router.complete(
            # **시스템 프롬프트를 함께 보낸다.** 전문가 경로는 항상 그렇게 부르고,
            # 형식 문제는 거기서 난다. 없이 시험하면 다른 것을 시험한 셈이 되고,
            # 실제로 그렇게 한 번 헛돌았다(2026-09-08).
            [Message("system", "짧게 답한다."), Message("user", "1+1은?")],
            model=model,
            sensitivity=Sensitivity.CONFIDENTIAL,
            max_tokens=16,
        )
    except Exception as exc:  # noqa: BLE001 - 이유를 그대로 보여 주는 것이 목적이다
        print(f"      호출 시험: 🔴 {error_reason(exc)}")
        print("      → 이 줄이 폴백의 실제 이유다.")
        print("        `not_found_error` · 모델 이름 → 콘솔에서 모델을 바꾼다")
        print("        `authentication_error` → API 키를 확인한다")
        print("        그 밖의 `invalid_request_error` → 요청 형태 문제다.")
        print("        벤더가 형식을 조이면 **모델을 바꾼 전문가만** 조용히 폴백한다")
        print("        (2026-09-08: `system` 을 문자열로 보내 opus 만 400 이었다).")
        return
    print(f"      호출 시험: OK ({resp.model} · ${resp.cost_usd:.6f})")


if __name__ == "__main__":
    raise SystemExit(main())
