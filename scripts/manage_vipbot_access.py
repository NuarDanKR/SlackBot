#!/usr/bin/env python3
"""웹콘솔을 쓸 수 없을 때 VIPBot 사용자·근거 채널을 관리한다.

원시 SQL을 대신하는 운영 도구다. 콘솔과 같은 ``user_directory`` 저장소를 호출하므로
활성 직원 확인, 중복 방지, 회수 이력과 감사 사유 계약이 갈라지지 않는다. 토큰이나
질문·답변 본문은 받거나 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.console import user_directory as directory  # noqa: E402
from tybot.envfile import load_env_file  # noqa: E402

BOT = "vipbot"


def _expires_at(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ISO 8601 날짜·시각이어야 합니다") from exc
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("만료 시각에는 시간대가 있어야 합니다")
    return value


def _audit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reason", required=True, help="변경 사유(감사 기록)")
    parser.add_argument("--actor", required=True, help="작업자 회사 이메일")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=os.getenv("TYBOT_ENV_FILE", ""))
    commands = parser.add_subparsers(dest="command", required=True)

    grant = commands.add_parser("grant", help="직원에게 VIPBot 사용 권한을 준다")
    grant.add_argument("--emp-no", required=True)
    grant.add_argument("--expires-at", type=_expires_at, default=None)
    _audit(grant)

    revoke = commands.add_parser("revoke", help="VIPBot 사용 권한을 회수한다")
    revoke.add_argument("--grant-id", required=True, type=int)
    _audit(revoke)

    add = commands.add_parser("add-source", help="VIPBot 근거 채널을 등록한다")
    add.add_argument("--workspace", required=True)
    add.add_argument("--channel-id", required=True)
    add.add_argument("--name", default="")
    _audit(add)

    remove = commands.add_parser("remove-source", help="VIPBot 근거 채널을 해제한다")
    remove.add_argument("--workspace", required=True)
    remove.add_argument("--channel-id", required=True)
    _audit(remove)

    status = commands.add_parser("status", help="실제 사용자 판정과 근거 채널을 확인한다")
    status.add_argument("--workspace", required=True)
    status.add_argument("--slack-user", required=True)
    return parser


def _channel(args: argparse.Namespace) -> list[dict]:
    return [{
        "workspace": args.workspace,
        "channelId": args.channel_id,
        "name": getattr(args, "name", ""),
    }]


def run(args: argparse.Namespace) -> int:
    if args.command == "grant":
        result = directory.grant(directory.GrantRequest(
            bot=BOT,
            emp_no=args.emp_no,
            reason=args.reason,
            actor=args.actor,
            expires_at=args.expires_at,
        ))
        print(f"권한 등록 완료: grant_id={result['id']} state={result['state']}")
        return 0
    if args.command == "revoke":
        result = directory.revoke(
            args.grant_id, reason=args.reason, actor=args.actor
        )
        print(f"권한 회수 완료: grant_id={result['id']} state={result['state']}")
        return 0
    if args.command == "add-source":
        result = directory.add_sources(
            bot=BOT,
            channels=_channel(args),
            reason=args.reason,
            actor=args.actor,
        )
        print(
            "근거 채널 반영 완료: "
            f"added={len(result['added'])} already={len(result['already'])}"
        )
        return 0
    if args.command == "remove-source":
        result = directory.remove_sources(
            bot=BOT,
            channels=_channel(args),
            reason=args.reason,
            actor=args.actor,
        )
        print(
            "근거 채널 해제 완료: "
            f"removed={len(result['removed'])} missing={len(result['missing'])}"
        )
        return 0

    decision = directory.check(
        bot=BOT, workspace=args.workspace, slack_user=args.slack_user
    )
    sources = directory.list_sources(bot=BOT)["channels"]
    print(
        f"사용자 판정: allowed={str(decision['allowed']).lower()} "
        f"code={decision['code']}"
    )
    print(f"근거 채널: {len(sources)}개")
    for source in sources:
        print(f"  {source['workspace']}:{source['channelId']} {source['name'] or '-'}")
    return 0 if decision["allowed"] and sources else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.env_file:
        os.environ["TYBOT_ENV_FILE"] = args.env_file
    load_env_file()
    try:
        return run(args)
    except directory.UserDirectoryError as exc:
        print(f"VIPBot 관리 실패: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
