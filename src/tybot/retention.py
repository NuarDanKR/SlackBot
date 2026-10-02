"""보존 기간 집행 — 정해 둔 90일을 **실제 자료에 적용한다.**

결정: 2026-10-02 오너(B-70). 콘솔 설정·영구 보관은 B-71 이라 여기 없다.

## 왜 필요했나

`archive_retention_policy` 에 90일이 들어 있었지만 **그 값을 읽어 지우는 코드가
없었다.** 정책값은 콘솔 화면과 운영 전환 차단 조건만 봤다. 그래서 체크리스트는
통과하는데 아무것도 만료되지 않았고, 사람은 체크된 것을 보증으로 읽었다.

스키마 주석이 막겠다던 것은 「값이 없으면 기본값이 영구 보관이 된다」 였다.
값이 있어도 그랬다. 그쪽이 더 나쁘다 — 눈에 안 보인다.

## 만료 기준은 **원래 좌표**다

파일 mtime 을 쓰지 않는다. 소급 수집은 몇 년 전 대화를 **오늘** 파일로 쓰므로,
mtime 으로 세면 90일이 그날부터 새로 시작한다. 2년 전 DM 이 오늘 수집되면
2년 하고도 90일을 더 보관하게 된다.

- QA 감사 기록 — 레코드의 `ts`(KST ISO)
- DM 원문 — 줄의 Slack `message_ts`
- DM 첨부 원본 — 그 원본을 가리키는 **DM 원문 줄**의 `message_ts`
- 인계 중인 사본 — 봉인 안의 `message_ts`

## 모르면 안 지운다

좌표를 못 읽은 것은 「오래됐다」 가 아니라 **「모른다」** 다. 지우면 되돌릴 수
없고, 안 지우면 다음 실행에서 다시 본다. 두 선택의 값이 다르다.

## 아카이브 원문은 재작성하지 않는다

DM 원문 파일은 **모든 줄이 만료됐을 때만 통째로** 지운다. 줄을 골라 지우면
그건 원문을 고치는 것이고(절대 원칙 1), 남은 줄의 `lineno` 가 어긋나 기존
출처가 다른 문장을 가리킨다.

감사 JSONL 은 아카이브가 아니므로 줄 단위로 거른다. 다만 **append 중인 파일**
이라, 읽은 뒤 크기가 그대로일 때만 바꿔 끼운다(아래 `_filter_jsonl`).

## 지우는 순서

첨부 원본을 **먼저** 지우고 DM 원문을 나중에 지운다. 원본의 좌표는 원문 줄에서
나오므로, 반대로 하면 중간에 멈췄을 때 남은 원본의 만료 여부를 **영영 알 수
없다.** 이 순서면 멈춰도 다음 실행이 이어서 한다.

## 기본은 미리보기

`plan()` 은 아무것도 안 지운다. `apply()` 를 따로 불러야 지운다. CLI 도 같다 —
`--apply` 없이는 건수만 보여 준다.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from .archive.store import RAW_LINE_RE, split_stamp

log = logging.getLogger("tybot.retention")

KST = timezone(timedelta(hours=9))

#: 정책 이름. `archive_retention_policy.name` 과 **같아야 한다** — 다르면 콘솔이
#: 정한 값과 여기서 집행하는 값이 갈린다.
POLICY_BOT_AUDIT = "bot_conversation_audit"
POLICY_DM_MESSAGE = "bot_dm_message"
POLICY_DM_ATTACHMENT = "bot_dm_attachment"

POLICIES: tuple[str, ...] = (POLICY_BOT_AUDIT, POLICY_DM_MESSAGE, POLICY_DM_ATTACHMENT)

#: 이 정책들이 **다루지 않는** 사본. 지우지 않지만 **세어서 보여 준다** —
#: 안 보이면 「90일 집행 완료」 로 읽히고, 사본은 남는다.
#:
#: `journald` 는 systemd 가 자기 보존 설정으로 돌린다. 여기서 못 지우므로
#: 적어도 **그런 사본이 있다는 사실**을 운영자에게 보여야 한다.
UNMANAGED_COPIES: tuple[tuple[str, str], ...] = (
    ("journald", "봇 서비스 로그. systemd 의 자체 보존 설정으로만 지워집니다"),
    ("archive-read.jsonl", "콘솔 열람 감사. 봇 대화가 아니라 별도 정책 대상입니다"),
    ("env-settings.jsonl", "설정 변경 감사. 봇 대화가 아니라 별도 정책 대상입니다"),
)

#: DM 원문에 남는 첨부 참조. `dm_consumer` 가 쓰는 모양과 **같아야 한다**.
_OBJECT_REFERENCE = re.compile(r"object=(?P<key>[0-9a-f]{64})")
_QA_JSONL = re.compile(r"qa-\d{4}-\d{2}\.jsonl\Z")
_FEEDBACK_JSONL = re.compile(r"feedback-\d{4}-\d{2}\.jsonl\Z")
_DAILY_MD = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})\.md\Z")

STATE_FILE = "retention-run.json"


class RetentionRefused(RuntimeError):
    """집행할 수 없다. **사유를 사람 말로 들고 있다.**"""


@dataclass(frozen=True)
class Target:
    """지울 것 하나. **적용 전에 그대로 보여 줄 수 있는 모양**이다."""

    policy: str
    path: Path
    #: `file` 은 통째로, `lines` 는 만료된 줄만 걸러 다시 쓴다.
    kind: str
    #: 이 대상에서 만료된 건수. 파일이면 1, 줄이면 만료된 줄 수다.
    count: int
    #: 가장 늦은 만료 시각. 화면이 「언제 지워지나」 를 답할 근거다.
    newest: str = ""
    expired_lines: tuple[int, ...] = ()


@dataclass(frozen=True)
class Unresolved:
    """좌표를 못 읽어 **건드리지 않은** 것. 숨기면 집행됐다고 오해한다."""

    policy: str
    path: Path
    reason: str


@dataclass
class Plan:
    """한 번의 집행 범위. `apply()` 전에는 **아무것도 바뀌지 않았다.**"""

    days: dict[str, int | None]
    targets: list[Target] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: 이번 실행이 **실제로 본** 정책들. 범위 밖이거나 기간이 미결정이면 빠진다.
    #: 「돌았다」 와 「전부 돌았다」 를 가르는 값이다 — 한 정책만 돌린 실행을
    #: 집행으로 인정하면 나머지 자료는 영원히 안 지워진 채로 통과한다.
    covered: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """모든 정책을 봤나. **하나라도 빠지면 집행이 아니다.**"""
        return set(self.covered) == set(POLICIES)

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(POLICIES, 0)
        for target in self.targets:
            out[target.policy] = out.get(target.policy, 0) + target.count
        return out

    @property
    def empty(self) -> bool:
        return not self.targets


# ---------------------------------------------------------------------------
# 만료 판정
# ---------------------------------------------------------------------------


#: 어떤 좌표도 이보다 이르지 않다. 「아무것도 만료되지 않음」 을 뜻한다.
NOTHING_EXPIRES = datetime.min.replace(tzinfo=UTC)


def cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)


def _expired_epoch(message_ts: str, limit: datetime) -> bool | None:
    """Slack 좌표가 만료됐나. 읽을 수 없으면 `None`."""
    try:
        when = datetime.fromtimestamp(float(message_ts), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return when < limit


def _expired_kst_iso(stamp: str, limit: datetime) -> bool | None:
    """QA 기록의 `ts`(KST ISO, 시간대 표기 없음)가 만료됐나."""
    text = str(stamp or "")[:19]
    try:
        when = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=KST)
    except ValueError:
        return None
    return when < limit


# ---------------------------------------------------------------------------
# 어디에 무엇이 있나
# ---------------------------------------------------------------------------


def scan_qa_logs(qa_dir: Path | str, *, limit: datetime) -> tuple[list, list]:
    """봇↔사람 대화 감사 기록.

    **빈 `bot_conversation_audit` 표가 아니라 이 파일들이 실제 보관처다.** 표에는
    쓰는 코드도 읽는 코드도 없다(2026-10-02 조사). 이름만 보고 표를 지우면
    아무것도 안 지워진 채로 「집행했다」 가 된다.
    """
    root = Path(qa_dir)
    targets: list[Target] = []
    unresolved: list[Unresolved] = []
    if not root.is_dir():
        return targets, unresolved

    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        daily = _DAILY_MD.fullmatch(path.name)
        if daily:
            target = _expired_daily_md(path, daily.group("date"), limit)
            if target is not None:
                targets.append(target)
            continue
        if _QA_JSONL.fullmatch(path.name) or _FEEDBACK_JSONL.fullmatch(path.name):
            found, bad = _expired_jsonl_lines(path, limit)
            if found is not None:
                targets.append(found)
            unresolved.extend(bad)
    return targets, unresolved


def _expired_daily_md(path: Path, date: str, limit: datetime) -> Target | None:
    """일자별 MD. **그 날짜가 지나면** 통째로 지운다.

    하루치가 한 파일이라 날짜 단위로 정확하다. 자정에 쓴 기록을 하루 더 두는
    대신, 줄을 골라 지우지 않는다.
    """
    try:
        when = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=KST)
    except ValueError:  # pragma: no cover - 정규식을 지났으면 파싱된다
        return None
    # 그 날 **마지막 순간**까지 보관한다. 날짜만 보고 지우면 23:59 기록이
    # 하루 일찍 사라진다.
    if when + timedelta(days=1) >= limit:
        return None
    return Target(POLICY_BOT_AUDIT, path, "file", 1, newest=date)


def _expired_jsonl_lines(path: Path, limit: datetime) -> tuple[Target | None, list]:
    """월별 JSONL 에서 만료된 **줄**을 센다.

    월 단위로 통째로 지우면 90일 정책이 최대 119일이 된다. 감사 기록은
    아카이브가 아니므로 줄 단위로 거르는 것이 정책에 맞다.
    """
    unresolved: list[Unresolved] = []
    expired: list[int] = []
    newest = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return None, [Unresolved(POLICY_BOT_AUDIT, path, f"읽지 못했습니다({exc.strerror})")]

    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # 깨진 줄은 **남긴다.** 좌표를 모르는 것을 지우는 것은 추측이다.
            unresolved.append(Unresolved(POLICY_BOT_AUDIT, path, "읽을 수 없는 줄이 있습니다"))
            continue
        stamp = str(row.get("ts") or row.get("at") or "")
        verdict = _expired_kst_iso(stamp, limit)
        if verdict is None:
            unresolved.append(Unresolved(POLICY_BOT_AUDIT, path, "시각을 읽을 수 없는 줄이 있습니다"))
            continue
        if verdict:
            expired.append(index)
            newest = max(newest, stamp[:19])
    if not expired:
        return None, unresolved
    return (
        Target(POLICY_BOT_AUDIT, path, "lines", len(expired),
               newest=newest, expired_lines=tuple(expired)),
        unresolved,
    )


#: DM 원문 파일의 실제 자리. `shadow_paths.dm_archive_dir()` 이 주는
#: `<root>/<ws>/dm/<user>/archive` 아래에 **`writer` 가 `raw/` 를 하나 더 만든다**
#: (`writer._ingest_locked`: `directory / "raw" / f"{day}.md"`).
#:
#: 처음엔 `archive/*.md` 로 적었다. 그러면 아무것도 안 걸리고, 걸린 것이 없으니
#: 「만료 대상 0건」 이 나온다. 오류는 없다 — **집행했는데 아무것도 안 지워진다.**
#: 그리고 첨부 좌표도 원문 줄에서 나오므로, 그 순간 모든 첨부가 「가리키는 줄이
#: 없음」 이 되어 함께 멈춘다.
DM_RAW_GLOB = "*/dm/*/archive/raw/*.md"


def dm_archive_files(archive_root: Path | str) -> list[Path]:
    """`<root>/<ws>/dm/<user>/archive/raw/*.md`. 사람끼리의 DM 은 여기 없다."""
    root = Path(archive_root)
    if not root.is_dir():
        return []
    return sorted(root.glob(DM_RAW_GLOB))


def scan_dm_messages(
    archive_root: Path | str, *, limit: datetime
) -> tuple[list, list, dict]:
    """봇↔사람 1:1 DM 원문.

    **모든 줄이 만료된 파일만** 통째로 지운다. 한 줄이라도 안 지났으면 건너뛴다 —
    줄을 골라 지우면 원문을 고치는 것이고, 남은 줄의 `lineno` 가 어긋나 기존
    출처가 다른 문장을 가리킨다.

    세 번째 값은 `object key -> message_ts` 다. 첨부 원본의 만료 기준이 여기서
    나온다 — 봉인 안의 좌표는 열어야 보이고, 원문 줄은 안 열어도 보인다.
    """
    targets: list[Target] = []
    unresolved: list[Unresolved] = []
    coordinates: dict[str, str] = {}

    for path in dm_archive_files(archive_root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            unresolved.append(
                Unresolved(POLICY_DM_MESSAGE, path, f"읽지 못했습니다({exc.strerror})")
            )
            continue
        stamps: list[str] = []
        unreadable = False
        for line in text.splitlines():
            matched = RAW_LINE_RE.match(line.strip())
            if matched is None:
                # 프론트매터·제목 줄이다. 원문 줄이 아니므로 만료 판정에 안 쓴다.
                continue
            _display, message_ts = split_stamp(matched.group("ts"))
            if not message_ts:
                # 좌표 없는 옛 줄. **모르는 것이지 오래된 것이 아니다.**
                unreadable = True
                continue
            for reference in _OBJECT_REFERENCE.finditer(matched.group("text")):
                coordinates[reference.group("key")] = message_ts
            stamps.append(message_ts)
        if unreadable:
            unresolved.append(
                Unresolved(POLICY_DM_MESSAGE, path, "좌표 없는 원문 줄이 있습니다")
            )
            continue
        if not stamps:
            continue
        verdicts = [_expired_epoch(stamp, limit) for stamp in stamps]
        if any(v is None for v in verdicts):
            unresolved.append(
                Unresolved(POLICY_DM_MESSAGE, path, "읽을 수 없는 좌표가 있습니다")
            )
            continue
        if not all(verdicts):
            continue
        targets.append(
            Target(POLICY_DM_MESSAGE, path, "file", 1, newest=max(stamps))
        )
    return targets, unresolved, coordinates


def scan_dm_objects(
    archive_root: Path | str, *, limit: datetime, coordinates: dict[str, str]
) -> tuple[list, list]:
    """DM 첨부 **원본 바이트**(봉인된 `.bin`).

    좌표는 `coordinates` 가 준다 — 그 원본을 가리키는 원문 줄에서 읽은 값이다.
    봉인을 열지 않는 이유는 둘이다. 열려면 키가 필요하고, 지우려고 남의 첨부
    본문을 복호화할 이유가 없다.

    가리키는 줄이 없는 원본은 **안 지운다.** 좌표를 모르는 것이지 오래된 것이
    아니다.
    """
    root = Path(archive_root)
    targets: list[Target] = []
    unresolved: list[Unresolved] = []
    if not root.is_dir():
        return targets, unresolved

    for path in sorted(root.glob("*/dm/*/objects/*/files/*.bin")):
        key = path.stem
        stamp = coordinates.get(key, "")
        if not stamp:
            unresolved.append(
                Unresolved(POLICY_DM_ATTACHMENT, path, "가리키는 원문 줄이 없습니다")
            )
            continue
        verdict = _expired_epoch(stamp, limit)
        if verdict is None:
            unresolved.append(
                Unresolved(POLICY_DM_ATTACHMENT, path, "좌표를 읽을 수 없습니다")
            )
            continue
        if verdict:
            targets.append(Target(POLICY_DM_ATTACHMENT, path, "file", 1, newest=stamp))
    return targets, unresolved


def scan_dm_spool(
    handoff_dir: Path | str | None, *, limit: datetime, reader=None
) -> tuple[list, list]:
    """아직 Archiver 로 넘어가지 않은 **인계 중인 사본**.

    큐지만 사본은 사본이다. 소비가 막히면 여기 그대로 남고, 보존 기간은
    그것을 모른다.

    경로는 `ARCHIVER_DM_HANDOFF_DIR` 이 정한다(`archiving_bot` 과 같은 값).
    `STATE_DIR` 아래라고 짐작하지 않는다 — 짐작한 경로는 **언제나 비어 있고**,
    비어 있으면 「지울 것이 없다」 로 보인다.

    좌표는 봉인 안에 있다. `reader(workspace, key)` 가 `message_ts` 를 돌려주고,
    못 읽으면 **안 지운다** — 큐 파일의 mtime 으로 세면 소급 수집한 옛 대화가
    오늘 받은 것으로 보인다.
    """
    targets: list[Target] = []
    unresolved: list[Unresolved] = []
    if not handoff_dir:
        return targets, unresolved
    root = Path(handoff_dir)
    if not root.is_dir():
        return targets, unresolved

    for path in sorted(root.glob("*/pending/*.bin")):
        workspace = path.parent.parent.name
        stamp = ""
        if reader is not None:
            try:
                stamp = str(reader(workspace, path.stem) or "")
            except Exception as exc:  # noqa: BLE001 - 못 읽으면 안 지운다
                log.warning("인계 사본의 좌표를 읽지 못했다: %s (%s)", path, exc)
                stamp = ""
        verdict = _expired_epoch(stamp, limit) if stamp else None
        if verdict is None:
            unresolved.append(
                Unresolved(POLICY_DM_MESSAGE, path, "봉인 안의 좌표를 읽지 못했습니다")
            )
            continue
        if verdict:
            targets.append(Target(POLICY_DM_MESSAGE, path, "file", 1, newest=stamp))
    return targets, unresolved


# ---------------------------------------------------------------------------
# 계획
# ---------------------------------------------------------------------------


def plan(
    *,
    days: dict[str, int | None],
    qa_dir: Path | str,
    archive_root: Path | str,
    handoff_dir: Path | str | None = None,
    now: datetime | None = None,
    policies: tuple[str, ...] = POLICIES,
    spool_reader=None,
) -> Plan:
    """무엇을 지울지 **세기만** 한다. 파일은 건드리지 않는다.

    `days[정책]` 이 `None` 이면 **미결정**이고, 미결정은 「영구」 가 아니라
    「건드리지 않음」 이다. 값이 없는 채로 지우면 아무도 정하지 않은 기간으로
    자료가 사라진다.
    """
    stamp = now or datetime.now(UTC)
    result = Plan(days=dict(days))

    for policy in POLICIES:
        if policy not in policies:
            result.skipped.append(f"{policy}: 이번 범위에서 제외")
        elif days.get(policy) is None:
            result.skipped.append(f"{policy}: 보존 기간이 미결정이라 지우지 않습니다")

    covered: list[str] = []

    def active(policy: str) -> datetime | None:
        if policy not in policies or days.get(policy) is None:
            return None
        covered.append(policy)
        return cutoff(stamp, int(days[policy]))

    audit_limit = active(POLICY_BOT_AUDIT)
    if audit_limit is not None:
        targets, unresolved = scan_qa_logs(qa_dir, limit=audit_limit)
        result.targets.extend(targets)
        result.unresolved.extend(unresolved)

    # 첨부 좌표는 원문에서 나온다. 그래서 DM 원문을 **먼저** 훑는다 —
    # 정책이 꺼져 있어도 훑는다(좌표만 쓰고 지우지 않는다).
    message_limit = active(POLICY_DM_MESSAGE)
    # 정책이 꺼져 있으면 **아무것도 만료되지 않는** 경계로 훑는다. 좌표만
    # 거두고 지우지는 않는다 — 첨부 정책이 켜져 있을 수 있기 때문이다.
    scan_limit = message_limit or NOTHING_EXPIRES
    messages, message_unresolved, coordinates = scan_dm_messages(
        archive_root, limit=scan_limit
    )
    if message_limit is not None:
        result.targets.extend(messages)
        result.unresolved.extend(message_unresolved)
        if handoff_dir and spool_reader is None:
            # 읽을 수단이 없으면 **봤다고 하지 않는다.** 0 건으로 보고하면
            # 인계 중인 사본이 영원히 남은 채로 「집행 완료」 가 된다.
            result.skipped.append(
                "인계 중인 사본: 봉인을 열 수단이 없어 보지 않았습니다"
            )
        else:
            spool, spool_unresolved = scan_dm_spool(
                handoff_dir, limit=message_limit, reader=spool_reader
            )
            result.targets.extend(spool)
            result.unresolved.extend(spool_unresolved)
        if not handoff_dir:
            result.skipped.append(
                "인계 중인 사본: ARCHIVER_DM_HANDOFF_DIR 이 없어 보지 않았습니다"
            )

    attachment_limit = active(POLICY_DM_ATTACHMENT)
    if attachment_limit is not None:
        objects, object_unresolved = scan_dm_objects(
            archive_root, limit=attachment_limit, coordinates=coordinates
        )
        result.targets.extend(objects)
        result.unresolved.extend(object_unresolved)
    result.covered = tuple(covered)
    return result


# ---------------------------------------------------------------------------
# 적용
# ---------------------------------------------------------------------------

#: 지우는 순서. 첨부 원본이 **먼저**다 — 그 좌표는 DM 원문 줄에서 나오므로,
#: 원문을 먼저 지우면 중간에 멈췄을 때 남은 원본의 만료 여부를 영영 모른다.
_ORDER = (POLICY_DM_ATTACHMENT, POLICY_DM_MESSAGE, POLICY_BOT_AUDIT)


def apply(ready: Plan) -> dict:
    """계획대로 지운다. **계획에 없는 것은 건드리지 않는다.**"""
    done = dict.fromkeys(POLICIES, 0)
    failed: list[str] = []
    for policy in _ORDER:
        for target in [t for t in ready.targets if t.policy == policy]:
            try:
                removed = _remove(target)
            except OSError as exc:
                failed.append(f"{target.path}: {exc.strerror}")
                continue
            done[policy] = done.get(policy, 0) + removed
    return {"removed": done, "failed": failed}


def _remove(target: Target) -> int:
    if target.kind == "file":
        target.path.unlink(missing_ok=True)
        return 1
    return _filter_jsonl(target)


def _size_of(path: Path) -> int:
    """지금 크기. **두 번 부르는 사이에 커졌는지** 보기 위한 자리다."""
    return path.stat().st_size


def _filter_jsonl(target: Target) -> int:
    """만료된 줄만 빼고 다시 쓴다. **덧붙는 중이면 건너뛴다.**

    이 파일들은 append 로 자란다. 읽은 뒤 바꿔 끼우는 사이에 한 줄이 덧붙으면
    그 줄은 사라지고, 사라진 감사 기록은 사라진 줄 모른다.

    쓰는 쪽은 잠그지 않으므로(`audit.QALog.write`) 여기서 잠가도 소용없다.
    대신 **크기를 견준다** — append 만 하는 파일은 크기가 그대로면 아무것도
    덧붙지 않았다는 뜻이다. 달라졌으면 이번엔 두고 다음 실행에서 한다.
    """
    from .audit import append_lock
    from .lock import AlreadyRunning, LockUnavailable

    path = target.path
    lock = append_lock(path)
    try:
        lock.acquire(timeout=LOCK_TIMEOUT)
    except (AlreadyRunning, LockUnavailable, OSError) as exc:
        # 덧붙이는 쪽이 쥐고 있다. **이번엔 두고 간다** — 기다리다 끼어들면
        # 그 사이의 기록을 잃는다. 다음 실행에서 다시 본다.
        log.info("감사 기록 락을 못 잡아 이번에는 두고 간다: %s (%s)", path, exc)
        return 0
    try:
        return _swap_filtered(target)
    finally:
        lock.release()


#: 덧붙이기가 끝나기를 기다리는 시간. 한 줄 쓰기는 밀리초라 넉넉하다.
LOCK_TIMEOUT = 5.0


def _swap_filtered(target: Target) -> int:
    path = target.path
    before = _size_of(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    drop = set(target.expired_lines)
    kept = [line for index, line in enumerate(lines) if index not in drop]
    temporary = path.with_name(f".{path.name}.retention.{os.getpid()}.tmp")
    body = "".join(f"{line}\n" for line in kept)
    temporary.write_text(body, encoding="utf-8")
    try:
        if _size_of(path) != before:
            log.info("집행 중 덧붙은 파일이라 이번에는 두고 간다: %s", path)
            return 0
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return len(drop)


# ---------------------------------------------------------------------------
# 집행 기록 — **설정값이 아니라 실행이 근거다**
# ---------------------------------------------------------------------------


def state_path(state_dir: Path | str | None = None) -> Path:
    base = Path(state_dir) if state_dir else Path(
        os.getenv("STATE_DIR", "").strip() or "/var/lib/tybot"
    )
    return base / "state" / STATE_FILE


def record_run(
    ready: Plan, result: dict, *, actor: str, applied: bool,
    state_dir: Path | str | None = None, now: datetime | None = None,
) -> Path:
    """언제 누가 무엇을 지웠나. **미리보기도 남긴다**(안 지운 것도 사실이다)."""
    path = state_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "by": actor.strip() or "unknown",
        "applied": bool(applied),
        "days": ready.days,
        "planned": ready.counts(),
        "removed": result.get("removed", {}),
        "failed": result.get("failed", []),
        "unresolved": len(ready.unresolved),
        "skipped": list(ready.skipped),
        "covered": list(ready.covered),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def last_run(state_dir: Path | str | None = None) -> dict | None:
    """마지막 집행. 없거나 못 읽으면 `None` — **「안 했다」 로 읽는다.**"""
    path = state_path(state_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("보존 집행 기록을 읽지 못했다: %s", path)
        return None


#: 이 기간 안에 **적용**이 한 번은 있어야 집행 중이라고 본다. 타이머가 하루
#: 한 번 돌 예정이므로 넉넉히 잡는다 — 짧게 잡으면 한 번 거른 날 운영이 막힌다.
ENFORCEMENT_FRESH_DAYS = 7


def enforcement_status(
    state_dir: Path | str | None = None, *, now: datetime | None = None
) -> tuple[bool, str]:
    """집행이 **실제로 돌고 있나.** 돌지 않으면 사유를 문장으로 든다.

    정책값이 있다는 사실을 집행 완료로 읽지 않기 위한 자리다(B-70).
    """
    record = last_run(state_dir)
    if record is None:
        return False, "보존 집행이 한 번도 실행되지 않았습니다"
    if not record.get("applied"):
        return False, "보존 집행이 미리보기로만 실행됐습니다"
    stamp = str(record.get("at") or "")
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return False, "보존 집행 기록의 시각을 읽을 수 없습니다"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    age = (now or datetime.now(UTC)) - when
    if age > timedelta(days=ENFORCEMENT_FRESH_DAYS):
        return False, f"마지막 보존 집행이 {age.days}일 전입니다"

    # **돌았다 ≠ 전부 돌았다.** 한 정책만 돌린 실행을 집행으로 인정하면 나머지
    # 자료는 영원히 안 지워진 채로 게이트를 통과한다. `covered` 가 없는 옛
    # 기록도 통과시키지 않는다 — 모르는 것을 「전부 돌았다」 로 읽지 않는다.
    missing = sorted(set(POLICIES) - set(record.get("covered") or ()))
    if missing:
        return False, f"마지막 집행이 보지 않은 정책이 있습니다: {', '.join(missing)}"

    # **실패가 있으면 집행이 아니다.** 지우려 했는데 못 지운 자료가 남아 있고,
    # 그걸 성공으로 세면 다음 실행까지 아무도 모른다.
    failed = list(record.get("failed") or ())
    if failed:
        return False, f"마지막 집행에서 {len(failed)}건을 지우지 못했습니다"
    return True, ""


# ---------------------------------------------------------------------------
# CLI — 콘솔(B-71)이 같은 함수를 부른다
# ---------------------------------------------------------------------------


def _report(ready: Plan) -> None:
    counts = ready.counts()
    print("보존 기간:")
    for policy in POLICIES:
        value = ready.days.get(policy)
        print(f"  {policy:24s} {value if value is not None else '미결정'}")
    print("만료 대상:")
    for policy in POLICIES:
        print(f"  {policy:24s} {counts.get(policy, 0):6d} 건")
    for note in ready.skipped:
        print(f"  건너뜀 — {note}")
    if ready.unresolved:
        print(f"좌표를 몰라 건드리지 않은 것 {len(ready.unresolved)} 건:")
        for item in ready.unresolved[:20]:
            print(f"  {item.policy} {item.path}: {item.reason}")
        if len(ready.unresolved) > 20:
            print(f"  … 외 {len(ready.unresolved) - 20}건")
    print("이 정책이 지우지 않는 사본:")
    for name, why in UNMANAGED_COPIES:
        print(f"  {name:24s} {why}")


def spool_reader(handoff_dir: str | Path | None):
    """인계 사본의 **좌표만** 꺼내는 함수. 못 만들면 `None`.

    `DmInbox` 가 봉인을 연다 — 키는 `archiving_bot` 이 쓰는 것과 **같은 것**이다
    (`console.workspace_store._fernet`). 두 벌이면 하나는 열지 못하고, 못 연
    것은 「만료 안 됨」 으로 남아 영원히 쌓인다.

    돌려주는 것은 `message_ts` 하나다. 본문·파일명은 꺼내지 않는다 — 지우려고
    남의 DM 을 읽을 이유가 없다.
    """
    if not handoff_dir:
        return None
    try:
        from .archive.dm_inbox import DmInbox
        from .console.workspace_store import _fernet

        inbox = DmInbox(Path(handoff_dir), _fernet())
    except Exception as exc:  # noqa: BLE001 - 못 만들면 그 사본은 안 본다
        log.warning("인계 사본을 열 수단을 만들지 못했다: %s", exc)
        return None

    def read(workspace: str, key: str) -> str:
        return inbox.read(workspace, key).message_ts

    return read


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .envfile import load_env_file

    parser = argparse.ArgumentParser(description="보존 기간을 실제 자료에 적용한다")
    parser.add_argument("--apply", action="store_true", help="실제로 지운다(기본은 미리보기)")
    parser.add_argument("--by", default="", help="실행한 사람. --apply 에 필요하다")
    parser.add_argument(
        "--policy", action="append", choices=POLICIES,
        help="이 정책만. 여러 번 줄 수 있다(기본: 전부)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_env_file()
    if args.apply and not args.by.strip():
        print("--apply 에는 --by 가 필요합니다. 누가 지웠는지 없는 기록은 감사에 못 씁니다.")
        return 2

    from .console.archiving_repo import default_repo
    from .paths import archive_dir

    try:
        rows = default_repo().retention()
    except Exception as exc:  # noqa: BLE001 - 정책을 못 읽으면 아무것도 안 지운다
        print(f"보존 정책을 읽지 못했습니다: {exc}")
        return 2
    days = {str(row["name"]): row["retention_days"] for row in rows}
    missing = [name for name in POLICIES if name not in days]
    if missing:
        print(f"정책 항목이 없습니다: {', '.join(missing)}. 스키마를 먼저 적용하세요.")
        return 2

    handoff_dir = os.getenv("ARCHIVER_DM_HANDOFF_DIR", "").strip()
    ready = plan(
        days=days,
        qa_dir=os.getenv("QA_LOG_DIR", "./qa-log"),
        archive_root=archive_dir(),
        handoff_dir=handoff_dir,
        spool_reader=spool_reader(handoff_dir),
        policies=tuple(args.policy) if args.policy else POLICIES,
    )
    _report(ready)
    if not args.apply:
        print("\n실제로 지우려면 `--apply --by <이름>` 을 붙이세요.")
        record_run(ready, {"removed": {}, "failed": []}, actor="preview", applied=False)
        return 0

    result = apply(ready)
    record_run(ready, result, actor=args.by, applied=True)
    print("\n지운 건수:")
    for policy in POLICIES:
        print(f"  {policy:24s} {result['removed'].get(policy, 0):6d} 건")
    for note in result["failed"]:
        print(f"  실패 — {note}")
    return 1 if result["failed"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
