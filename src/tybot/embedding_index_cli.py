"""B-12 색인 실행 진입점 — 타이머·콘솔·사람이 같은 문으로 들어온다.

    python -m tybot.embedding_index_cli                       # 계획만. 모델 호출 0, DB 쓰기 0
    python -m tybot.embedding_index_cli --execute --trigger timer

## 왜 이 파일이 필요한가

색인의 모든 조각(정본 인벤토리 → 정책 판정 → 임베딩 1회 → 원자적 발행 →
좌표 이동 retire)은 `embedding_index_runtime.run_embedding_index` 하나로 이미
묶여 있다. 그런데 **그것을 부르는 곳이 없었다.** 그래서 운영 DB 의 벡터 표는
스키마만 있고 0행이었다(2026-11-09 실측).

이 모듈은 그 한 번의 호출을 운영에서 돌릴 수 있게 감싼다. 판정·발행 규칙은
**하나도 새로 쓰지 않는다.** 여기 있는 것은 셋뿐이다.

1. **무엇을 돌릴지** — 켜진 워크스페이스 정책이 가리키는 활성 모델 revision
2. **한 번에 하나만** — 실행 이력 표를 잠그고 확인해 겹친 실행을 막는다
3. **무엇이 일어났는지** — 숫자와 고정 코드만 이력 표에 남긴다

## 켜는 스위치는 여기 없다

이 모듈은 정책을 읽기만 한다. 켜진 워크스페이스가 없으면 `nothing_enabled` 로
끝나고 모델을 부르지 않는다. 그래서 타이머를 먼저 깔아도 안전하다 — 켜는 것은
콘솔의 정책 화면(사유·실행자 기록)이다.

## 본문은 어디에도 남지 않는다

출력·로그·이력 표에 있는 것은 개수·글자 수·비용·고정 코드뿐이다. 예외 메시지도
남기지 않는다 — 경로나 프로바이더가 되돌려 준 입력이 들어 있을 수 있다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tybot.embedding_index_cli")

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENV = 2

TRIGGERS = ("cli", "timer", "console")

#: 이보다 오래 `running` 인 행은 죽은 실행으로 본다. 서비스 유닛의
#: `TimeoutStartSec` 보다 길어야 한다 — 짧으면 살아 있는 실행을 버린다.
STALE_RUN_MINUTES = 150

#: 이력 표·출력으로 나가는 코드의 모양. 스키마의 CHECK 와 같다.
_CODE = re.compile(r"^[a-z0-9_]{1,80}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: 실행 이력의 단일 실행 판정을 직렬화하는 advisory 락 키. 판정과 INSERT 사이에
#: 다른 실행이 끼어들면 둘 다 「돌고 있는 것이 없다」 고 본다.
_RUN_GUARD = "tybot-embedding-index-run"


class RunRefused(RuntimeError):
    """돌지 않기로 했다. 코드는 고정 낱말이다."""

    def __init__(self, code: str) -> None:
        self.code = safe_code(code)
        super().__init__(self.code)


def safe_code(value: object, *, default: str = "unknown") -> str:
    """고정 ASCII 코드만 통과시킨다. 예외 본문이 코드 칸으로 새면 안 된다."""
    text = value if isinstance(value, str) else ""
    return text if _CODE.fullmatch(text) else default


# --- 무엇을 돌릴지 -------------------------------------------------------------


def enabled_model_revisions(connection) -> list[str]:
    """켜진 워크스페이스 정책이 가리키는 **활성** 모델 revision.

    활성이 아닌 모델을 가리키는 정책은 여기서 빼지만, 판정은 어차피
    `authorize_index_request` 가 문서마다 다시 한다. 여기서 거르는 이유는
    「돌려 봤자 전부 거절될 실행」 을 이력에 남기지 않기 위해서다.
    """
    with connection.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT p.model_revision
                 FROM search_embedding_workspace_policy p
                 JOIN search_embedding_model_revision m
                   ON m.revision = p.model_revision
                WHERE p.enabled AND m.state = 'active'
                ORDER BY 1"""
        )
        return [str(row[0]) for row in cur.fetchall()]


def misdeclared_policies(connection) -> list[str]:
    """외부 프로바이더 모델을 가리키면서 `transport='local'` 인 켜진 정책.

    `authorize_index_request` 는 `transport` 칸을 믿는다 — `local` 이면 외부 전송
    승인 두 개(모델·워크스페이스)를 보지 않는다. 그 칸은 사람이 적는 값이라,
    잘못 적히면 승인 없이 원문이 밖으로 나간다. 그래서 실행 전에 사실과 대조하고,
    하나라도 어긋나면 **전체 실행을 멈춘다**(어긋난 곳만 빼면 나머지는 돌지만,
    그러면 잘못 적힌 행이 아무도 모르는 채 남는다).
    """
    with connection.cursor() as cur:
        cur.execute(
            """SELECT p.workspace, m.provider
                 FROM search_embedding_workspace_policy p
                 JOIN search_embedding_model_revision m
                   ON m.revision = p.model_revision
                WHERE p.enabled AND p.transport = 'local'
                ORDER BY 1"""
        )
        rows = cur.fetchall()
    from tybot.embedding_index_job import EXTERNAL
    from tybot.embedding_index_policy import PROVIDER_TRANSPORTS

    return [
        str(row[0])
        for row in rows
        if PROVIDER_TRANSPORTS.get(str(row[1])) == EXTERNAL
    ]


def model_price(connection, model_revision: str) -> float | None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT input_price_per_mtok FROM search_embedding_model_revision WHERE revision=%s",
            (model_revision,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


# --- 실행 이력 ----------------------------------------------------------------


def start_run(connection, *, model_revision: str, trigger: str, requested_by: str) -> int:
    """이력 행을 연다. 다른 실행이 돌고 있으면 `already_running` 으로 거절한다.

    파일 락이 아니라 이 표로 막는 이유: 타이머와 콘솔은 같은 서버의 다른 프로세스가
    아닐 수도 있고, 무엇보다 **화면이 「돌고 있음」 을 같은 사실에서 읽어야** 한다.
    락 파일과 표가 따로 있으면 둘이 엇갈렸을 때 화면이 거짓말을 한다.
    """
    with connection.transaction(), connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_RUN_GUARD,))
        # 죽은 실행이 다음 실행을 영원히 막지 않게 먼저 정리한다.
        cur.execute(
            """UPDATE search_embedding_index_run
                  SET state='failed', error_code='run_abandoned', finished_at=now()
                WHERE state='running'
                  AND started_at < now() - make_interval(mins => %s)""",
            (STALE_RUN_MINUTES,),
        )
        cur.execute("SELECT 1 FROM search_embedding_index_run WHERE state='running' LIMIT 1")
        if cur.fetchone() is not None:
            raise RunRefused("already_running")
        cur.execute(
            """INSERT INTO search_embedding_index_run
                   (model_revision, trigger, requested_by)
                VALUES (%s, %s, %s) RETURNING id""",
            (model_revision, trigger, requested_by),
        )
        return int(cur.fetchone()[0])


def finish_run(connection, run_id: int, *, summary: RunSummary) -> None:
    """이력 행을 닫는다. 실패해도 숫자는 남긴다 — 어디까지 갔는지가 조사의 시작이다."""
    with connection.cursor() as cur:
        cur.execute(
            """UPDATE search_embedding_index_run
                  SET state=%s, error_code=%s, stopped_reason=%s,
                      inventory_count=%s, unchanged_count=%s, requested_count=%s,
                      published_count=%s, embed_calls=%s, embedded_chars=%s,
                      embedded_chunks=%s, cost_usd=%s, outcomes=%s::jsonb,
                      finished_at=now()
                WHERE id=%s""",
            (
                "failed" if summary.error_code else "succeeded",
                summary.error_code,
                summary.stopped_reason,
                summary.inventory_count,
                summary.unchanged_count,
                summary.requested_count,
                summary.published_count,
                summary.embed_calls,
                summary.embedded_chars,
                summary.embedded_chunks,
                summary.cost_usd,
                json.dumps(summary.outcomes, ensure_ascii=True, sort_keys=True),
                run_id,
            ),
        )


# --- 한 번의 실행 --------------------------------------------------------------


@dataclass
class RunSummary:
    model_revision: str
    error_code: str = ""
    stopped_reason: str = ""
    inventory_count: int | None = None
    unchanged_count: int | None = None
    requested_count: int | None = None
    published_count: int | None = None
    embed_calls: int | None = None
    embedded_chars: int | None = None
    embedded_chunks: int | None = None
    cost_usd: float | None = None
    outcomes: dict[str, int] = field(default_factory=dict)
    run_id: int | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "modelRevision": self.model_revision,
            "state": "failed" if self.error_code else "succeeded",
            "errorCode": self.error_code,
            "stoppedReason": self.stopped_reason,
            "inventory": self.inventory_count,
            "unchanged": self.unchanged_count,
            "requested": self.requested_count,
            "published": self.published_count,
            "embedCalls": self.embed_calls,
            "embeddedChars": self.embedded_chars,
            "embeddedChunks": self.embedded_chunks,
            "costUsd": self.cost_usd,
            "outcomes": dict(self.outcomes),
        }


def summarize(model_revision: str, result) -> RunSummary:
    """`ReconciledIndexResult` 를 이력 한 줄로 옮긴다. 본문이 없는 칸만 읽는다."""
    batch = result.batch
    outcomes: dict[str, int] = {}
    for code, count in batch.outcomes:
        key = safe_code(code)
        outcomes[key] = outcomes.get(key, 0) + int(count)
    error = ""
    if result.migrations_failed:
        # 새 좌표 발행은 됐는데 옛 좌표 retire 가 실패했다. 검색은 resolver 가
        # 현재 원문으로 다시 막지만, 색인이 깨끗하지 않다는 사실은 남긴다.
        error = "migration_retire_failed"
    return RunSummary(
        model_revision=model_revision,
        error_code=error,
        stopped_reason=safe_code(batch.stopped_reason, default="") if batch.stopped_reason else "",
        inventory_count=result.inventory_count,
        unchanged_count=result.unchanged_count,
        requested_count=result.requested_count,
        published_count=batch.published,
        embed_calls=batch.embed_calls,
        embedded_chars=batch.embedded_chars,
        embedded_chunks=batch.embedded_chunks,
        cost_usd=batch.cost_usd,
        outcomes=outcomes,
    )


def execute_model(
    connection,
    store,
    *,
    model_revision: str,
    trigger: str,
    requested_by: str,
    providers: Mapping,
    limits=None,
    run_index: Callable | None = None,
) -> RunSummary:
    """모델 revision 하나를 한 회차 돌리고 이력을 닫는다."""
    if run_index is None:
        from tybot.embedding_index_runtime import run_embedding_index as run_index
    run_id = start_run(
        connection, model_revision=model_revision, trigger=trigger, requested_by=requested_by
    )
    try:
        result = run_index(
            connection,
            store,
            model_revision=model_revision,
            providers=providers,
            # 필터 없이 아카이브 전체를 훑는다. 일부만 넘기면서 「완전하다」 고
            # 말하면 reconcile 이 안 보인 문서를 사라진 것으로 판정한다.
            scan_complete=True,
            limits=limits,
        )
        summary = summarize(model_revision, result)
    except Exception as exc:  # noqa: BLE001 - 본문·경로는 이력으로 내보내지 않는다
        summary = RunSummary(model_revision=model_revision, error_code=_failure_code(exc))
        logger.warning(
            "embedding index run failed run=%s code=%s type=%s",
            run_id, summary.error_code, type(exc).__name__,
        )
    summary.run_id = run_id
    finish_run(connection, run_id, summary=summary)
    return summary


def _failure_code(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "SourceUnavailable":
        # revision DB 를 못 보거나 DM 범위 요청. 지금 정본을 모르면 돌지 않는다.
        return "source_unavailable"
    if name == "ReconciliationError":
        return safe_code(str(exc), default="reconciliation_failed")
    if name in {"IndexPublishError", "IndexPlanError"}:
        return safe_code(str(exc), default="index_failed")
    return "run_failed"


# --- 계획 --------------------------------------------------------------------


def plan_model(store, *, model_revision: str, price_per_mtok: float | None,
               limits=None, sources=None) -> dict[str, object]:
    """돌리면 얼마나 되는지 센다. 모델 호출 0, DB 쓰기 0.

    비용은 **글자 수를 토큰 수로 본 추정**이다. 실제 토큰 수는 프로바이더만 안다.
    단가가 없으면 비용 칸은 null 이다 — 0 이 아니다.
    """
    from tybot.archive.embedding_source import iter_index_sources
    from tybot.embedding_index_inventory import inventory

    report = inventory(
        sources if sources is not None else iter_index_sources(store),
        model_revision=model_revision,
        limits=limits,
    ).to_json()
    chars = int(report.get("plannedChars") or 0)
    report["priceUsdPerMtok"] = price_per_mtok
    report["estimatedCostUsd"] = (
        None if price_per_mtok is None else round(chars / 1_000_000 * price_per_mtok, 4)
    )
    return report


# --- 진입점 ------------------------------------------------------------------


def _connect():
    import psycopg

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RunRefused("database_unconfigured")
    return psycopg.connect(url, autocommit=True)


def _limits(args):
    from tybot.embedding_index_job import IndexLimits

    values = {
        "max_documents": args.max_documents,
        "max_total_chars": args.max_chars,
        "max_total_chunks": args.max_chunks,
        "max_calls": args.max_calls,
    }
    chosen = {key: value for key, value in values.items() if value is not None}
    return IndexLimits(**chosen)


def _models(connection, explicit: str) -> list[str]:
    if explicit:
        if not _REVISION.fullmatch(explicit):
            raise RunRefused("model_revision_invalid")
        return [explicit]
    return enabled_model_revisions(connection)


def parse_args(argv: Sequence[str] | None = None):
    ap = argparse.ArgumentParser(description="B-12 임베딩 색인 실행 (기본: 계획만)")
    ap.add_argument("--execute", action="store_true",
                    help="실제로 임베딩을 호출하고 발행한다. 없으면 분량만 센다")
    ap.add_argument("--model-revision", default="",
                    help="기본: 켜진 워크스페이스 정책이 가리키는 활성 모델 전부")
    ap.add_argument("--trigger", choices=TRIGGERS, default="cli")
    ap.add_argument("--requested-by", default="",
                    help="이력에 남길 요청자. 기본: 트리거 이름")
    ap.add_argument("--max-documents", type=int, default=None)
    ap.add_argument("--max-chars", type=int, default=None)
    ap.add_argument("--max-chunks", type=int, default=None)
    ap.add_argument("--max-calls", type=int, default=None)
    ap.add_argument("--archive", default=None, help="기본: ARCHIVE_DIR")
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, connect=None, providers=None,
         store_factory=None) -> int:
    args = parse_args(argv)
    from tybot.envfile import load_env_file

    load_env_file()
    requested_by = (args.requested_by or args.trigger).strip()[:200] or args.trigger
    try:
        limits = _limits(args)
    except (TypeError, ValueError):
        return _emit({"result": "refused", "code": "limits_invalid"}, EXIT_REFUSED)

    try:
        connection = (connect or _connect)()
    except RunRefused as exc:
        return _emit({"result": "refused", "code": exc.code}, EXIT_ENV)
    except Exception:  # noqa: BLE001 - DSN 이 예외 본문에 들어 있다
        return _emit({"result": "refused", "code": "database_unavailable"}, EXIT_ENV)

    with connection:
        try:
            misdeclared = misdeclared_policies(connection)
            if misdeclared:
                logger.error("embedding policy declares external provider as local: %s",
                             ",".join(misdeclared))
                return _emit({"result": "refused", "code": "transport_misdeclared",
                              "workspaces": misdeclared}, EXIT_REFUSED)
            models = _models(connection, args.model_revision.strip())
        except RunRefused as exc:
            return _emit({"result": "refused", "code": exc.code}, EXIT_REFUSED)
        except Exception:  # noqa: BLE001
            return _emit({"result": "refused", "code": "policy_unreadable"}, EXIT_ENV)
        if not models:
            # 켜진 곳이 없다. 타이머가 매번 여기로 오는 것이 정상 상태다.
            return _emit({"result": "nothing_enabled", "modelCalls": 0}, EXIT_OK)

        store = (store_factory or _store)(args.archive)
        if not args.execute:
            plans = []
            for revision in models:
                try:
                    plans.append(plan_model(
                        store, model_revision=revision,
                        price_per_mtok=model_price(connection, revision), limits=limits,
                    ))
                except Exception as exc:  # noqa: BLE001
                    plans.append({"modelRevision": revision, "errorCode": _failure_code(exc)})
            return _emit({"result": "planned", "modelCalls": 0, "plans": plans}, EXIT_OK)

        if providers is None:
            from tybot.gateway.providers.openai_embeddings import build_embedding_providers

            providers = build_embedding_providers()
        runs = []
        for revision in models:
            try:
                summary = execute_model(
                    connection, store, model_revision=revision, trigger=args.trigger,
                    requested_by=requested_by, providers=providers, limits=limits,
                )
            except RunRefused as exc:
                runs.append({"modelRevision": revision, "state": "refused", "errorCode": exc.code})
                continue
            runs.append(summary.to_json())
    failed = any(run.get("errorCode") for run in runs)
    return _emit({"result": "executed", "runs": runs}, EXIT_REFUSED if failed else EXIT_OK)


def _store(archive: str | None):
    from tybot.archive.store import ArchiveStore
    from tybot.paths import archive_dir

    return ArchiveStore(Path(archive or archive_dir()))


def _emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return code


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
