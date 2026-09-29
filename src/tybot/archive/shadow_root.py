"""흩어진 shadow root 를 **하나로** 모은다.

설계: `docs/design/archiver-supervisor-backfill-console-2026-09-29.md` §1 · §5 · §7-5

## 무엇이 문제였나

파일럿은 워크스페이스마다 root 를 따로 줬다.

```text
/var/lib/tybot/archiver-shadow/tyit/archive/workspaces/tyit/channels/...
/var/lib/tybot/archiver-shadow/mgmt/archive/workspaces/mgmt/channels/...
```

`ArchiveStore` 는 이미 `workspaces/<key>/` 로 나눈다. 그 위에 워크스페이스를 한 번 더
붙이면 **같은 구분이 두 층**이 되고, root 하나만 읽는 supervisor 는 한 워크스페이스만
본다. 나머지는 오류 없이 안 보인다 — 파일은 그대로 있고 근거만 사라진다.

## 이 모듈이 정하는 것

| 무엇 | 규칙 |
|---|---|
| 옮기는 단위 | `<base>/archive` · `<base>/objects` · `<base>/staging` 세 갈래 |
| 상대경로 | 그대로 유지한다. `workspaces/<ws>/...` 가 곧 목적지 경로다 |
| 원본 | **지우지 않는다.** 복사만 하고 검증 기간 동안 남긴다(§5-6) |
| 같은 내용 | 건너뛴다. 다시 실행해도 같은 결과다 |
| 다른 내용 | **거부**한다. 덮어쓰면 어느 쪽이 원문인지 알 수 없게 된다 |
| 남의 워크스페이스 | 거부한다. root 를 잘못 적은 것이고, 그대로 옮기면 권한이 섞인다 |

## 왜 세 갈래인가

첨부는 archive root 의 **형제**에 쌓인다(`files.attachment_storage` — `objects/`,
`staging/`). archive 만 옮기면 원문은 새 root 에, 첨부 정본은 옛 root 에 남는다.
그 상태는 오류를 내지 않는다. 검색이 첨부만 못 찾을 뿐이다.

같은 이유로 목적지를 고를 때 **운영 archive 의 형제가 되면 안 된다.** 그러면 shadow
첨부가 운영 `objects/` 에 섞여 들어가고, 그건 되돌릴 수 없다(§8).

## 여기서 하지 않는 것

원본 삭제, systemd 조작, DB 갱신. 이 모듈은 파일만 본다.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

log = logging.getLogger("tybot.archive.shadow_root")

#: root 하나가 거느리는 갈래. `archive` 는 원문, 나머지 둘은 첨부다.
#:
#: `.locks` 는 뺀다 — 프로세스가 도는 동안의 자취이고, 옮기면 새 프로세스가 남의
#: 락을 쥔 것으로 본다.
AREAS = ("archive", "objects", "staging")

_CHUNK = 1 << 20


class MigrationRefused(RuntimeError):
    """이 계획으로는 옮기지 않는다. **사유를 사람 말로 들고 있다.**"""


def sha256_of(path: Path) -> str:
    """파일 하나의 내용 지문. 통째로 읽지 않는다 — 첨부는 크다."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceRoot:
    """옮겨 올 워크스페이스 하나. `base` 는 `archive` 의 **부모**다."""

    workspace: str
    base: Path


@dataclass(frozen=True)
class Item:
    """옮길 파일 하나. 계획과 검증이 같은 값을 본다."""

    area: str
    relative: str
    source: Path
    destination: Path
    sha256: str
    size: int
    mtime: float
    #: `copy`(목적지 없음) · `same`(같은 내용) · `conflict`(다른 내용)
    verdict: str

    def as_json(self) -> dict:
        return {
            "area": self.area,
            "relative": self.relative,
            "sha256": self.sha256,
            "size": self.size,
            "mtime": self.mtime,
            "verdict": self.verdict,
        }


@dataclass
class Plan:
    """무엇을 어디로 옮기나. **거부 사유가 하나라도 있으면 실행하지 않는다.**"""

    destination: Path
    items: list[Item] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    @property
    def copies(self) -> list[Item]:
        return [item for item in self.items if item.verdict == "copy"]

    @property
    def blocked(self) -> bool:
        return bool(self.refusals)

    def as_json(self) -> dict:
        counts: dict[str, int] = {"copy": 0, "same": 0, "conflict": 0}
        for item in self.items:
            counts[item.verdict] = counts.get(item.verdict, 0) + 1
        return {
            "destination": str(self.destination),
            "archiveRoot": str(archive_root(self.destination)),
            "counts": counts,
            "bytes": sum(item.size for item in self.items if item.verdict == "copy"),
            "refusals": list(self.refusals),
            "blocked": self.blocked,
            "items": [item.as_json() for item in self.items],
            "note": (
                "원본은 지우지 않습니다. 검증이 끝날 때까지 옛 root 를 읽기 전용으로"
                " 두고, 새 supervisor 가 공통 root 만 쓰는지 확인한 뒤에 정리하세요."
            ),
        }


def archive_root(destination: Path) -> Path:
    """공통 base 아래 **원문 root**. supervisor 의 `ARCHIVER_SHADOW_DIR` 이 될 값이다."""
    return Path(destination) / "archive"


def check_destination(destination: Path, live_archive: Path) -> list[str]:
    """목적지를 골라도 되나. **운영 자료와 섞이는 자리는 거부한다.**

    두 가지를 본다.

    - 원문 root 가 운영 archive 와 겹치는가. 겹치면 shadow 원문이 운영 근거가 된다
    - base 가 운영 archive 의 **부모와 같은가.** 첨부는 archive 의 형제에 쌓이므로,
      그 자리에 두면 shadow 첨부가 운영 `objects/` 안으로 들어간다. 경로가 다르니
      아무 오류도 나지 않고, 나중에 어느 것이 shadow 였는지 구분할 수 없다
    """
    refusals: list[str] = []
    base = Path(destination)
    live = Path(live_archive)
    if not base.is_absolute():
        refusals.append(f"공통 root 는 절대경로여야 합니다: {base}")
    if not live.is_absolute():
        refusals.append(f"운영 archive 경로는 절대경로여야 합니다: {live}")
    if refusals:
        return refusals

    root = archive_root(base)
    if root == live or root in live.parents or live in root.parents:
        refusals.append(
            f"공통 root 의 원문 경로 {root} 가 운영 archive {live} 와 겹칩니다."
            " shadow 자료가 운영 근거가 됩니다."
        )
    if base == live.parent:
        refusals.append(
            f"공통 root {base} 가 운영 archive 의 부모와 같습니다. 첨부는 archive 의"
            f" 형제({base / 'objects'})에 쌓이므로 운영 첨부와 섞입니다."
        )
    return refusals


def _walk(area_dir: Path) -> list[Path]:
    if not area_dir.is_dir():
        return []
    return sorted(path for path in area_dir.rglob("*") if path.is_file())


def plan(sources: list[SourceRoot], destination: Path, *, live_archive: Path) -> Plan:
    """무엇을 어디로 옮길지 센다. **아무것도 바꾸지 않는다.**

    `SourceRoot.workspace` 는 선언이고, 경로 안의 `workspaces/<key>` 가 사실이다.
    둘이 다르면 옮기지 않는다 — root 를 잘못 적은 것이고, 그대로 복사하면 한
    워크스페이스 자료가 다른 워크스페이스 ACL 아래로 들어간다(절대 원칙 3·4).
    """
    base = Path(destination)
    result = Plan(destination=base)
    result.refusals.extend(check_destination(base, Path(live_archive)))

    seen: dict[tuple[str, str], Path] = {}
    for source in sources:
        for area in AREAS:
            area_dir = Path(source.base) / area
            for path in _walk(area_dir):
                relative = PurePosixPath(path.relative_to(area_dir).as_posix())
                parts = relative.parts
                if len(parts) < 2 or parts[0] != "workspaces":
                    result.refusals.append(
                        f"{source.workspace}: {area}/{relative} 는 workspaces/ 아래가"
                        " 아닙니다. 옮길 자리를 정할 수 없습니다."
                    )
                    continue
                if parts[1] != source.workspace:
                    result.refusals.append(
                        f"{source.workspace} root 에 다른 워크스페이스 자료가 있습니다:"
                        f" {area}/{relative}. root 를 잘못 적었는지 먼저 확인하세요."
                    )
                    continue
                key = (area, str(relative))
                if key in seen:
                    result.refusals.append(
                        f"같은 상대경로를 두 root 가 주장합니다: {area}/{relative}"
                        f" ({seen[key]}, {path})."
                    )
                    continue
                seen[key] = path

                target = base / area / Path(*relative.parts)
                digest = sha256_of(path)
                stat = path.stat()
                verdict = "copy"
                if target.exists():
                    verdict = "same" if sha256_of(target) == digest else "conflict"
                    if verdict == "conflict":
                        result.refusals.append(
                            f"목적지에 다른 내용이 이미 있습니다: {area}/{relative}."
                            " 덮어쓰지 않습니다 — 어느 쪽이 원문인지 사람이 정해야 합니다."
                        )
                result.items.append(Item(
                    area=area, relative=str(relative), source=path, destination=target,
                    sha256=digest, size=stat.st_size, mtime=stat.st_mtime,
                    verdict=verdict,
                ))
    result.items.sort(key=lambda item: (item.area, item.relative))
    return result


def apply(plan_: Plan) -> dict:
    """계획대로 **복사만** 한다. 원본은 그대로 둔다.

    임시 파일에 쓰고 마지막에 이름을 바꾼다. 중간에 죽으면 목적지에는 반쪽 파일이
    아니라 **아무것도 없다** — 반쪽이 남으면 다음 실행이 그것을 `conflict` 로 보고
    사람을 세운다.
    """
    if plan_.blocked:
        raise MigrationRefused(
            "계획에 거부 사유가 있어 옮기지 않습니다:\n- " + "\n- ".join(plan_.refusals)
        )
    copied = 0
    for item in plan_.copies:
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        temp = item.destination.with_name(item.destination.name + ".partial")
        shutil.copy2(item.source, temp)
        os.replace(temp, item.destination)
        copied += 1
    log.info("공통 root 로 %d개 파일을 복사했다 dest=%s", copied, plan_.destination)
    return {
        "copied": copied,
        "skippedSame": sum(1 for item in plan_.items if item.verdict == "same"),
    }


def verify(plan_: Plan) -> list[str]:
    """복사본이 원본과 같은가. **목록·SHA-256 을 다시 센다**(§5-4).

    계획 때 잰 값을 믿지 않는다. 믿으면 복사가 실패해도 계획이 맞다고 말한다.
    """
    problems: list[str] = []
    for item in plan_.items:
        if item.verdict == "conflict":
            problems.append(f"{item.area}/{item.relative}: 내용이 달라 옮기지 않았습니다")
            continue
        if not item.destination.is_file():
            problems.append(f"{item.area}/{item.relative}: 복사본이 없습니다")
            continue
        if sha256_of(item.destination) != item.sha256:
            problems.append(f"{item.area}/{item.relative}: 복사본 내용이 원본과 다릅니다")
    return problems
