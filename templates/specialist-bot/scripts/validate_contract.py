"""Validate a prompt-contract specialist repository without executing its code."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?$")
REQUIRED = {
    "schema", "key", "name", "domain", "release_type",
    "contract_version", "version", "artifacts",
}
FORBIDDEN_PROMPT_TEXT = ("출처:", "slack.com/archives/", "file://", "DATABASE_URL")


def fail(message: str) -> None:
    raise ValueError(message)


def artifact_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        fail("artifact 경로는 비어 있지 않은 문자열이어야 합니다.")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "contract":
        fail(f"artifact는 contract/ 아래의 상대 경로여야 합니다: {value}")
    candidate = ROOT / relative
    if candidate.is_symlink():
        fail(f"artifact는 심볼릭 링크일 수 없습니다: {value}")
    path = candidate.resolve()
    contract_root = (ROOT / "contract").resolve()
    if contract_root not in path.parents or not path.is_file():
        fail(f"일반 파일인 artifact를 찾을 수 없습니다: {value}")
    return path


def validate_cases(path: Path) -> None:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        fail("계약 테스트 사례가 하나 이상 필요합니다.")
    required = {"id", "question", "evidence", "must_include", "must_not_include"}
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict) or set(case) != required:
            fail(f"테스트 사례 {index}의 필드가 계약과 다릅니다.")
        if not isinstance(case["evidence"], list) or not case["evidence"]:
            fail(f"테스트 사례 {index}에는 근거가 하나 이상 필요합니다.")
        for key in ("id", "question"):
            if not isinstance(case[key], str) or not case[key].strip():
                fail(f"테스트 사례 {index}의 {key}가 비었습니다.")
        for key in ("evidence", "must_include", "must_not_include"):
            if not all(isinstance(item, str) and item.strip() for item in case[key]):
                fail(f"테스트 사례 {index}의 {key} 값이 잘못됐습니다.")


def main() -> int:
    manifest_path = ROOT / "tybot-specialist.toml"
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest) != REQUIRED:
        fail(f"매니페스트 필드가 계약과 다릅니다: {sorted(set(manifest) ^ REQUIRED)}")
    if manifest["schema"] != "tybot-specialist/v1":
        fail("지원하지 않는 매니페스트 스키마입니다.")
    if not isinstance(manifest["key"], str) or not KEY_RE.fullmatch(manifest["key"]):
        fail("key는 영문 소문자로 시작하는 2~32자의 소문자·숫자·하이픈이어야 합니다.")
    if manifest["release_type"] != "prompt-contract" or manifest["contract_version"] != "v1":
        fail("현재는 prompt-contract의 v1 계약만 지원합니다.")
    if not isinstance(manifest["version"], str) or not VERSION_RE.fullmatch(manifest["version"]):
        fail("version은 SemVer 형식이어야 합니다.")
    if not all(isinstance(manifest[key], str) and manifest[key].strip() for key in ("name", "domain")):
        fail("name과 domain은 비어 있을 수 없습니다.")
    if not isinstance(manifest["artifacts"], list) or not manifest["artifacts"]:
        fail("artifact가 하나 이상 필요합니다.")

    files = [artifact_path(value) for value in manifest["artifacts"]]
    prompt = ROOT / "contract" / "prompts" / "system.md"
    cases = ROOT / "contract" / "tests" / "cases.json"
    if prompt not in files or cases not in files:
        fail("system.md와 cases.json은 필수 artifact입니다.")
    prompt_text = prompt.read_text(encoding="utf-8")
    found = [value for value in FORBIDDEN_PROMPT_TEXT if value in prompt_text]
    if found:
        fail(f"프롬프트에 마스터 전용 값이 포함됐습니다: {', '.join(found)}")
    validate_cases(cases)

    result = {
        "key": manifest["key"],
        "version": manifest["version"],
        "contract_version": manifest["contract_version"],
        "artifacts": {
            str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"계약 검사 실패: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
