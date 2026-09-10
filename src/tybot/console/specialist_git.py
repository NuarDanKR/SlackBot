"""Read a versioned specialist contract from a public GitHub release.

Repository code is never checked out or executed.  A bare clone is used only to
read the manifest and the explicitly declared ``contract/`` blobs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

GITHUB_HOST = "github.com"
MAX_ARTIFACTS = 20
MAX_ARTIFACT_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024
MAX_RULES_CHARS = 8000
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
REQUIRED_MANIFEST = {
    "schema", "key", "name", "domain", "release_type",
    "contract_version", "version", "artifacts",
}
FORBIDDEN_PROMPT_TEXT = ("출처:", "slack.com/archives/", "file://", "DATABASE_URL")
CASE_FIELDS = {"id", "question", "evidence", "must_include", "must_not_include"}


class SpecialistGitError(RuntimeError):
    """A remote specialist release could not be imported safely."""


def normalize_repository_url(value: str) -> tuple[str, str, str]:
    raw = value.strip()
    parsed = urllib.parse.urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname != GITHUB_HOST
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
    ):
        raise SpecialistGitError(
            "공개 GitHub HTTPS 저장소 URL만 사용할 수 있습니다. 자격증명, 쿼리와 fragment는 허용하지 않습니다."
        )
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise SpecialistGitError("GitHub 저장소 URL은 https://github.com/소유자/저장소 형식이어야 합니다.")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    component_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    if not component_re.fullmatch(owner) or not component_re.fullmatch(repository):
        raise SpecialistGitError("GitHub 소유자 또는 저장소 이름 형식이 잘못됐습니다.")
    return f"https://github.com/{owner}/{repository}.git", owner, repository


def _latest_release(owner: str, repository: str) -> str:
    url = f"https://api.github.com/repos/{owner}/{repository}/releases/latest"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "TYBot-specialist-contract-importer/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read(MAX_ARTIFACT_BYTES).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SpecialistGitError(
                "게시된 GitHub Release가 없습니다. 전문 봇 개발자가 먼저 불변 릴리스 태그를 게시해야 합니다."
            ) from exc
        raise SpecialistGitError(f"GitHub 릴리스 조회 실패: HTTP {exc.code}") from exc
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SpecialistGitError(f"GitHub 릴리스 조회 실패: {exc}") from exc
    tag = str(payload.get("tag_name") or "").strip()
    if not TAG_RE.fullmatch(tag):
        raise SpecialistGitError("최신 GitHub Release의 태그 형식이 안전하지 않습니다.")
    return tag


def _git_env(home: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": home,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "LANG": "C.UTF-8",
    }
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    for key in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        if value := os.environ.get(key):
            env[key] = value
    return env


def _run_git(args: list[str], *, env: dict[str, str], binary: bool = False) -> bytes | str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", *args],
            check=False,
            capture_output=True,
            text=not binary,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpecialistGitError(f"Git 저장소 조회 실패: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace") if binary else result.stderr
        detail = str(stderr or "git 명령 실패").strip().splitlines()[-1][:300]
        raise SpecialistGitError(f"Git 저장소 조회 실패: {detail}")
    return result.stdout


def _read_blob(git_dir: str, commit: str, path: str, *, env: dict[str, str]) -> bytes:
    listing = str(_run_git(["--git-dir", git_dir, "ls-tree", commit, "--", path], env=env)).strip()
    if not listing:
        raise SpecialistGitError(f"릴리스에 계약 파일이 없습니다: {path}")
    metadata, _, listed_path = listing.partition("\t")
    mode, kind, _object_id = metadata.split(" ", 2)
    if listed_path != path or mode != "100644" or kind != "blob":
        raise SpecialistGitError(f"계약 파일은 일반 비실행 파일이어야 합니다: {path}")
    size_text = str(_run_git(["--git-dir", git_dir, "cat-file", "-s", f"{commit}:{path}"], env=env)).strip()
    try:
        size = int(size_text)
    except ValueError as exc:
        raise SpecialistGitError(f"계약 파일 크기를 확인하지 못했습니다: {path}") from exc
    if size > MAX_ARTIFACT_BYTES:
        raise SpecialistGitError(f"계약 파일이 {MAX_ARTIFACT_BYTES // 1024}KB를 넘습니다: {path}")
    return bytes(_run_git(["--git-dir", git_dir, "show", f"{commit}:{path}"], env=env, binary=True))


def _artifact_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise SpecialistGitError("artifact 경로는 contract/ 아래의 POSIX 상대 경로여야 합니다.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "contract":
        raise SpecialistGitError(f"artifact는 contract/ 아래에 있어야 합니다: {value}")
    return str(path)


def _validate_cases(cases: object) -> int:
    if not isinstance(cases, list) or not cases:
        raise SpecialistGitError("계약 테스트 사례가 하나 이상 필요합니다.")
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict) or set(case) != CASE_FIELDS:
            raise SpecialistGitError(f"테스트 사례 {index}의 필드가 계약과 다릅니다.")
        for key in ("id", "question"):
            if not isinstance(case[key], str) or not case[key].strip():
                raise SpecialistGitError(f"테스트 사례 {index}의 {key}가 비었습니다.")
        for key in ("evidence", "must_include", "must_not_include"):
            values = case[key]
            if not isinstance(values, list) or not all(
                isinstance(item, str) and item.strip() for item in values
            ):
                raise SpecialistGitError(f"테스트 사례 {index}의 {key} 값이 잘못됐습니다.")
        if not case["evidence"]:
            raise SpecialistGitError(f"테스트 사례 {index}에는 근거가 하나 이상 필요합니다.")
    return len(cases)


def import_release(repository_url: str, release: str = "latest") -> dict:
    normalized_url, owner, repository = normalize_repository_url(repository_url)
    requested_release = release.strip() or "latest"
    tag = _latest_release(owner, repository) if requested_release == "latest" else requested_release
    if not TAG_RE.fullmatch(tag):
        raise SpecialistGitError("릴리스는 안전한 Git 태그 이름이어야 합니다.")

    with tempfile.TemporaryDirectory(prefix="tybot-specialist-") as temp:
        git_dir = os.path.join(temp, "repository.git")
        env = _git_env(temp)
        _run_git(
            ["clone", "--bare", "--filter=blob:none", "--depth", "1", "--branch", tag, normalized_url, git_dir],
            env=env,
        )
        commit = str(_run_git(["--git-dir", git_dir, "rev-parse", "HEAD^{commit}"], env=env)).strip()
        if not COMMIT_RE.fullmatch(commit):
            raise SpecialistGitError("릴리스 커밋 SHA를 확인하지 못했습니다.")
        manifest_bytes = _read_blob(git_dir, commit, "tybot-specialist.toml", env=env)
        try:
            manifest = tomllib.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise SpecialistGitError(f"전문 봇 매니페스트를 읽지 못했습니다: {exc}") from exc
        if set(manifest) != REQUIRED_MANIFEST:
            raise SpecialistGitError(
                f"매니페스트 필드가 계약과 다릅니다: {sorted(set(manifest) ^ REQUIRED_MANIFEST)}"
            )
        if manifest["schema"] != "tybot-specialist/v1":
            raise SpecialistGitError("지원하지 않는 전문 봇 매니페스트 스키마입니다.")
        if manifest["release_type"] != "prompt-contract" or manifest["contract_version"] != "v1":
            raise SpecialistGitError("현재는 prompt-contract v1 릴리스만 가져올 수 있습니다.")
        key = str(manifest["key"])
        version = str(manifest["version"])
        if not KEY_RE.fullmatch(key) or not VERSION_RE.fullmatch(version):
            raise SpecialistGitError("전문 봇 키 또는 SemVer 버전 형식이 잘못됐습니다.")
        if tag.removeprefix("v") != version:
            raise SpecialistGitError(f"릴리스 태그({tag})와 매니페스트 버전({version})이 다릅니다.")
        if not all(
            isinstance(manifest[name], str) and 1 <= len(manifest[name].strip()) <= 80
            for name in ("name", "domain")
        ):
            raise SpecialistGitError("전문 봇 이름과 담당 분야는 비어 있을 수 없습니다.")
        raw_artifacts = manifest["artifacts"]
        if not isinstance(raw_artifacts, list) or not 1 <= len(raw_artifacts) <= MAX_ARTIFACTS:
            raise SpecialistGitError(f"artifact는 1~{MAX_ARTIFACTS}개여야 합니다.")
        artifacts = [_artifact_path(value) for value in raw_artifacts]
        if len(set(artifacts)) != len(artifacts):
            raise SpecialistGitError("중복된 artifact 경로가 있습니다.")
        prompt_path = "contract/prompts/system.md"
        cases_path = "contract/tests/cases.json"
        if prompt_path not in artifacts or cases_path not in artifacts:
            raise SpecialistGitError("system.md와 cases.json은 필수 artifact입니다.")

        blobs = {path: _read_blob(git_dir, commit, path, env=env) for path in artifacts}
        if sum(len(value) for value in blobs.values()) > MAX_TOTAL_BYTES:
            raise SpecialistGitError("계약 파일 전체 크기가 256KB를 넘습니다.")
        try:
            rules = blobs[prompt_path].decode("utf-8").strip()
            cases = json.loads(blobs[cases_path].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpecialistGitError(f"계약 파일을 읽지 못했습니다: {exc}") from exc
        if not rules or len(rules) > MAX_RULES_CHARS:
            raise SpecialistGitError(f"답변 규칙은 1~{MAX_RULES_CHARS}자여야 합니다.")
        found = [text for text in FORBIDDEN_PROMPT_TEXT if text in rules]
        if found:
            raise SpecialistGitError(f"답변 규칙에 마스터 전용 값이 포함됐습니다: {', '.join(found)}")
        case_count = _validate_cases(cases)

        return {
            "repositoryUrl": normalized_url.removesuffix(".git"),
            "releaseRef": tag,
            "sourceCommit": commit,
            "artifactHashes": {
                path: hashlib.sha256(content).hexdigest() for path, content in blobs.items()
            },
            "key": key,
            "name": str(manifest["name"]).strip(),
            "domain": str(manifest["domain"]).strip(),
            "adapter": key,
            "version": version,
            "contractVersion": str(manifest["contract_version"]),
            "rules": rules,
            "checks": [
                {"id": "repository", "state": "pass", "detail": f"{owner}/{repository}@{tag}"},
                {"id": "commit", "state": "pass", "detail": commit},
                {
                    "id": "contract",
                    "state": "pass",
                    "detail": f"artifact {len(artifacts)}개 · 테스트 {case_count}개 검증",
                },
            ],
        }
