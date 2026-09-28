"""Slack App Manifest 정본과 **대조 확인**.

설계: `docs/design/workspace-service-console-redesign.md` §6 (2026-09-28)

## 두 가지를 합치지 않는다

| 무엇 | 무슨 질문 | 누가 답하나 |
|---|---|---|
| 신원 확인 | 이 토큰이 **누구인가** | Slack `auth.test`(`bot_identity`) |
| Manifest 적용 확인 | 그 앱이 **무슨 권한을 갖고 있나** | 사람이 정본 hash 와 대조 |

`auth.test` 성공만으로 Manifest 가 일치한다고 표시하지 않는다(§6.2). 토큰은 맞는데
스코프가 빠져 있으면, 수집은 조용히 절반만 된다 — 그건 오류로 안 나타난다.

## hash 를 DB 에 복사하지 않는다

정본은 저장소의 파일이다. DB 에 적어 두면 파일이 바뀌어도 그 값이 남고, 그때
화면은 「대조했다」 를 **옛 hash 기준으로** 보여 준다. 그래서 목록은 파일에서
매번 센다. 연결에 남기는 것은 **사람이 확인했을 때의 hash** 이고, 그 둘이 다르면
화면이 「정본이 바뀌었다」 고 말할 수 있어야 한다.

Manifest 표(`slack_app_manifest`, §6.1)는 아직 만들지 않는다 — 스키마를 건드리면
release gate 지문이 바뀌어 격리 DB 재검증이 필요해진다. 지금 필요한 것은 목록과
대조 기록뿐이고, 둘 다 파일과 기존 열로 된다.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import bot_admin
from .bot_repo import BotRepo, default_repo

ROOT = Path(__file__).resolve().parents[3]

#: 정본 Manifest. **봇 key 로 건다** — 역할 이름으로 걸면 이름을 바꾼 날 끊긴다.
#:
#: Hermes 직접 연결 Manifest 는 PF 승인본이라 이 저장소에 없다(§6.1). 없는 것을
#: 빈 값으로 채우지 않는다 — 「아직 없다」 와 「대조했는데 비었다」 는 다르다.
MANIFESTS: tuple[tuple[str, str, str], ...] = (
    ("master", "docs/pilot/slack-app-manifest.yaml", "Slack 진입·답변·명령"),
    ("archiver", "docs/pilot/archiving-app-manifest.yaml", "채널·첨부·Canvas 수집"),
)


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def catalog() -> dict:
    """정본 목록과 지금 파일의 hash."""
    rows = []
    for bot_key, relative, purpose in MANIFESTS:
        path = ROOT / relative
        digest = _sha256(path)
        rows.append({
            "manifestId": f"{bot_key}/slack_socket",
            "botKey": bot_key,
            "connectorType": "slack_socket",
            "sourcePath": relative,
            "purpose": purpose,
            "sha256": digest,
            # 파일이 없으면 **없다고 말한다.** 빈 hash 를 정상으로 보이게 두면
            # 사람이 그 값으로 대조했다고 적을 수 있다.
            "present": bool(digest),
        })
    return {"manifests": rows}


def _entry(manifest_id: str) -> dict | None:
    return next(
        (row for row in catalog()["manifests"] if row["manifestId"] == manifest_id),
        None,
    )


def known_sha256(manifest_id: str) -> str:
    row = _entry(manifest_id)
    return str(row["sha256"]) if row else ""


def detail(manifest_id: str) -> dict:
    """정본 **내용**까지. catalog 에 있는 것만 읽는다.

    ## 왜 경로를 안 받나

    화면이 파일 경로를 보내게 하면 그 자리가 곧 경로 탈출이다. 여기서 받는 것은
    **catalog 의 ID 뿐**이고, 파일 경로는 우리가 코드에 적어 둔 값에서만 나온다.
    요청자가 고를 수 있는 것은 「어느 정본이냐」 이지 「어느 파일이냐」 가 아니다.

    Manifest YAML 은 시크릿이 아니다 — 스코프와 이벤트 목록이다. 그래서 승인된
    콘솔 사용자에게 내용을 보여 준다. 대신 **보여 주는 것과 대조했다고 적는 것은
    다른 동작**이다(§4.2). 보기만으로 확인 상태를 바꾸지 않는다.
    """
    row = _entry(manifest_id)
    if row is None:
        raise bot_admin.BotAdminRefused(f"등록되지 않은 Manifest 입니다: {manifest_id}")
    path = ROOT / str(row["sourcePath"])
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        # 파일이 없으면 **없다고 말한다.** 빈 내용을 정상처럼 보여 주면 사람이
        # 그걸 Slack 에 붙여 넣는다.
        raise bot_admin.BotAdminRefused(
            f"정본 파일을 읽지 못했습니다: {row['sourcePath']}"
        ) from None
    return {
        "manifestId": row["manifestId"],
        "botKey": row["botKey"],
        "sourcePath": row["sourcePath"],
        "sha256": row["sha256"],
        "content": content,
    }


def owner_of(manifest_id: str) -> str:
    """이 Manifest 는 **어느 봇의 것인가.**

    `manifest_id` 는 화면이 보내 주는 값이라 요청의 봇과 다를 수 있다. 그대로
    받으면 Archiver 연결에 Master Manifest 를 대조했다고 적을 수 있고, 그때
    화면은 두 연결 다 「확인됨」 으로 보인다 — 실제로 확인된 것은 하나뿐이다.
    """
    row = _entry(manifest_id)
    return str(row["botKey"]) if row else ""


def attest(
    workspace: str,
    bot_key: str,
    *,
    manifest_id: str,
    sha256: str,
    actor: bot_admin.Actor,
    repo: BotRepo | None = None,
) -> None:
    """관리자가 대조했다고 적는다. **정본 hash 와 다르면 거절한다.**

    다른 값을 그대로 받으면 화면은 「확인됨」 인데 무엇과 확인했는지 모르는 상태가
    된다. 그 상태는 확인 안 한 것보다 나쁘다 — 사람이 다시 안 본다.
    """
    expected = known_sha256(manifest_id)
    if not expected:
        raise bot_admin.BotAdminRefused(
            f"저장소에 없는 Manifest 입니다: {manifest_id}."
            " PF 승인본처럼 저장소 밖 정본은 아직 대조 대상이 아닙니다."
        )
    owner = owner_of(manifest_id)
    if owner != bot_key:
        # 남의 Manifest 로 대조를 적으면 두 연결 다 「확인됨」 으로 보인다.
        # 실제로 확인된 것은 하나뿐이고, 어느 쪽인지는 화면에 안 남는다.
        raise bot_admin.BotAdminRefused(
            f"{manifest_id} 는 {owner} 의 Manifest 입니다. {bot_key} 연결에는"
            " 대조로 적을 수 없습니다."
        )
    if sha256 != expected:
        raise bot_admin.BotAdminRefused(
            "정본 hash 와 다릅니다. 화면의 값을 그대로 붙여 넣으세요."
        )

    store = repo or default_repo()
    with store.transaction() as tx:
        row = tx.connection(workspace, bot_key)
        if row is None:
            raise bot_admin.BotAdminRefused(f"없는 연결입니다: {workspace}/{bot_key}")
        tx.save_manifest_attestation({
            "id": int(row["id"]), "manifest_id": manifest_id, "sha256": sha256,
            "actor": actor.name,
        })
        tx.add_audit({
            "actor": actor.name, "subject": bot_admin.SUBJECT_CONNECTION,
            "workspace": workspace,
            "field": f"connection.{bot_key}.slack_socket.manifest",
            "old_value": "", "new_value": f"{manifest_id}@{sha256[:12]}",
            "reason": actor.reason,
        })
