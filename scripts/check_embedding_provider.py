#!/usr/bin/env python3
"""임베딩 프로바이더가 **실제로** 약속한 모양으로 답하는지 한 번 확인한다.

B-12 게이트웨이 경계는 가짜 SDK 로만 검증돼 있다. 진짜 API 가 같은 모양으로
답하는지는 아무도 안 봤다 — 응답 필드 이름 하나가 달라도 첫 색인 실행에서야
드러나고, 그때는 이미 돈이 나간 뒤다. 이 도구가 그 한 번을 미리 친다.

**기본은 계획 모드다.** 무엇을 보낼지만 적고 네트워크도 SDK 도 건드리지 않는다.
실제 호출은 `--execute` 가 있어야만 한다.

    # 계획만 (네트워크 0회)
    python scripts/check_embedding_provider.py \\
        --provider openai --model text-embedding-3-small --dimensions 1536

    # 실제 호출 1회
    ... --execute

진단 스크립트이므로 콘솔 규칙의 예외다(CLAUDE.md 「운영 기능은 콘솔에 둔다」).

## 무엇을 안 읽나

아카이브·QA 로그·DB 원문을 **하나도 안 읽는다.** 보내는 것은 아래에 박아 둔
합성 문장 세 개뿐이고, 그 본문은 출력에도 안 나온다. 모델 레지스트리도 안 본다 —
provider·model·dimensions 를 사람이 명시해야 한다. 그래야 「무엇을 어디로
보내는지」 가 명령줄에 그대로 보인다.
"""
from __future__ import annotations

import argparse
import math
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot.embedding_gateway import (
    EmbeddingBinding,
    GatewayEmbedder,
)
from tybot.embedding_index_job import (
    EMBEDDING_FAILURE_OUTCOMES,
    EmbeddingFailure,
    EmbeddingInput,
)

#: 이 도구가 스스로 내는 거절. 경계가 내는 코드와 **섞이지 않게** 접두어를 다르게
#: 둔다 — 「프로바이더가 거절했다」 와 「여기까지 오지도 못했다」 는 조치가 다르다.
SMOKE_FAILURE_CODES = frozenset(
    {
        "smoke_api_key_missing",
        "smoke_provider_unsupported",
        "smoke_arguments_invalid",
    }
)

#: 보낼 문장. **코드에 박아 둔다.** 파일이나 DB 에서 읽어 오는 순간 이 도구는
#: 「아무 글이나 외부로 보내는 길」 이 되고, 그 길은 한 번 열리면 닫기 어렵다.
#: 회사 정보·사람 이름·숫자가 없는 문장만 쓴다.
SYNTHETIC_INPUTS = (
    "This sentence exists only to measure one embedding call.",
    "Rain is expected on the north side of the valley tomorrow.",
    "이 문장은 임베딩 호출의 모양을 재어 보기 위한 것이다.",
)

#: 스모크가 아는 어댑터. 레지스트리가 아니라 **이 도구가 띄울 수 있는 것**의
#: 목록이다. 여기 없는 프로바이더는 임베딩 API 가 없다는 뜻이고, completion 으로
#: 우회하지 않는다.
SMOKE_PROVIDERS = ("openai",)

#: 레지스트리를 안 보므로 revision 도 가짜다. 색인에 쓰이지 않는다.
SMOKE_REVISION = "smoke-check"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class SmokeRefusal(RuntimeError):
    """이 도구가 호출 전에 멈춘 이유. `str()` 은 고정 낱말 하나다."""

    def __init__(self, code: str) -> None:
        if code not in SMOKE_FAILURE_CODES:
            raise ValueError("unknown smoke failure code")
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self)


def build_inputs() -> tuple[EmbeddingInput, ...]:
    """경계가 요구하는 그대로 — 지문은 경계가 다시 계산해 대조한다."""
    import hashlib

    return tuple(
        EmbeddingInput(
            chunk_no=number,
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            text=text,
        )
        for number, text in enumerate(SYNTHETIC_INPUTS)
    )


def resolve_api_key(provider: str, *, source: str) -> str:
    """키를 찾는다. **값은 돌려만 주고 어디에도 안 적는다.**

    기본은 환경변수다. 스모크가 운영 비밀 경로(DB + 암호화 키 파일)에 기대면
    그 경로가 막힌 서버에서는 도구 자체가 못 돌고, 그러면 정작 보려던 것 —
    프로바이더 응답 모양 — 을 못 본다. 운영 경로까지 같이 보고 싶으면
    `--key-from secret-store` 로 명시한다.
    """
    from tybot.envfile import load_env_file

    # 두 경로 모두 env 파일을 먼저 읽는다. 비밀 저장소는 `DATABASE_URL` 과 암호화
    # 키 경로를 env 에서 찾는데, 읽지 않으면 DB 를 보지도 않고 「키 없음」 이 된다
    # (2026-11-09 서버에서 실제로 `smoke_api_key_missing` 이 그렇게 났다).
    load_env_file()
    if source == "secret-store":
        try:
            from tybot.console.llm_secret_store import resolve_key

            found = resolve_key(provider)
        except Exception:  # noqa: BLE001 - 조회 실패와 미설정을 구분하지 않는다
            found = None
    else:
        found = os.getenv(f"{provider.upper()}_API_KEY")
    if not found or not str(found).strip():
        raise SmokeRefusal("smoke_api_key_missing")
    return str(found)


def build_provider(name: str, *, key_from: str):
    """어댑터 하나를 띄운다. SDK 는 어댑터 안에서 lazy import 된다."""
    if name not in SMOKE_PROVIDERS:
        raise SmokeRefusal("smoke_provider_unsupported")
    api_key = resolve_api_key(name, source=key_from)
    from tybot.gateway.providers.openai_embeddings import OpenAIEmbeddingProvider

    return OpenAIEmbeddingProvider(api_key=api_key)


def plan(args) -> dict:
    """무엇을 보낼지만 적는다. 네트워크도 SDK 도 건드리지 않는다."""
    return {
        "mode": "plan",
        "provider_requested": args.provider,
        "model_requested": args.model,
        "dimensions_requested": args.dimensions,
        "timeout_seconds": args.timeout,
        "inputs": len(SYNTHETIC_INPUTS),
        "key_source": args.key_from,
        "key_checked": False,
        "calls_made": 0,
        "result": "planned",
        "note": "--execute 를 붙이면 실제로 1회 호출한다",
    }


def execute(args) -> dict:
    """정확히 한 번 부른다. 재시도도 폴백도 없다."""
    provider = build_provider(args.provider, key_from=args.key_from)
    binding = EmbeddingBinding(
        revision=SMOKE_REVISION,
        provider=args.provider,
        model=args.model,
        dimensions=args.dimensions,
        state="active",
        input_price_per_mtok=args.price_per_mtok,
    )
    embedder = GatewayEmbedder(
        bindings=lambda revision: binding if revision == SMOKE_REVISION else None,
        providers={args.provider: provider},
        timeout_seconds=args.timeout,
    )
    batch = embedder.embed(
        build_inputs(),
        model_revision=SMOKE_REVISION,
        policy_revision="smoke-check:manual",
    )
    widths = sorted({len(vector) for vector in batch.vectors})
    return {
        "mode": "execute",
        "provider_requested": args.provider,
        "model_requested": args.model,
        "dimensions_requested": args.dimensions,
        "timeout_seconds": args.timeout,
        "inputs": len(SYNTHETIC_INPUTS),
        "key_source": args.key_from,
        "key_checked": True,
        "calls_made": 1,
        # 경계를 통과했다는 것 자체가 provider·model 일치의 증거다. 안 맞으면
        # `embedding_identity_mismatch` 로 여기 못 온다.
        "identity_matched": True,
        "vectors_returned": len(batch.vectors),
        "vector_widths": widths,
        "cost_confirmed": batch.cost_usd is not None,
        "cost_usd": batch.cost_usd,
        "result": "ok",
    }


def render(report: dict) -> str:
    lines = []
    for key, value in report.items():
        lines.append(f"{key:22} {value}")
    return "\n".join(lines)


def checked_arguments(args) -> None:
    if (
        isinstance(args.dimensions, bool)
        or not isinstance(args.dimensions, int)
        or not 1 <= args.dimensions <= 16_000
    ):
        raise SmokeRefusal("smoke_arguments_invalid")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise SmokeRefusal("smoke_arguments_invalid")
    for value in (args.provider, args.model):
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise SmokeRefusal("smoke_arguments_invalid")
    if args.price_per_mtok is not None and (
        not math.isfinite(args.price_per_mtok) or args.price_per_mtok < 0
    ):
        raise SmokeRefusal("smoke_arguments_invalid")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="임베딩 프로바이더 응답 모양을 1회 확인한다(기본은 계획 모드)",
    )
    parser.add_argument("--provider", required=True, help="예: openai")
    parser.add_argument("--model", required=True, help="프로바이더가 아는 모델 이름")
    parser.add_argument(
        "--dimensions", required=True, type=int, help="기대하는 벡터 차원"
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--price-per-mtok",
        type=float,
        default=None,
        dest="price_per_mtok",
        help="100만 토큰당 USD. 없으면 비용은 '확인 불가' 로 남는다(0 이 아니다)",
    )
    parser.add_argument(
        "--key-from",
        choices=("env", "secret-store"),
        default="env",
        dest="key_from",
        help="env: <PROVIDER>_API_KEY · secret-store: 운영 비밀 저장소",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="실제로 1회 호출한다. 없으면 아무것도 안 보낸다",
    )
    parser.add_argument("--json", action="store_true", help="JSON 한 줄로 출력")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        checked_arguments(args)
        report = execute(args) if args.execute else plan(args)
    except (SmokeRefusal, EmbeddingFailure) as exc:
        # 고정 낱말 하나만 나간다. 프로바이더 오류 본문도, 보낸 문장도,
        # 키도 여기에 없다.
        code = str(exc)
        report = {
            "mode": "execute" if args.execute else "plan",
            "provider_requested": args.provider,
            "model_requested": args.model,
            "dimensions_requested": args.dimensions,
            "result": "failed",
            "code": code,
            "code_known": code in (EMBEDDING_FAILURE_OUTCOMES | SMOKE_FAILURE_CODES),
        }
    if args.json:
        import json

        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(render(report))
    return 0 if report.get("result") in {"ok", "planned"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
