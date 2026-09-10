"""전문 봇 2단계 소스 검역 — 받되 실행하지 않는다.

설계: [`../../../docs/design/specialist-runtime-v2.md`](../../../docs/design/specialist-runtime-v2.md)
구현 순서 4단계.

## 1단계 계약 ZIP 과 왜 분리하나

1단계(`specialist_zip`)는 **프롬프트 계약**만 받는다 — 2MB, 30개, 512KB. 여기는
**전체 소스**라 25MB·2,000개·100MB 다. 같은 함수에 상한만 넓혀 붙이면, 프롬프트
계약 업로드가 조용히 25MB 를 받게 된다. 계약이 둘이면 코드도 둘이어야 한다.

## 콘솔은 압축을 풀지 않는다

콘솔 프로세스는 ZIP **색인만** 읽고 원본 바이트를 검역 경로에 그대로 둔다.
푸는 것은 격리 builder 의 일이다(5단계). 콘솔이 풀면, 경로 탈출·심볼릭 링크·
압축 폭탄이 **콘솔이 쓸 수 있는 모든 곳**에 닿는다.

검역 파일명은 **서버가 만든 UUID** 다. 업로드한 이름을 경로로 쓰면 그 이름이
곧 쓰기 위치가 된다.

## 무엇을 거부하나

거부 목록은 "위험해 보여서" 가 아니라 **빌드에 필요 없는데 위험한 것**이다.
`node_modules` 는 lockfile 로 다시 만들면 되고, `.git` 은 빌드 입력이 아니며,
`.env`·키 파일은 애초에 소스에 있으면 안 된다. 중첩 아카이브는 검사를 한 겹
건너뛰는 통로다 — 우리가 본 것은 바깥 껍데기뿐이다.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import stat
import tomllib
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

# 2단계 상한. 설계표와 같은 값이다.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ENTRIES = 2_000
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100

# 검역 보존 기간. 빌드하지 않은 제출물이 무한히 쌓이면 디스크가 찬다.
QUARANTINE_TTL = timedelta(days=7)

SCHEMA = "tybot-specialist/v2"
CONTRACT_VERSION = "v2"
MANIFEST_NAME = "tybot-specialist.toml"
LOCKFILE_NAME = "package-lock.json"

# **첫 구현은 하나뿐이다.** 임의 이미지·Dockerfile·shell entrypoint 를 받지 않는다.
ALLOWED_RUNTIMES = frozenset({"nodejs22"})
ALLOWED_NETWORK_PROFILES = frozenset({"none", "anthropic-only"})
ALLOWED_RELEASE_TYPES = frozenset({"http-service"})

KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
PATH_RE = re.compile(r"^/[A-Za-z0-9/_-]{1,64}$")
# 불변 태그만. `main`·`latest`·`HEAD` 처럼 움직이는 것은 빌드 입력이 아니다.
TAG_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?$")
MOVING_REFS = frozenset({"latest", "main", "master", "head", "dev", "develop", "trunk"})

GITHUB_HOST = "github.com"

# 빌드에 필요 없는데 위험한 것들.
FORBIDDEN_DIRS = ("node_modules", ".git", ".hg", ".svn", "__pycache__")
FORBIDDEN_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".jks", ".keystore",
    ".pyc", ".so", ".dll", ".dylib", ".node",
)
# 중첩 아카이브는 검사를 한 겹 건너뛰는 통로다.
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar", ".jar")
FORBIDDEN_NAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".npmrc", ".netrc")


class SourceError(RuntimeError):
    """검역 거부. 문구가 그대로 개발자에게 보인다 — 무엇을 고칠지 말해야 한다."""


@dataclass(frozen=True)
class Manifest:
    """`tybot-specialist.toml` v2. 실행 코드가 아니라 선언이다."""

    key: str
    name: str
    domain: str
    version: str
    runtime: str
    entrypoint: tuple[str, ...]
    health_path: str
    complete_path: str
    network_profile: str


@dataclass(frozen=True)
class Bundle:
    """검사를 통과한 제출물. **내용은 담지 않는다** — 해시와 수치뿐이다."""

    manifest: Manifest
    bundle_sha256: str
    entries: int
    uncompressed_bytes: int
    root: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


# --- ZIP ---------------------------------------------------------------------
def _member(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise SourceError(f"ZIP 내부 경로 형식이 안전하지 않습니다: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SourceError(f"ZIP 내부 경로가 묶음 밖을 가리킵니다: {value}")
    return path


def _refuse_forbidden(path: PurePosixPath) -> None:
    parts = [p.lower() for p in path.parts]
    for bad in FORBIDDEN_DIRS:
        if bad in parts:
            raise SourceError(f"`{bad}` 는 넣지 않습니다(빌드가 다시 만듭니다): {path}")
    name = path.name.lower()
    if name in FORBIDDEN_NAMES or name.startswith(".env"):
        raise SourceError(f"자격·환경 파일은 소스에 있으면 안 됩니다: {path}")
    for suffix in FORBIDDEN_SUFFIXES:
        if name.endswith(suffix):
            raise SourceError(f"허용하지 않는 파일 형식입니다: {path}")
    for suffix in ARCHIVE_SUFFIXES:
        if name.endswith(suffix):
            # 우리가 본 것은 바깥 껍데기뿐이다.
            raise SourceError(f"중첩 아카이브는 받지 않습니다: {path}")


def inspect_zip(content: bytes, filename: str) -> Bundle:
    """ZIP 색인을 읽어 검사한다. **압축을 풀지 않는다.**

    매니페스트와 lockfile 만 메모리에서 열어 본다 — 그 둘은 우리가 판정에 써야
    하고, 크기가 상한 안이라는 것을 이미 확인한 뒤다.
    """
    if not filename.lower().endswith(".zip") or "/" in filename or "\\" in filename:
        raise SourceError("파일명은 경로가 없는 .zip 이어야 합니다.")
    if not content:
        raise SourceError("빈 파일입니다.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise SourceError(
            f"소스 ZIP 은 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 이하여야 합니다."
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SourceError("올바른 ZIP 파일이 아닙니다.") from exc

    with archive:
        infos = archive.infolist()
        if not infos:
            raise SourceError("ZIP 이 비어 있습니다.")
        if len(infos) > MAX_ENTRIES:
            raise SourceError(f"ZIP 항목이 {MAX_ENTRIES}개를 넘습니다({len(infos)}).")

        seen: set[str] = set()
        total = 0
        manifests: list[tuple[PurePosixPath, zipfile.ZipInfo]] = []
        for info in infos:
            path = _member(info.filename)
            if info.flag_bits & 0x1:
                raise SourceError("암호화된 ZIP 은 받지 않습니다.")
            mode = (info.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(mode)
            if kind == stat.S_IFLNK:
                # 심볼릭 링크는 푸는 쪽에서 묶음 밖을 가리킬 수 있다.
                raise SourceError(f"심볼릭 링크는 받지 않습니다: {path}")
            if info.is_dir():
                continue
            if kind not in (0, stat.S_IFREG):
                raise SourceError(f"일반 파일만 받습니다: {path}")
            if mode & 0o111:
                raise SourceError(f"실행 권한이 붙은 파일은 받지 않습니다: {path}")
            _refuse_forbidden(path)

            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise SourceError(
                    "압축 해제 합계가 "
                    f"{MAX_UNCOMPRESSED_BYTES // (1024 * 1024)}MB 를 넘습니다."
                )
            if (
                info.file_size > 4096
                and info.file_size > max(info.compress_size, 1) * MAX_COMPRESSION_RATIO
            ):
                raise SourceError(f"비정상적으로 압축률이 높은 항목입니다: {path}")

            normalized = str(path)
            if normalized in seen:
                raise SourceError(f"ZIP 에 중복 경로가 있습니다: {path}")
            seen.add(normalized)
            if path.name == MANIFEST_NAME:
                manifests.append((path, info))

        if len(manifests) != 1:
            raise SourceError(f"ZIP 에는 {MANIFEST_NAME} 이 정확히 하나 있어야 합니다.")
        manifest_path, manifest_info = manifests[0]
        root = manifest_path.parent
        if manifest_info.file_size > 64 * 1024:
            raise SourceError("매니페스트가 너무 큽니다.")

        manifest = parse_manifest(archive.read(manifest_info))

        # lockfile 은 **필수**다. 없으면 빌드가 그때그때 다른 것을 설치하고,
        # 승인한 digest 와 다음 빌드의 digest 가 달라진다.
        lock = str(root / LOCKFILE_NAME) if str(root) != "." else LOCKFILE_NAME
        if lock not in seen:
            raise SourceError(f"{LOCKFILE_NAME} 이 없습니다(`npm ci` 가 필요합니다).")
        for rival in ("yarn.lock", "pnpm-lock.yaml", "bun.lockb"):
            candidate = str(root / rival) if str(root) != "." else rival
            if candidate in seen:
                raise SourceError(f"첫 구현은 npm 만 지원합니다: {rival}")

    return Bundle(
        manifest=manifest,
        bundle_sha256=hashlib.sha256(content).hexdigest(),
        entries=len(seen),
        uncompressed_bytes=total,
        root=str(root),
    )


# --- 매니페스트 ---------------------------------------------------------------
def parse_manifest(raw: bytes) -> Manifest:
    """`tybot-specialist.toml` v2. **모르는 값을 통과시키지 않는다.**"""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SourceError("매니페스트가 UTF-8 이 아닙니다.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SourceError(f"매니페스트를 읽지 못했습니다: {exc}") from exc

    if data.get("schema") != SCHEMA:
        raise SourceError(f"schema 는 `{SCHEMA}` 여야 합니다.")
    if data.get("contract_version") != CONTRACT_VERSION:
        raise SourceError(f"contract_version 은 `{CONTRACT_VERSION}` 여야 합니다.")
    if data.get("release_type") not in ALLOWED_RELEASE_TYPES:
        raise SourceError(
            f"release_type 은 {sorted(ALLOWED_RELEASE_TYPES)} 중 하나여야 합니다."
        )

    key = str(data.get("key") or "")
    if not KEY_RE.match(key):
        raise SourceError("key 는 소문자·숫자·하이픈 2~32자여야 합니다.")
    version = str(data.get("version") or "")
    if not VERSION_RE.match(version):
        raise SourceError("version 은 `1.2.0` 형식이어야 합니다.")

    runtime = str(data.get("runtime") or "")
    if runtime not in ALLOWED_RUNTIMES:
        raise SourceError(
            f"runtime 은 {sorted(ALLOWED_RUNTIMES)} 만 지원합니다(받은 값: {runtime!r})."
        )

    entrypoint = data.get("entrypoint")
    if (
        not isinstance(entrypoint, list)
        or not entrypoint
        or len(entrypoint) > 8
        or not all(isinstance(a, str) and a.strip() for a in entrypoint)
    ):
        raise SourceError("entrypoint 는 1~8개의 문자열 배열이어야 합니다.")
    # **셸을 거치지 않는다.** 셸을 끼우면 매니페스트 한 줄이 임의 명령이 된다.
    if entrypoint[0] not in ("node",):
        raise SourceError("entrypoint 는 `node` 로 시작해야 합니다.")
    for arg in entrypoint:
        if any(ch in arg for ch in ";|&`$><\n"):
            raise SourceError(f"entrypoint 인자에 셸 문자가 있습니다: {arg!r}")

    health_path = str(data.get("health_path") or "")
    complete_path = str(data.get("complete_path") or "")
    for label, value in (("health_path", health_path), ("complete_path", complete_path)):
        if not PATH_RE.match(value):
            raise SourceError(f"{label} 는 `/v1/...` 형식이어야 합니다.")
    if health_path == complete_path:
        raise SourceError("health_path 와 complete_path 가 같습니다.")

    profile = str(data.get("network_profile") or "none")
    if profile not in ALLOWED_NETWORK_PROFILES:
        raise SourceError(
            f"network_profile 은 {sorted(ALLOWED_NETWORK_PROFILES)} 중 하나여야 합니다."
        )

    name = str(data.get("name") or "").strip()
    domain = str(data.get("domain") or "").strip()
    if not name or not domain:
        raise SourceError("name 과 domain 이 필요합니다.")

    return Manifest(
        key=key, name=name, domain=domain, version=version, runtime=runtime,
        entrypoint=tuple(entrypoint), health_path=health_path,
        complete_path=complete_path, network_profile=profile,
    )


# --- Git ---------------------------------------------------------------------
def validate_git_source(repository_url: str, release_ref: str) -> tuple[str, str]:
    """`(정규화한 URL, 태그)`. 공개 HTTPS GitHub 와 **불변 태그**만 받는다.

    브랜치와 움직이는 `latest` 를 빌드 입력으로 쓰지 않는 이유는 하나다 —
    승인한 것과 나중에 도는 것이 달라진다. 승인은 그 순간의 코드에 대한 것이다.
    """
    import urllib.parse

    url = (repository_url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise SourceError("공개 HTTPS 저장소만 받습니다.")
    if parsed.hostname not in (GITHUB_HOST, f"www.{GITHUB_HOST}"):
        raise SourceError(f"{GITHUB_HOST} 저장소만 받습니다.")
    if parsed.username or parsed.password or parsed.port:
        # 자격이 URL 에 실려 있으면 그 값이 DB·로그·화면에 남는다.
        raise SourceError("URL 에 자격이나 포트를 넣지 마세요.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2:
        raise SourceError("저장소 URL 은 `https://github.com/<소유자>/<저장소>` 형식입니다.")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not re.match(r"^[A-Za-z0-9._-]{1,100}$", owner) or not re.match(
        r"^[A-Za-z0-9._-]{1,100}$", repo
    ):
        raise SourceError("저장소 경로 형식이 올바르지 않습니다.")

    ref = (release_ref or "").strip()
    if ref.lower() in MOVING_REFS:
        raise SourceError(
            f"`{ref}` 는 움직입니다. 불변 태그(`v1.2.0`)를 지정하세요 — "
            "승인한 것과 도는 것이 달라집니다."
        )
    if not TAG_RE.match(ref):
        raise SourceError("태그는 `v1.2.0` 형식이어야 합니다.")

    return f"https://{GITHUB_HOST}/{owner}/{repo}", ref


def refuse_repository_shape(entries: list[str]) -> None:
    """submodule·LFS·중첩 저장소를 첫 구현에서 거부한다.

    셋 다 **체크아웃한 것이 곧 빌드 입력이 아니게** 만든다 — 우리가 해시한
    커밋 밖에서 내용이 들어온다.
    """
    lowered = [e.strip().lower() for e in entries]
    if ".gitmodules" in lowered:
        raise SourceError("submodule 은 첫 구현에서 받지 않습니다.")
    if ".gitattributes" in lowered:
        # LFS 포인터는 내용이 아니라 주소다. 빌드가 그 주소로 나가야 한다.
        raise SourceError("Git LFS 는 첫 구현에서 받지 않습니다(.gitattributes).")
    for entry in lowered:
        if entry != ".git" and entry.endswith("/.git"):
            raise SourceError(f"중첩 Git 저장소가 있습니다: {entry}")


# --- 검역 저장 ----------------------------------------------------------------
def quarantine_root(base: str | os.PathLike[str]) -> Path:
    return Path(base) / "subbot-quarantine"


def store(content: bytes, base: str | os.PathLike[str]) -> str:
    """원본 바이트를 검역에 둔다. `quarantine_key` 를 돌려준다.

    **파일명은 서버가 만든 UUID** 다. 업로드한 이름을 쓰면 그 이름이 곧 쓰기
    위치가 되고, 경로 탈출 검사가 하나라도 새면 그대로 파일시스템에 닿는다.

    권한은 소유자만이다. 이 디렉터리는 `noexec` 로 마운트하는 것이 전제지만,
    그것이 안 돼 있어도 실행 비트를 주지 않는다.
    """
    root = quarantine_root(base)
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    key = f"{uuid.uuid4().hex}.zip"
    path = root / key
    tmp = path.with_suffix(".part")
    tmp.write_bytes(content)
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    tmp.replace(path)
    return key


def path_for(base: str | os.PathLike[str], quarantine_key: str) -> Path:
    """검역 키 → 경로. **키 형식을 다시 본다** — DB 를 통해서 온 값이라도
    그대로 경로에 붙이지 않는다."""
    if not re.fullmatch(r"[0-9a-f]{32}\.zip", quarantine_key or ""):
        raise SourceError("검역 키 형식이 올바르지 않습니다.")
    return quarantine_root(base) / quarantine_key


def sweep(base: str | os.PathLike[str], *, now: datetime | None = None) -> int:
    """오래된 검역 파일을 지운다. 지운 개수를 돌려준다.

    빌드하지 않은 제출물이 무한히 쌓이면 디스크가 차고, 그때 멈추는 것은
    업로드가 아니라 아카이브 쓰기다.
    """
    root = quarantine_root(base)
    if not root.is_dir():
        return 0
    cutoff = (now or datetime.now(UTC)) - QUARANTINE_TTL
    removed = 0
    for path in root.iterdir():
        if not path.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
    return removed


__all__ = [
    "ALLOWED_NETWORK_PROFILES",
    "ALLOWED_RUNTIMES",
    "MAX_ENTRIES",
    "MAX_UNCOMPRESSED_BYTES",
    "MAX_UPLOAD_BYTES",
    "QUARANTINE_TTL",
    "SCHEMA",
    "Bundle",
    "Manifest",
    "SourceError",
    "inspect_zip",
    "parse_manifest",
    "path_for",
    "quarantine_root",
    "refuse_repository_shape",
    "store",
    "sweep",
    "validate_git_source",
]
