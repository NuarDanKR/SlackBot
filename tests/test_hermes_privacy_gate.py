"""`pf-archiver` 전환 관문 — **manifest 가 공개 여부의 권위다.**

설계: `docs/design/hermes-write-entrypoints.md` §7.12

## 무엇을 지키나

모르는 채널을 공개로 다루면 **오류가 아니라 평범한 답변**으로 내용이 나간다. 그래서
이 관문은 「엇갈렸다」 가 아니라 **「모른다」 에서도 막아야 한다.**

| 갈래 | 종료코드 |
|---|---|
| manifest 가 없다·못 읽는다·형식이 다르다·남의 워크스페이스다 | 2 |
| 정본에 있는데 manifest 에 없다(미확인) | 1 |
| manifest 가 비공개인데 `privateChannels` 에 없다 | 1 |
| manifest 가 공개인데 선언은 비공개 | 0 (경고) |
| manifest 에 있는데 정본에 아직 없다 | 0 (경고) |

## Hermes 는 DB 에 안 붙는다

자격증명을 PF 쪽에 두면 운영 DB 로 가는 길이 하나 더 생기고, 그 길은 읽기 전용이라는
보장이 없다. 받는 쪽은 JSON 한 장이다 — 그 사실을 소스로 고정한다.

manifest 는 **TYBot 쪽 exporter 가 만든 것**을 쓴다. 손으로 적으면 두 쪽의 키가
갈려도 시험이 통과한다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tybot.archive import privacy_manifest as M

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "subbots" / "hermes"
GATE = "scripts/check-archiver-privacy.js"
MAKE_FIXTURE = ROOT / "scripts" / "make_archiver_fixture.py"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None or not HERMES.is_dir(), reason="node 또는 subbots/hermes 가 없다"
)
needs_deps = pytest.mark.skipif(
    not (HERMES / "node_modules").is_dir(),
    reason="subbots/hermes/node_modules 가 없어 실행 시험은 건너뛴다",
)

# 정본 채널 셋. fixture 생성기가 만드는 것과 같아야 한다.
FUNDS = ("C1000FUNDS", "팀_자금(ABB540)_주간보고")
SITE = ("C2000SITE0", "현장_김해외동(180182)_채팅방")
PRIVATE = ("C3000PRIV0", "팀_인사(HRA100)_비공개")


def _config(archiver: dict, private_channels: list[str]) -> dict:
    raw = json.loads((HERMES / "config.example.json").read_text(encoding="utf-8"))
    cfg = {k: v for k, v in raw.items() if not k.startswith("_")}
    for key in ("digest", "limits", "models", "search", "log"):
        if isinstance(cfg.get(key), dict):
            cfg[key] = {k: v for k, v in cfg[key].items() if not k.startswith("_")}
    for sub in ("health", "healthPre", "ingest", "ingestPre", "daily", "weekly"):
        if isinstance(cfg["digest"].get(sub), dict):
            cfg["digest"][sub] = {
                k: v for k, v in cfg["digest"][sub].items() if not k.startswith("_")
            }
    # 표시 용어 프로필은 **명시**다(2026-10-08) — 기본값이 없으므로 픽스처도 적는다.
    cfg["domain"] = "pf-construction"
    cfg["workspace"] = "태영건설 전산팀"
    cfg["org"] = "태영건설 전산팀"
    cfg["owner"] = {"name": "담당", "label": "", "slackUserId": "U0000000001"}
    cfg["privateChannels"] = list(private_channels)
    cfg["digest"]["channelId"] = "C0000000001"
    cfg["digest"]["deliverTo"] = "dm"
    cfg["digest"]["skipChannels"] = []
    cfg["archiver"] = archiver
    return cfg


@pytest.fixture(scope="module")
def fixture_root(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("canonical") / "archiver"
    done = subprocess.run(
        [sys.executable, str(MAKE_FIXTURE), str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
    )
    if done.returncode != 0:
        pytest.skip(f"fixture 를 못 만들었다: {done.stderr.strip().splitlines()[-3:]}")
    return out


def _manifest(path: Path, rows, *, workspace: str = "tyit") -> Path:
    """**TYBot 쪽 exporter 로** 만든다. 손으로 적으면 두 쪽 키가 갈려도 통과한다."""
    M.write(path, M.build(workspace, [M.Channel(cid, name, private)
                                      for cid, name, private in rows]))
    return path


def _run(fixture_root: Path, tmp_path: Path, *, manifest: Path | str | None,
         private_channels: list[str], in_config: bool = False) -> subprocess.CompletedProcess:
    root = tmp_path / "dataroot"
    root.mkdir(exist_ok=True)
    archiver = {"root": str(fixture_root), "workspace": "tyit"}
    if in_config and manifest is not None:
        archiver["privacyManifest"] = str(manifest)
    (root / "config.json").write_text(
        json.dumps(_config(archiver, private_channels), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    env = dict(os.environ, HERMES_MODE="pf-archiver", HERMES_DATA_ROOT=str(root))
    env.pop("HERMES_PRIVACY_MANIFEST", None)
    env.pop("HERMES_ARCHIVER_ROOT", None)
    env.pop("HERMES_ARCHIVER_WORKSPACE", None)
    args = [NODE, GATE]
    if manifest is not None and not in_config:
        args.append(str(manifest))
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(HERMES), env=env)


ALL_PUBLIC_BUT_HR = [
    (FUNDS[0], FUNDS[1], False),
    (SITE[0], SITE[1], False),
    (PRIVATE[0], PRIVATE[1], True),
]


# --- ① 전수 대조가 맞으면 통과 ------------------------------------------------

@needs_node
@needs_deps
def test_a_complete_manifest_with_matching_declarations_passes(fixture_root, tmp_path):
    """① 세 채널이 전부 manifest 에 있고 비공개 선언이 맞으면 통과한다.

    늘 빨개지는 관문은 사람이 끄는 법부터 배운다 — 통과하는 길이 있어야 한다.
    """
    manifest = _manifest(tmp_path / "m.json", ALL_PUBLIC_BUT_HR)
    got = _run(fixture_root, tmp_path, manifest=manifest, private_channels=[PRIVATE[1]])

    assert got.returncode == 0, got.stdout + got.stderr
    assert "엇갈림 없음" in got.stdout


@needs_node
@needs_deps
def test_the_manifest_path_can_come_from_config(fixture_root, tmp_path):
    """② 경로를 설정에 둘 수 있어야 운영이 명령줄 없이 돌린다."""
    manifest = _manifest(tmp_path / "m.json", ALL_PUBLIC_BUT_HR)
    got = _run(fixture_root, tmp_path, manifest=manifest,
               private_channels=[PRIVATE[1]], in_config=True)
    assert got.returncode == 0, got.stdout + got.stderr


# --- ② 미확인·선언 누락은 거부(1) --------------------------------------------

@needs_node
@needs_deps
def test_a_channel_missing_from_the_manifest_refuses_the_switch(fixture_root, tmp_path):
    """③ 정본에 있는데 manifest 에 없으면 **공개 여부를 모른다.** 막는다.

    exporter 가 `membership_checked_at` 없는 행을 안 보내므로, 「동기화 안 된 채널」
    이 바로 이 모양으로 나타난다.
    """
    manifest = _manifest(tmp_path / "m.json",
                         [r for r in ALL_PUBLIC_BUT_HR if r[0] != SITE[0]])
    got = _run(fixture_root, tmp_path, manifest=manifest, private_channels=[PRIVATE[1]])

    assert got.returncode == 1, got.stdout + got.stderr
    assert "전환 금지" in got.stderr
    assert SITE[0] in got.stderr


@needs_node
@needs_deps
def test_a_private_channel_not_declared_refuses_the_switch(fixture_root, tmp_path):
    """④ manifest 가 비공개라는데 선언에 없으면 Hermes 가 **공개로** 다룬다."""
    manifest = _manifest(tmp_path / "m.json", ALL_PUBLIC_BUT_HR)
    got = _run(fixture_root, tmp_path, manifest=manifest, private_channels=[])

    assert got.returncode == 1, got.stdout + got.stderr
    assert "전환 금지" in got.stderr
    assert PRIVATE[1] in got.stderr
    assert PRIVATE[0] in got.stderr


@needs_node
@needs_deps
def test_a_public_channel_declared_private_is_only_a_warning(fixture_root, tmp_path):
    """⑤ 닫히는 쪽으로 틀린 것 때문에 전환을 막지 않는다.

    막으면 「선언이 과한」 설치가 영영 전환 못 하고, 그 상태에서 사람이 배우는 것은
    이 관문을 끄는 법이다.
    """
    manifest = _manifest(tmp_path / "m.json", ALL_PUBLIC_BUT_HR)
    got = _run(fixture_root, tmp_path, manifest=manifest,
               private_channels=[PRIVATE[1], FUNDS[1]])

    assert got.returncode == 0, got.stdout + got.stderr
    assert FUNDS[1] in got.stdout
    assert "막지 않습니다" in got.stdout


@needs_node
@needs_deps
def test_a_manifest_channel_not_yet_collected_is_only_a_warning(fixture_root, tmp_path):
    """⑥ 수집이 아직 안 닿은 채널은 샐 자료가 없다 — 막을 이유가 없다."""
    manifest = _manifest(tmp_path / "m.json",
                         [*ALL_PUBLIC_BUT_HR, ("C7000SOON0", "팀_신규(NEW001)_준비", True)])
    got = _run(fixture_root, tmp_path, manifest=manifest,
               private_channels=[PRIVATE[1], "팀_신규(NEW001)_준비"])

    assert got.returncode == 0, got.stdout + got.stderr
    assert "C7000SOON0" in got.stdout


# --- ③ 대조 자체를 못 하면 거부(2) -------------------------------------------

@needs_node
@needs_deps
def test_no_manifest_path_configured_refuses(fixture_root, tmp_path):
    """⑦ 경로가 없으면 **통과시키지 않는다.** 「없음」 은 「비공개가 없음」 이 아니다."""
    got = _run(fixture_root, tmp_path, manifest=None, private_channels=[PRIVATE[1]])
    assert got.returncode == 2, got.stdout + got.stderr
    assert "manifest" in got.stderr


@needs_node
@needs_deps
def test_an_unreadable_manifest_refuses(fixture_root, tmp_path):
    """⑧ 파일이 사라진 날 전 채널이 공개로 판정되면 안 된다."""
    missing = _run(fixture_root, tmp_path, manifest=tmp_path / "없다.json",
                   private_channels=[PRIVATE[1]])
    assert missing.returncode == 2, missing.stdout + missing.stderr

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    got = _run(fixture_root, tmp_path, manifest=broken, private_channels=[PRIVATE[1]])
    assert got.returncode == 2, got.stdout + got.stderr


@needs_node
@needs_deps
def test_a_foreign_or_future_manifest_refuses(fixture_root, tmp_path):
    """⑨ 남의 워크스페이스·모르는 형식을 짐작해 읽으면 공개 여부를 잘못 판정한다."""
    foreign = _manifest(tmp_path / "foreign.json", ALL_PUBLIC_BUT_HR, workspace="pfteam")
    got = _run(fixture_root, tmp_path, manifest=foreign, private_channels=[PRIVATE[1]])
    assert got.returncode == 2, got.stdout + got.stderr
    assert "workspace" in got.stderr

    future = tmp_path / "future.json"
    payload = json.loads(_manifest(tmp_path / "m.json", ALL_PUBLIC_BUT_HR)
                         .read_text(encoding="utf-8"))
    payload["schema"] = "channel-privacy-manifest/v2"
    future.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    got = _run(fixture_root, tmp_path, manifest=future, private_channels=[PRIVATE[1]])
    assert got.returncode == 2, got.stdout + got.stderr
    assert "형식" in got.stderr


@needs_node
@needs_deps
def test_a_non_boolean_is_private_refuses(fixture_root, tmp_path):
    """⑩ `"false"` 를 참으로 읽거나 그 반대로 읽는 쪽이 **둘 다 조용히 틀린다.**"""
    path = tmp_path / "m.json"
    payload = json.loads(_manifest(path, ALL_PUBLIC_BUT_HR).read_text(encoding="utf-8"))
    for row in payload["channels"]:
        row["is_private"] = str(row["is_private"]).lower()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    got = _run(fixture_root, tmp_path, manifest=path, private_channels=[PRIVATE[1]])
    assert got.returncode == 2, got.stdout + got.stderr
    assert "is_private" in got.stderr


# --- ④ 두 쪽 계약과 경계 -----------------------------------------------------

@needs_node
def test_both_sides_name_the_same_manifest_schema():
    """⑪ 형식 이름이 갈리면 받는 쪽이 **늘 「형식이 다르다」** 로 막는다.

    Hermes 쪽 이름은 관문이 아니라 **공통 loader** 에 있다 — 관문과 런타임 ACL 이 같은
    검증을 쓰기 때문이다. 관문이 자기 상수를 또 들면 그 자리가 갈린다.
    """
    loader = (HERMES / "src" / "archive-reader" / "privacy-manifest.js").read_text(
        encoding="utf-8")
    assert f"'{M.SCHEMA}'" in loader, M.SCHEMA
    gate = (HERMES / "scripts" / "check-archiver-privacy.js").read_text(encoding="utf-8")
    assert M.SCHEMA not in gate, "관문이 형식 이름을 따로 들고 있다 — loader 하나여야 한다"


@needs_node
def test_hermes_never_connects_to_the_operational_database():
    """⑫ Hermes 쪽에 DB 자격증명을 두면 **운영 DB 로 가는 길이 하나 더** 생긴다.

    그 길은 읽기 전용이라는 보장이 없다. 받는 쪽은 JSON 한 장이어야 한다.
    """
    suspicious = ("pg", "postgres", "DATABASE_URL", "psycopg", "mysql", "knex", "sequelize")
    for path in sorted((HERMES / "src").rglob("*.js")) + sorted((HERMES / "scripts").glob("*.js")):
        if "node_modules" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in suspicious:
            assert f"require('{token}" not in text, f"{path.name}: {token}"
            assert f'from \'{token}' not in text, f"{path.name}: {token}"
        assert "DATABASE_URL" not in text, path.name
    # 의존성 목록에도 없어야 한다 — import 를 안 해도 깔려 있으면 다음 사람이 쓴다.
    deps = json.loads((HERMES / "package.json").read_text(encoding="utf-8"))
    names = {*deps.get("dependencies", {}), *deps.get("devDependencies", {})}
    assert not names & {"pg", "postgres", "mysql2", "knex", "sequelize", "better-sqlite3"}


def test_the_gate_is_registered_in_the_check_catalog():
    """⑬ 등록이 빠지면 그 검사는 **어디서도 안 돌면서 아무것도 안 빨개진다.**"""
    catalog = (HERMES / "scripts" / "check-catalog.js").read_text(encoding="utf-8")
    assert '"scripts/check-archiver-privacy.js"' in catalog
