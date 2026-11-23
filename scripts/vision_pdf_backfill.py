#!/usr/bin/env python3
"""B-80 PDF 페이지 Vision 소급. 기본은 **목록만**(dry-run). 실제 전송은 --apply.

서버(운영):
    # 1) 목록 — 원본을 열지도 보내지도 않는다
    sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/archiver.env \\
        /opt/tybot/.venv/bin/python /opt/tybot/scripts/vision_pdf_backfill.py --json

    # 3) 이미 저장된 전사의 검색 투영 소급(멱등)
    #    … vision_pdf_backfill.py --project

    # 2) 실행 — 목록을 본 사람이 실행자·사유를 대고 상한 안에서만
    sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/archiver.env \\
        TYBOT_VISION_DATABASE_URL_FILE=/etc/tybot/tybot-vision-database-url \\
        VISION_PROVIDER_SECRET_KEY_FILE=/etc/tybot/vision-provider-secret.key \\
        /opt/tybot/.venv/bin/python /opt/tybot/scripts/vision_pdf_backfill.py \\
        --apply --workspace tyit --actor ops@taeyoung.com --reason "B-80 소급 31건" \\
        --max-pages 40 --amount-usd 0.01

지키는 것: 실패·미확인 쪽만 · 같은 쪽 재호출 없음(원장 UNIQUE) · timeout/429 자동 재시도 없음 ·
결과는 파생 revision · 민감도는 공통 계약(`vision_sensitivity`) · 카탈로그를 못 읽으면 보내지 않는다.

종료 코드: 0 정상 · 2 입력/설정 오류
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tybot.archive import vision_pdf_batch as batch

EXIT_OK, EXIT_INPUT = 0, 2


def _catalog_checks(connection):
    """공용 모델 카탈로그(`archiver_image_model_catalog`)에서 읽은 사실만. 못 읽으면 {}."""
    from tybot.gateway.base import Sensitivity

    def read(policy) -> dict:
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """SELECT supports_vision, credential_ready, max_sensitivity
                       FROM archiver_image_model_catalog WHERE provider=%s AND model=%s""",
                    (policy.provider, policy.model),
                )
                row = cur.fetchone()
        except Exception:  # noqa: BLE001 - 모르면 보내지 않는다
            return {}
        if not row:
            return {}
        return {
            "capability_verified": row["supports_vision"] is True,
            "credential_ready": row["credential_ready"] is True,
            "catalog_max_sensitivity": Sensitivity(row["max_sensitivity"]),
        }

    return read


def channel_scope_from_master(url: str):
    """지금 수집 범위 안인 채널 집합(`archive_channel_mode`). **읽기 전용**으로만 연다.

    Vision 계정 권한을 넓히지 않으려고 Master 접속으로 읽는다. 쓰지 않는다
    (`default_transaction_read_only`). 못 읽으면 빈 집합 — 그러면 아무 쪽도 보내지 않는다.
    """
    allowed: set[tuple[str, str]] = set()
    if url:
        try:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(url, autocommit=True, row_factory=dict_row,
                                 options="-c default_transaction_read_only=on") as conn:
                rows = conn.execute(
                    """SELECT workspace, channel_id FROM archive_channel_mode
                       WHERE mode IN ('active','shadow') AND membership = 'joined'
                         AND NOT operator_hold"""
                ).fetchall()
            allowed = {(r["workspace"], r["channel_id"]) for r in rows}
        except Exception as exc:  # noqa: BLE001 - 모르면 보내지 않는다
            print(f"등록 채널을 조회하지 못했습니다({type(exc).__name__}). 보내지 않습니다.",
                  file=sys.stderr)
    return lambda workspace, channel_id: (workspace, channel_id) in allowed


def _router(policy, api_key: str, state: Path):
    from tybot.archive.image_description_job import _provider
    from tybot.archive.vision_sensitivity import request_sensitivity
    from tybot.gateway.base import ModelSpec
    from tybot.gateway.cost import CostGuard
    from tybot.gateway.router import Router

    spec = ModelSpec(model=policy.model, provider=policy.provider, input_price_per_mtok=0.0,
                     output_price_per_mtok=0.0,
                     max_sensitivity=request_sensitivity(policy.sensitivity))
    return Router(providers={policy.provider: _provider(policy.provider, api_key)},
                  registry={policy.model: spec},
                  cost_guard=CostGuard(0.5, state_path=state / "vision-pdf-cost.json"),
                  default_model=policy.model, fallback_models=())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="B-80 PDF 페이지 Vision 소급 (기본 dry-run)")
    ap.add_argument("--workspace", default="")
    ap.add_argument("--file", action="append", default=[], help="Slack 파일 ID 로 좁히기")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--project", action="store_true",
                    help="이미 저장된 전사(실패·보류는 본문 없는 묘비)를 검색 투영에 소급(멱등, 모델 호출 없음)")
    ap.add_argument("--actor", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--amount-usd", default="",
                    help="쪽당 예약 상한(USD, 0 불가). 정책 요금표로 본 출력 상한 비용보다 작으면 거절")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from tybot.envfile import load_env_file

    load_env_file()
    archive = os.getenv("ARCHIVE_DIR", "").strip()
    if not archive or not Path(archive).is_dir():
        print("ARCHIVE_DIR 이 없거나 디렉터리가 아닙니다.", file=sys.stderr)
        return EXIT_INPUT

    if args.project:
        import psycopg
        from psycopg.rows import dict_row

        from tybot.archive.vision_store import VisionStore
        from tybot.console.vision_dsn import resolve_vision_database_url

        with psycopg.connect(resolve_vision_database_url(), autocommit=True,
                             row_factory=dict_row) as conn:
            added = VisionStore(conn).project_pages()
        print(json.dumps({"projected": added}, ensure_ascii=False))
        return EXIT_OK

    if not args.apply:
        report = batch.dry_run(archive, workspace=args.workspace, file_ids=args.file)
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
        else:
            print(f"문서 {report['documents']} · 보낼 문서 {report['eligible_documents']} · "
                  f"보낼 쪽 {report['pages']}")
            for code, count in report["codes"].items():
                print(f"  {code}: {count}")
            print("  (dry-run — 실제 전송은 --apply)")
        return EXIT_OK

    if not args.workspace or not args.actor.strip() or not args.reason.strip():
        print("--apply 는 --workspace·--actor·--reason 이 필요합니다.", file=sys.stderr)
        return EXIT_INPUT
    if not 1 <= args.max_pages <= 500:
        print("--max-pages 는 1~500 입니다.", file=sys.stderr)
        return EXIT_INPUT
    try:
        amount = Decimal(args.amount_usd)
    except InvalidOperation:
        amount = Decimal(0)
    if amount <= 0:
        print("--amount-usd 는 0 보다 커야 합니다(모르는 요금을 0 으로 두지 않는다).", file=sys.stderr)
        return EXIT_INPUT

    import psycopg
    from psycopg.rows import dict_row

    from tybot.archive import vision_pdf_render
    from tybot.archive.image_description_secret import resolve_provider_key
    from tybot.archive.vision_store import VisionStore
    from tybot.console.vision_dsn import resolve_vision_database_url

    state = Path(os.getenv("STATE_DIR", "/var/lib/tybot"))
    with psycopg.connect(resolve_vision_database_url(), autocommit=True,
                         row_factory=dict_row) as conn:
        store = VisionStore(conn)
        policy, _config, _sources = store.effective_policy(args.workspace)
        router = _router(policy, resolve_provider_key(conn, policy.provider), state)
        stats = batch.apply(
            archive, store=store, router=router, renderer=vision_pdf_render,
            catalog_checks=_catalog_checks(conn),
            channel_scope=channel_scope_from_master(os.getenv("DATABASE_URL", "").strip()),
            actor=args.actor, reason=args.reason,
            now=datetime.now(UTC), amount_usd=amount, workspace=args.workspace,
            file_ids=args.file, max_pages=args.max_pages,
        )
    print(json.dumps(stats.to_json(), ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
