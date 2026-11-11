"""매일 새벽 변환 재점검 — 「바뀐 것만」 다시 연다 (B-89).

## 무엇을 다시 여나

야간 배치(`drain_conversion_queue.py --nightly`)는 네 갈래를 본다.

1. **일시 실패** — 변환기 crash·timeout 처럼 `retryable` 로 남은 것. 전부터 하던 일이다
2. **판정이 바뀐 실패** — 그때는 「미지원」·「암호」·「손상」·「빈 출력」 이었지만
   그 판정을 내린 변환기가 지금과 다르다. 같은 변환기로 다시 돌리면 결과가 같으므로
   **변환기가 바뀐 것만** 연다
3. **구버전 성공** — 성공했지만 옛 변환기로 읽은 것. 새 판으로 나란히 다시 읽는다
4. **rhwp 위임 미완료** — HWP 를 변환 앱에 넘겼는데 그 PDF 가 아직 안 들어온 것.
   TYBot 이 직접 변환하지 않는다(위임 중에 변환하면 같은 문서가 두 벌의 근거가
   된다). **세어서 알리기만** 한다

## 다시 열지 않는 것

- `pii_refused` — 정책 제외다. 변환기가 바뀌어도 사람이 막은 것을 기계가 풀지 않는다
- `original_changed`·`original_missing` — 원본이 다르거나 없다. 다시 돌릴 대상이 없다
- 같은 변환기가 내린 영구 판정 — 몇 번을 돌려도 같다

이 모듈은 **판정만** 한다. 큐 쓰기·파일 읽기는 호출부(스크립트)가 하므로 DB 없이
시험할 수 있다.
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

#: 변환기가 바뀌어도 다시 열지 않는 코드. 정책 제외와 원본 문제다.
NEVER_REOPEN = frozenset({"pii_refused", "original_changed", "original_missing"})

#: 같은 변환기라면 결과가 같은 영구 판정. **변환기가 바뀌었을 때만** 연다.
VERDICT_CODES = frozenset({"unsupported", "encrypted", "corrupt", "empty_output"})

#: 판정이 바뀌어 다시 여는 작업의 사유 코드. 큐 정책상 재시도 가능 코드다.
CHANGED_CODE = "converter_changed"

#: rhwp 에 넘긴 뒤 PDF 가 들어오기를 기다리는 시간. 넘으면 「미완료」 로 센다.
DEFAULT_RHWP_GRACE = timedelta(hours=6)

#: 한 밤에 「판정 변경」·「구버전」 으로 새로 올리는 작업 상한. 소급은 비용이 드는
#: 작업이고, 변환기 판을 올린 첫날 밤에 수천 건이 한꺼번에 몰리면 그 밤이 장애가 된다.
#: 남은 것은 다음 밤에 이어진다 — 큐 키에 변환기 판이 들어가 같은 일을 두 번 하지 않는다.
DEFAULT_RECHECK_LIMIT = 50
RECHECK_LIMIT_ENV = "TYBOT_CONVERT_RECHECK_LIMIT"
RHWP_GRACE_ENV = "TYBOT_CONVERT_RHWP_GRACE_HOURS"

#: 마지막 야간 실행 결과. staging 밑에 두는 이유는 변환 서비스가 쓸 수 있는 곳이
#: staging·archive 둘뿐이기 때문이다(`tybot-convert-nightly.service` ReadWritePaths).
REPORT_NAME = "convert-nightly-last.json"


def recheck_limit(env: dict | None = None) -> int:
    """환경에서 상한을 읽는다. 틀린 값은 기본값으로 — 0 은 「소급하지 않음」 이다."""
    raw = (env if env is not None else os.environ).get(RECHECK_LIMIT_ENV, "")
    try:
        value = int(str(raw).strip())
    except ValueError:
        return DEFAULT_RECHECK_LIMIT
    return max(0, value)


def rhwp_grace(env: dict | None = None) -> timedelta:
    raw = (env if env is not None else os.environ).get(RHWP_GRACE_ENV, "")
    try:
        hours = float(str(raw).strip())
    except ValueError:
        return DEFAULT_RHWP_GRACE
    return timedelta(hours=hours) if hours > 0 else DEFAULT_RHWP_GRACE


def verdict_changed(
    *,
    status: str,
    error_code: str,
    recorded_version: str,
    current_version: str,
    convertible_now: bool,
) -> bool:
    """이 영구 판정을 지금 변환기로 다시 내려 볼 만한가.

    - 「미지원」 은 **지금 변환할 수 있으면** 연다. 그때 변환기가 이 형식을
      다루지 않았다는 뜻이고(또는 rhwp 위임이 꺼졌다), 지금은 다룬다
    - 나머지 판정은 **기록된 변환기 판이 지금과 다를 때만** 연다. 판이 기록되지
      않은 옛 metadata 는 열지 않는다 — 무엇이 바뀌었는지 모르는데 다시 돌리면
      「바뀐 것만」 이 아니라 「전부」 가 된다
    """
    code = (error_code or "").strip()
    if code in NEVER_REOPEN or status == "pii_refused":
        return False
    if status == "unsupported" or code == "unsupported":
        return convertible_now
    if code not in VERDICT_CODES:
        return False
    if not convertible_now:
        return False
    recorded = (recorded_version or "").strip()
    return bool(recorded) and recorded != current_version


def _stem(name: str) -> str:
    """파일 이름의 비교용 줄기. 확장자·대소문자·유니코드 정규화 차이를 지운다."""
    base = PurePosixPath(unicodedata.normalize("NFC", name or "").strip()).stem
    return base.casefold().strip()


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class PendingDelegation:
    """PDF 가 아직 안 들어온 rhwp 위임 원본. **좌표만** 담는다 — 파일 이름은 담지 않는다."""

    workspace: str
    channel_id: str
    file_id: str
    staged_at: str


def pending_delegations(items, *, now: datetime, grace: timedelta) -> list[PendingDelegation]:
    """rhwp 에 넘긴 HWP 중 같은 채널에 같은 이름의 PDF 가 아직 없는 것.

    **대조는 이름 줄기로 한다.** rhwp 는 `보고서.hwp` 를 `보고서.pdf` 로 올린다.
    원본과 PDF 를 잇는 좌표가 Slack 에 없어서 이것이 가진 가장 좁은 단서다. 그래서
    이 결과는 **알림**에만 쓰고 근거·권한 판정에는 쓰지 않는다. 이름을 바꿔 올린
    경우는 미완료로 보일 수 있다 — 놓치는 것보다 사람이 한 번 보는 쪽이 낫다.

    받은 지 `grace` 가 안 된 것은 세지 않는다. 앱이 변환하는 중이다.
    """
    pdf_stems: set[tuple[str, str, str]] = set()
    delegated = []
    for item in items:
        if getattr(item, "delegated", False):
            delegated.append(item)
        elif (item.filetype or "").lower() == "pdf":
            pdf_stems.add((item.workspace, item.channel_id, _stem(item.name)))
    out = []
    for item in delegated:
        staged = _parse_time(getattr(item, "staged_at", "") or "")
        if staged is not None and now - staged < grace:
            continue
        if (item.workspace, item.channel_id, _stem(item.name)) in pdf_stems:
            continue
        out.append(
            PendingDelegation(
                workspace=item.workspace,
                channel_id=item.channel_id,
                file_id=item.file_id,
                staged_at=getattr(item, "staged_at", "") or "",
            )
        )
    return out


@dataclass
class NightlyReport:
    """야간 한 회차의 운영 지표. 본문·파일 이름·사람 정보는 넣지 않는다."""

    started_at: str = ""
    finished_at: str = ""
    retry_candidates: int = 0
    retry_enqueued: int = 0
    changed_candidates: int = 0
    changed_enqueued: int = 0
    outdated_candidates: int = 0
    outdated_enqueued: int = 0
    recheck_limit: int = DEFAULT_RECHECK_LIMIT
    deferred_to_next_night: int = 0
    excluded_policy: int = 0
    original_missing: int = 0
    rhwp_delegated: int = 0
    rhwp_pending: int = 0
    rhwp_pending_files: list[dict] = field(default_factory=list)
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    exit_code: int = 0

    @property
    def success_rate(self) -> float | None:
        """재처리 성공률. 처리한 것이 없으면 **모른다**(None) — 0% 로 적지 않는다."""
        return round(self.succeeded / self.processed, 4) if self.processed else None

    def to_json(self) -> dict:
        data = asdict(self)
        data["success_rate"] = self.success_rate
        return data


def report_path(archive_dir: str | Path) -> Path:
    """`attachment_review.staging_root` 의 형제 자리. 그 밑(`workspaces`)은 스캔 대상이다."""
    return Path(archive_dir).parent / "staging" / "_reports" / REPORT_NAME


def write_report(archive_dir: str | Path, report: NightlyReport) -> Path:
    """원자적으로 쓴다. 반쯤 쓴 파일을 콘솔이 읽으면 「0건」 으로 보인다."""
    path = report_path(archive_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(report.to_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def read_report(archive_dir: str | Path) -> dict | None:
    try:
        return json.loads(report_path(archive_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


__all__ = [
    "CHANGED_CODE",
    "DEFAULT_RECHECK_LIMIT",
    "DEFAULT_RHWP_GRACE",
    "NEVER_REOPEN",
    "VERDICT_CODES",
    "NightlyReport",
    "PendingDelegation",
    "pending_delegations",
    "read_report",
    "recheck_limit",
    "report_path",
    "rhwp_grace",
    "verdict_changed",
    "write_report",
]
