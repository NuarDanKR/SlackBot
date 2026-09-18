"""콘솔 채널 관리 — 한 화면에서 담당자를 정한다.

설계: [`docs/design/console-channel-admin.md`](../../../docs/design/console-channel-admin.md)

## 왜
옛날에 만든 채널은 `/채널 수정` 이 안 된다. **담당자가 없기 때문이다.** 담당자가
없으면 검토자도 못 정하고, 그러면 요약 검토 DM도 안 간다 — 한 칸이 비어서 그 뒤
기능이 줄줄이 멈춘다.

Slack 에서 채널마다 명령을 치게 하면 수십 개를 하나씩 돌아야 한다. 그 일은 안 한다 —
그래서 지금까지 안 된 것이다. 표로 보고 한 번에 정한다.

## 활성도를 같이 보는 이유
담당자를 정할 때 **어느 채널이 살아 있는지** 알아야 한다. 답변이 0건이고 수집도 멈춘
채널에 담당자를 붙이는 것은 일을 만드는 것이다. 반대로 답변이 많은데 담당자가 없는
채널이 가장 급하다.

## 이 모듈이 하지 않는 것
- 채널을 만들거나 지우지 않는다. 이미 있는 채널의 메타데이터만 다룬다.
- 원문을 보여 주지 않는다. 건수와 시각만 — 열람은 기록을 남기는 기존 경로로.
- **검토자를 바꾸지 않는다.** 검토자는 채널 소유자가 Slack 에서 정한다. 여기서도
  바꾸면 같은 결정이 두 곳에서 나고 누가 정했는지가 흐려진다. 보여만 준다.
- 담당자를 추측하지 않는다. 빈 칸은 빈 칸으로 둔다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("tybot.console.channel_admin")


@dataclass
class ChannelRow:
    """표 한 줄. 한 채널의 기본 정보와 활성도."""

    workspace: str
    workspace_label: str
    channel_id: str
    channel: str
    owner: str = ""
    owner_source: str = ""
    managers: list[str] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)
    send_at: str = ""
    documents: int = 0
    lines: int = 0
    attachment_lines: int = 0
    last_ingested: str = ""
    answers: int = 0
    last_answer: str = ""

    @property
    def needs_owner(self) -> bool:
        """담당자가 없다 = **아무도 이 채널을 고칠 수 없다.**"""
        return not self.owner

    @property
    def needs_reviewer(self) -> bool:
        """검토자가 없다 = 요약 후보가 아무에게도 가지 않는다."""
        return not self.reviewers

    def urgency(self) -> tuple:
        """정렬 키. **고칠 수 없는데 사람이 쓰는 채널**이 맨 위로 온다.

        답변이 많은데 담당자가 없는 곳이 가장 급하다. 반대로 답변도 수집도 없는
        채널에 담당자를 붙이는 것은 일을 만드는 것이다.
        """
        return (
            0 if self.needs_owner else 1,
            0 if self.needs_reviewer else 1,
            -self.answers,
            -self.lines,
            self.channel,
        )

    def to_json(self) -> dict:
        return {
            "workspace": self.workspace,
            "workspaceLabel": self.workspace_label,
            "channelId": self.channel_id,
            "channel": self.channel,
            "owner": self.owner,
            "ownerSource": self.owner_source,
            "managers": list(self.managers),
            "reviewers": list(self.reviewers),
            "sendAt": self.send_at,
            "documents": self.documents,
            "lines": self.lines,
            "attachmentLines": self.attachment_lines,
            "lastIngestedAt": self.last_ingested,
            "answers": self.answers,
            "lastAnswerAt": self.last_answer,
            "needsOwner": self.needs_owner,
            "needsReviewer": self.needs_reviewer,
        }


def _merge(rows: list[ChannelRow]) -> list[ChannelRow]:
    return sorted(rows, key=lambda r: r.urgency())


def build_rows(
    docs: list[dict],
    *,
    owners: dict[tuple[str, str], dict],
    reviewers: dict[tuple[str, str], list[dict]],
    answers: dict[tuple[str, str], tuple[int, str]],
    labels: dict[str, str] | None = None,
) -> list[ChannelRow]:
    """조각들을 한 줄로 합친다. **순수 함수다** — DB·파일은 호출자가 읽는다.

    `docs` 는 `reader.collected_docs()` 모양이다. 한 채널에 날짜별 문서가 여럿이므로
    채널 단위로 합친다.

    채널 ID 로 잇는다. 이름은 바뀌고, 바뀌면 담당자가 조용히 사라진다 — `/채널
    이름변경` 이 실제로 그렇게 만든다.
    """
    labels = labels or {}
    by_channel: dict[tuple[str, str], ChannelRow] = {}
    workspace_labels: dict[str, str] = {}

    for doc in docs:
        workspace = str(doc.get("workspace") or "")
        channel_id = str(doc.get("channelId") or doc.get("channel_id") or "")
        channel = str(doc.get("channel") or "")
        if not workspace or not channel:
            continue
        label = str(labels.get(workspace) or doc.get("workspaceLabel") or workspace)
        current_label = workspace_labels.get(workspace, "")
        # Archive rows know the configured label, while heartbeat-only rows use the
        # workspace key as a fallback.  If both exist, one workspace must still have
        # one label or the frontend sorts it into two separate groups.
        if not current_label or (current_label == workspace and label != workspace):
            workspace_labels[workspace] = label
        # v1 문서에는 채널 ID 가 없다. 이름을 키로 쓰되 **담당자 지정은 막는다** —
        # ID 없이 쓰면 다른 채널에 붙을 수 있다.
        key = (workspace, channel_id or f"name:{channel}")
        row = by_channel.get(key)
        if row is None:
            row = ChannelRow(
                workspace=workspace,
                workspace_label=label,
                channel_id=channel_id,
                channel=channel,
            )
            by_channel[key] = row
        row.documents += int(doc.get("documents", 1))
        row.lines += int(doc.get("lines") or 0)
        row.attachment_lines += int(doc.get("attachmentLines") or 0)
        last = str(doc.get("lastIngestedAt") or "")
        if last > row.last_ingested:
            row.last_ingested = last

    for row in by_channel.values():
        row.workspace_label = workspace_labels.get(row.workspace, row.workspace_label)

    for key, row in by_channel.items():
        found = owners.get(key) or {}
        row.owner = str(found.get("owner_user_id") or "")
        row.owner_source = str(found.get("owner_source") or "")
        row.managers = [str(u) for u in (found.get("manager_user_ids") or []) if str(u)]

        people = reviewers.get(key) or []
        row.reviewers = [str(r.get("reviewer_user") or "") for r in people if r.get("reviewer_user")]
        if people:
            row.send_at = str(people[0].get("send_at") or "")

        count, last = answers.get(key, (0, ""))
        row.answers = count
        row.last_answer = last

    return _merge(list(by_channel.values()))


def summary(rows: list[ChannelRow]) -> dict:
    """머리글 숫자. **무엇부터 해야 하는지**를 먼저 말한다."""
    return {
        "channels": len(rows),
        "missingOwner": sum(1 for r in rows if r.needs_owner),
        "missingReviewer": sum(1 for r in rows if r.needs_reviewer),
        # 담당자가 없는데 사람이 쓰는 채널. 이게 급한 것이다.
        "activeMissingOwner": sum(1 for r in rows if r.needs_owner and r.answers),
    }


# --- 일괄 지정 ----------------------------------------------------------------
@dataclass
class AssignResult:
    """무엇이 바뀌고 무엇이 안 바뀌었는가.

    **부분 성공을 부분 성공이라고 말한다.** 전부 성공으로 보이면 사람은 확인하지
    않고, 안 바뀐 채널은 계속 아무도 못 고치는 상태로 남는다.
    """

    changed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (채널, 사유)

    @property
    def ok(self) -> bool:
        return not self.skipped

    def message(self) -> str:
        parts = [f"{len(self.changed)}건 지정"]
        if self.skipped:
            parts.append(f"{len(self.skipped)}건 변경 없음")
        return " · ".join(parts)

    def to_json(self) -> dict:
        return {
            "changed": list(self.changed),
            "skipped": [{"channel": c, "reason": r} for c, r in self.skipped],
            "message": self.message(),
            "ok": self.ok,
        }


def assign_owner(
    store,
    targets: list[tuple[str, str, str]],
    owner_user_id: str,
    *,
    actor: str,
    overwrite: bool = False,
) -> AssignResult:
    """여러 채널의 담당자를 한 번에 정한다.

    `targets` 는 `(워크스페이스, 채널ID, 채널명)` 목록이다. 채널명은 기록용이고
    판정은 ID 로 한다.

    한 건이 실패해도 나머지를 계속한다 — 첫 실패에서 멈추면 사람이 다시 눌러야 하고,
    무엇이 됐는지도 모른다.
    """
    result = AssignResult()
    owner = str(owner_user_id or "").strip()
    if not owner:
        raise ValueError("담당자를 고르지 않았습니다.")

    for workspace, channel_id, channel in targets:
        label = channel or channel_id
        if not channel_id or channel_id.startswith("name:"):
            # 채널 ID 를 모르는 문서(v1)다. 이름으로 쓰면 다른 채널에 붙을 수 있다.
            result.skipped.append((label, "채널 ID 를 모릅니다(옛 아카이브 형식)"))
            continue
        try:
            changed = store.set_owner(
                workspace, channel_id, owner,
                set_by=actor, name=channel, overwrite=overwrite,
            )
        except Exception as exc:  # noqa: BLE001 - 한 건 실패가 나머지를 막지 않는다
            result.skipped.append((label, f"{type(exc).__name__}"))
            logger.warning("담당자 지정 실패 ws=%s ch=%s: %s", workspace, channel_id, exc)
            continue
        if changed:
            result.changed.append(label)
        else:
            result.skipped.append((label, "이미 담당자가 있습니다"))

    logger.info(
        "채널 담당자 일괄 지정 actor=%s owner=%s 변경=%d 미변경=%d",
        actor, owner, len(result.changed), len(result.skipped),
    )
    return result


# --- 실제 조각을 읽는다 --------------------------------------------------------
#
# 위 함수들은 순수하다. 여기서만 DB·파일을 만진다 — 그래야 표 로직을 DB 없이 시험한다.
def answer_counts(days: int = 365) -> dict[tuple[str, str], tuple[int, str]]:
    """채널별 (답변 수, 마지막 답변 시각). QA 기록에서 센다.

    **봇 답변이 실제로 나간 횟수**다. 질문 수가 아니라 이 채널이 쓰이는 정도를 본다.
    """
    from . import reader

    out: dict[tuple[str, str], list] = {}
    for row in reader._read_qa_records(days):
        workspace = str(row.get("workspace") or "")
        channel_id = str(row.get("channel_id") or "")
        if not workspace or not channel_id:
            continue
        key = (workspace, channel_id)
        slot = out.setdefault(key, [0, ""])
        slot[0] += 1
        ts = str(row.get("ts") or "")
        if ts > slot[1]:
            slot[1] = ts
    return {k: (v[0], v[1]) for k, v in out.items()}


def reviewer_map() -> dict[tuple[str, str], list[dict]]:
    """채널별 검토자. DB 를 못 읽으면 **빈 값이 아니라 예외**다 — 「검토자 없음」 과
    「못 읽었다」 를 섞으면 화면이 전 채널을 빨강으로 칠한다."""
    from .. import reviewers

    out: dict[tuple[str, str], list[dict]] = {}
    for row in reviewers.all_enabled():
        key = (str(row.get("workspace") or ""), str(row.get("channel_id") or ""))
        out.setdefault(key, []).append(row)
    return out


def owner_store():
    """봇이 쓰는 것과 **같은 파일**을 본다. 다른 경로를 보면 화면과 실제가 갈린다."""
    from ..channel_management import ChannelOwnerStore
    from ..heartbeat import state_dir

    return ChannelOwnerStore(state_dir() / "channel-owners.json")


def snapshot(days: int = 365) -> tuple[list[ChannelRow], dict]:
    """화면이 쓰는 표와 머리글 숫자."""
    from . import reader

    docs = reader.collected_docs()
    docs.extend(heartbeat_channel_rows())
    rows = build_rows(
        docs,
        owners=owner_store().all(),
        reviewers=reviewer_map(),
        answers=answer_counts(days),
        labels=reader.workspace_labels(),
    )
    return rows, summary(rows)


def heartbeat_channel_rows() -> list[dict]:
    """봇이 현재 참여한 채널 중 아직 원문 문서가 없는 채널도 표에 보탠다.

    콘솔이 Slack API를 직접 부르면 화면을 열 때마다 토큰과 rate limit을 쓰게 된다.
    봇 heartbeat에는 채널 ID·이름만 있고 업무 본문은 없다.
    """
    from .. import heartbeat

    status_root = heartbeat.state_dir() / "status"
    rows: list[dict] = []
    if not status_root.is_dir():
        return rows
    for path in sorted(status_root.glob("*.json")):
        workspace = path.stem
        status = heartbeat.read(workspace) or {}
        for channel in status.get("channel_rows") or []:
            channel_id = str(channel.get("id") or "")
            name = str(channel.get("name") or "")
            if channel_id and name:
                rows.append(
                    {
                        "workspace": workspace,
                        "workspaceLabel": workspace,
                        "channelId": channel_id,
                        "channel": name,
                        "documents": 0,
                        "lines": 0,
                        "attachmentLines": 0,
                    }
                )
    return rows


# --- 담당자로 고를 수 있는 사람 -------------------------------------------------
#
# 아무 문자열이나 받으면 **오타가 그대로 저장되고**, 그 채널은 계속 아무도 못 고친다.
# 콘솔은 Slack 을 모르므로 이미 아는 사람 중에서 고른다 — `user_identity` 에 이어진
# 재직자다.
CANDIDATE_SQL = """
select ui.workspace, ui.slack_user, e.name, o.name as org_name
  from user_identity ui
  join employee e on e.emp_no = ui.emp_no and e.active
  left join org_unit o on o.code = e.org_code
 order by ui.workspace, e.name
"""


def owner_candidates() -> list[dict]:
    """담당자 후보. 워크스페이스·Slack ID·이름·소속.

    이메일은 담지 않는다 — 화면에 뿌릴 이유가 없고, 뿌리면 주소록이 된다.
    """
    from .workspace_store import _connect

    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(CANDIDATE_SQL)
            return [
                {
                    "workspace": str(r["workspace"]),
                    "slackUser": str(r["slack_user"]),
                    "name": str(r["name"] or ""),
                    "org": str(r["org_name"] or ""),
                }
                for r in cur.fetchall()
            ]
    except Exception as exc:  # noqa: BLE001 - 후보를 못 읽어도 표는 보여야 한다
        logger.warning("담당자 후보를 읽지 못했다: %s", exc)
        return []


def is_known_user(slack_user: str) -> bool:
    """이 Slack ID 가 사번과 이어져 있는가.

    못 읽으면 **막지 않는다.** DB 장애 때문에 담당자 지정이 통째로 안 되면, 그게
    오타 하나보다 나쁘다. 대신 후보 목록이 비어 화면에서 이미 드러난다.
    """
    from .workspace_store import _connect

    user = str(slack_user or "").strip()
    if not user:
        return False
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select 1 from user_identity ui"
                " join employee e on e.emp_no = ui.emp_no and e.active"
                " where ui.slack_user = %s limit 1",
                (user,),
            )
            return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001 - DB 장애로 지정을 막지 않는다
        logger.warning("담당자 후보 확인 실패(통과시킨다): %s", exc)
        return True


def channel_names() -> dict[tuple[str, str], str]:
    """채널 ID → 이름. 기록용이다 — 판정은 언제나 ID 로 한다."""
    from . import reader

    out: dict[tuple[str, str], str] = {}
    for doc in reader.collected_docs():
        workspace = str(doc.get("workspace") or "")
        channel_id = str(doc.get("channelId") or "")
        channel = str(doc.get("channel") or "")
        if workspace and channel_id and channel:
            out[(workspace, channel_id)] = channel
    return out
