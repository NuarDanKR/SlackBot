"""manifest 가 **실행 중 ACL** 이다 — 갈아 끼우면 그 자리에서 반영된다.

설계: `docs/design/hermes-write-entrypoints.md` §7.13

## 무엇이 문제였나

`7515e8a` 의 manifest 는 **전환 관문**에만 쓰였다. 통과한 뒤 봇이 뜨고 나면, 그 사이에
- 공개 채널이 Slack 에서 **비공개로 바뀌어도**
- **새 비공개 채널**이 생겨도

Hermes 는 `config.privateChannels` 만 보므로 그 둘을 **공개로** 다룬다. 사람이
`config.json` 을 고치고 봇을 다시 띄우기 전까지다. 그동안 새는 것은 오류가 아니라
평범한 답변이고, 내용을 아는 사람만 알아챈다.

## 두 방향이 다르다

| 방향 | 반영 |
|---|---|
| manifest 가 **비공개**라고 한다 | **닫는다.** 선언에 없어도 닫는다 |
| manifest 가 **공개**라고 한다 | 안 연다. `config.privateChannels` 가 비공개라면 비공개다 |

여는 쪽을 manifest 에 맡기지 않는 이유는, manifest 가 낡거나 틀렸을 때 **열리는**
방향으로 틀리기 때문이다. 닫는 쪽으로만 합친다(합집합).

## 못 읽거나 오래됐으면 닫는다

「공개로 후퇴」 가 아니다. 비공개 여부를 확정할 수 없으면 **전체 권한이 아닌 접근을
전부 닫는다** — 개명 지도가 죽었을 때와 같은 자리, 같은 모양이다(`isPrivateWith` 의
`map-dead` 분기).
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
MAKE_FIXTURE = ROOT / "scripts" / "make_archiver_fixture.py"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None or not HERMES.is_dir(), reason="node 또는 subbots/hermes 가 없다"
)
needs_deps = pytest.mark.skipif(
    not (HERMES / "node_modules").is_dir(),
    reason="subbots/hermes/node_modules 가 없어 실행 시험은 건너뛴다",
)

FUNDS = ("C1000FUNDS", "팀_자금(ABB540)_주간보고")
SITE = ("C2000SITE0", "현장_김해외동(180182)_채팅방")
PRIVATE = ("C3000PRIV0", "팀_인사(HRA100)_비공개")
ALL_PUBLIC_BUT_HR = [
    (FUNDS[0], FUNDS[1], False),
    (SITE[0], SITE[1], False),
    (PRIVATE[0], PRIVATE[1], True),
]


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


def _manifest(path: Path, rows, *, workspace="tyit", generated_at=None) -> Path:
    """**TYBot 쪽 exporter 로** 만든다. 손으로 적으면 두 쪽 키가 갈려도 통과한다."""
    payload = M.build(workspace, [M.Channel(cid, name, private)
                                  for cid, name, private in rows])
    if generated_at is not None:
        payload["generated_at"] = generated_at
    M.write(path, payload)
    return path


def _dataroot(tmp_path: Path, fixture_root: Path, manifest: Path | str,
              private_channels: list[str], *, max_age_hours=None) -> Path:
    root = tmp_path / "dataroot"
    root.mkdir(exist_ok=True)
    archiver = {"root": str(fixture_root), "workspace": "tyit",
                "privacyManifest": str(manifest)}
    if max_age_hours is not None:
        archiver["privacyManifestMaxAgeHours"] = max_age_hours
    (root / "config.json").write_text(
        json.dumps(_config(archiver, private_channels), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return root


def _node(args: list[str], *, dataroot: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, HERMES_MODE="pf-archiver", HERMES_DATA_ROOT=str(dataroot))
    for key in ("HERMES_PRIVACY_MANIFEST", "HERMES_ARCHIVER_ROOT",
                "HERMES_ARCHIVER_WORKSPACE"):
        env.pop(key, None)
    return subprocess.run([NODE, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(HERMES), env=env)


def _gate(dataroot: Path) -> subprocess.CompletedProcess:
    return _node(["scripts/check-archiver-privacy.js"], dataroot=dataroot)


# --- ① 관문: generated_at 을 **검증한다** ------------------------------------

@needs_node
@needs_deps
@pytest.mark.parametrize("stamp,why", [
    (None, "없음"),
    ("", "빈 값"),
    ("어제쯤", "파싱 실패"),
    ("2026-13-45T99:99:99+00:00", "달력에 없는 값"),
])
def test_a_bad_generated_at_refuses_hard(fixture_root, tmp_path, stamp, why):
    """① `generated_at` 이 없거나 못 읽으면 **얼마나 낡았는지 알 수 없다.** rc 2.

    낡은 정도를 모르는 manifest 는 「방금 뽑았다」 와 「석 달 전 것」 이 구별되지 않는다.
    """
    path = tmp_path / "m.json"
    _manifest(path, ALL_PUBLIC_BUT_HR)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if stamp is None:
        payload.pop("generated_at", None)
    else:
        payload["generated_at"] = stamp
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    got = _gate(_dataroot(tmp_path, fixture_root, path, [PRIVATE[1]]))
    assert got.returncode == 2, f"{why}: {got.stdout}{got.stderr}"
    assert "generated_at" in got.stderr


@needs_node
@needs_deps
def test_a_stale_manifest_refuses_hard(fixture_root, tmp_path):
    """② 허용 기간을 넘기면 rc 2.

    그 사이에 공개 채널이 비공개로 바뀌었을 수 있다. 오래된 manifest 를 통과시키면
    **그 변경을 모른 채** 전환한다.
    """
    path = _manifest(tmp_path / "m.json", ALL_PUBLIC_BUT_HR,
                     generated_at="2026-10-01T00:00:00+00:00")
    got = _gate(_dataroot(tmp_path, fixture_root, path, [PRIVATE[1]], max_age_hours=1))
    assert got.returncode == 2, got.stdout + got.stderr
    assert "오래" in got.stderr or "기간" in got.stderr


@needs_node
@needs_deps
def test_a_future_manifest_refuses_hard(fixture_root, tmp_path):
    """③ 미래 시각은 rc 2.

    시계가 어긋났거나 손으로 적은 값이다. 어느 쪽이든 「얼마나 낡았나」 를 못 재고,
    못 재는 값으로 허용 기간을 판정하면 **영원히 신선한** manifest 가 생긴다.
    """
    path = _manifest(tmp_path / "m.json", ALL_PUBLIC_BUT_HR,
                     generated_at="2099-01-01T00:00:00+00:00")
    got = _gate(_dataroot(tmp_path, fixture_root, path, [PRIVATE[1]]))
    assert got.returncode == 2, got.stdout + got.stderr
    assert "미래" in got.stderr


# --- ② 관문: 빈 ID·중복 ID ----------------------------------------------------

@needs_node
@needs_deps
def test_an_empty_channel_id_refuses_hard(fixture_root, tmp_path):
    """④ 키가 없는 행은 **대조에서 조용히 빠진다.** 빠진 줄을 모르면 전수 대조가 아니다."""
    path = tmp_path / "m.json"
    _manifest(path, ALL_PUBLIC_BUT_HR)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["channels"].append({"channel_id": "", "channel_name": "이름만", "is_private": True})
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    got = _gate(_dataroot(tmp_path, fixture_root, path, [PRIVATE[1]]))
    assert got.returncode == 2, got.stdout + got.stderr
    assert "channel_id" in got.stderr


@needs_node
@needs_deps
def test_duplicate_channel_ids_refuse_hard_and_are_all_named(fixture_root, tmp_path):
    """⑤ 중복 ID 는 **뒤엣것이 앞엣것을 조용히 덮는다.**

    `is_private: true` 뒤에 `false` 가 오면 비공개 채널이 공개로 판정된다. 어느 쪽을
    고르든 틀릴 수 있으므로 **고르지 않고 거부한다.** 중복은 **전부** 적는다 — 하나만
    적으면 사람이 그것만 고치고 다시 돌린다.
    """
    path = tmp_path / "m.json"
    _manifest(path, ALL_PUBLIC_BUT_HR)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["channels"].append(
        {"channel_id": PRIVATE[0], "channel_name": PRIVATE[1], "is_private": False})
    payload["channels"].append(
        {"channel_id": FUNDS[0], "channel_name": FUNDS[1], "is_private": True})
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    got = _gate(_dataroot(tmp_path, fixture_root, path, [PRIVATE[1]]))
    assert got.returncode == 2, got.stdout + got.stderr
    assert PRIVATE[0] in got.stderr
    assert FUNDS[0] in got.stderr, "중복을 하나만 적었다"


# --- ③ 실행 중 ACL — manifest 교체가 **그 자리에서** 반영된다 -----------------

#: 한 프로세스 안에서 manifest 를 갈아 끼우고 권한을 다시 묻는다.
#: 프로세스를 다시 띄우면 캐시가 어차피 비어 있어 **아무것도 안 지킨다.**
_ACL_PROBE = """
import fs from 'node:fs';
const c = await import('./src/config.js');
const access = c.accessFor([]);          // 공개 채널에서 물은 사람 — 비공개는 못 본다
const look = () => ({
  funds: { private: c.isPrivateChannel(%(funds)s), canSee: c.canSee(access, %(funds)s) },
  site: { private: c.isPrivateChannel(%(site)s), canSee: c.canSee(access, %(site)s) },
  hr: { private: c.isPrivateChannel(%(hr)s), canSee: c.canSee(access, %(hr)s) },
});
const out = { before: look() };
fs.writeFileSync(process.env.SWAP_TO_PATH, fs.readFileSync(process.env.SWAP_FROM_PATH));
out.after = look();
process.stdout.write(JSON.stringify(out));
"""


def _acl_probe() -> str:
    return _ACL_PROBE % {
        "funds": json.dumps(FUNDS[1], ensure_ascii=False),
        "site": json.dumps(SITE[1], ensure_ascii=False),
        "hr": json.dumps(PRIVATE[1], ensure_ascii=False),
    }


def _swap(dataroot: Path, live: Path, replacement: Path) -> dict:
    env_extra = {"SWAP_TO_PATH": str(live), "SWAP_FROM_PATH": str(replacement)}
    env = dict(os.environ, HERMES_MODE="pf-archiver", HERMES_DATA_ROOT=str(dataroot),
               **env_extra)
    for key in ("HERMES_PRIVACY_MANIFEST", "HERMES_ARCHIVER_ROOT",
                "HERMES_ARCHIVER_WORKSPACE"):
        env.pop(key, None)
    done = subprocess.run([NODE, "--input-type=module", "-e", _acl_probe()],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(HERMES), env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


@needs_node
@needs_deps
def test_public_to_private_takes_effect_without_a_restart(fixture_root, tmp_path):
    """⑥ 공개였던 채널이 manifest 에서 **비공개가 되면 그 자리에서 닫힌다.**

    Slack 에서 채널을 비공개로 바꾸면 다음 동기화가 그 사실을 DB 에 적고, manifest 가
    갈린다. 그때까지 `config.privateChannels` 만 보면 봇은 **그 채널을 계속 공개로**
    다루고, 사람이 설정을 고쳐 봇을 다시 띄우기 전까지 샌다.
    """
    live = _manifest(tmp_path / "live.json", ALL_PUBLIC_BUT_HR)
    later = _manifest(tmp_path / "later.json", [
        (FUNDS[0], FUNDS[1], True),          # 공개 → 비공개
        (SITE[0], SITE[1], False),
        (PRIVATE[0], PRIVATE[1], True),
    ])
    root = _dataroot(tmp_path, fixture_root, live, [PRIVATE[1]])

    got = _swap(root, live, later)

    assert got["before"]["funds"] == {"private": False, "canSee": True}
    assert got["after"]["funds"] == {"private": True, "canSee": False}
    # 나머지는 그대로다 — 바뀐 한 채널만 바뀐다.
    assert got["after"]["site"] == {"private": False, "canSee": True}
    assert got["after"]["hr"]["private"] is True


@needs_node
@needs_deps
def test_a_new_private_channel_closes_without_a_restart(fixture_root, tmp_path):
    """⑦ manifest 에 **새로 생긴 비공개 채널**도 `privateChannels` 없이 닫힌다."""
    live = _manifest(tmp_path / "live.json",
                     [r for r in ALL_PUBLIC_BUT_HR if r[0] != SITE[0]])
    later = _manifest(tmp_path / "later.json", [
        (FUNDS[0], FUNDS[1], False),
        (SITE[0], SITE[1], True),            # 새로 들어온 비공개 채널
        (PRIVATE[0], PRIVATE[1], True),
    ])
    root = _dataroot(tmp_path, fixture_root, live, [PRIVATE[1]])

    got = _swap(root, live, later)

    # 처음에는 manifest 에 없어 **확정할 수 없다** — 그때도 닫혀 있어야 한다(원칙 3).
    assert got["before"]["site"]["canSee"] is False
    assert got["after"]["site"] == {"private": True, "canSee": False}


@needs_node
@needs_deps
def test_declared_private_stays_private_even_if_the_manifest_says_public(
    fixture_root, tmp_path,
):
    """⑧ manifest 가 공개라고 해도 **선언이 비공개면 비공개다.**

    여는 쪽을 manifest 에 맡기면, manifest 가 낡거나 틀렸을 때 **열리는** 방향으로
    틀린다. 닫는 쪽으로만 합친다.
    """
    live = _manifest(tmp_path / "live.json", ALL_PUBLIC_BUT_HR)
    root = _dataroot(tmp_path, fixture_root, live, [PRIVATE[1], FUNDS[1]])
    got = _swap(root, live, live)

    assert got["before"]["funds"]["private"] is True
    assert got["before"]["funds"]["canSee"] is False


# --- ④ 못 읽거나 오래되면 **닫는다** -----------------------------------------

@needs_node
@needs_deps
@pytest.mark.parametrize("how", ["unreadable", "stale", "deleted"])
def test_an_unusable_manifest_closes_access_instead_of_opening_it(
    fixture_root, tmp_path, how,
):
    """⑨ 못 읽거나 오래됐으면 **공개로 후퇴하지 않는다.** 전체 권한이 아닌 접근을 닫는다.

    개명 지도가 죽었을 때와 같은 자리, 같은 모양이다 — 비공개 여부를 확정할 수 없으면
    전부 비공개로 답한다.
    """
    live = _manifest(tmp_path / "live.json", ALL_PUBLIC_BUT_HR)
    broken = tmp_path / "broken.json"
    if how == "unreadable":
        broken.write_text("{not json", encoding="utf-8")
    elif how == "stale":
        _manifest(broken, ALL_PUBLIC_BUT_HR, generated_at="2026-09-01T00:00:00+00:00")
    else:
        broken.write_text("", encoding="utf-8")
    root = _dataroot(tmp_path, fixture_root, live, [PRIVATE[1]], max_age_hours=1)

    got = _swap(root, live, broken)

    # 바뀌기 전에는 공개 채널이 보였다 — 비교가 공허하지 않다.
    assert got["before"]["funds"] == {"private": False, "canSee": True}
    # 바뀐 뒤에는 **전부 닫힌다.**
    for key in ("funds", "site", "hr"):
        assert got["after"][key] == {"private": True, "canSee": False}, key


@needs_node
@needs_deps
def test_full_access_still_works_when_the_manifest_is_unusable(fixture_root, tmp_path):
    """⑩ 닫는 것은 **전체 권한이 아닌 접근**이다. 전체 권한은 산다 — 점검·복구가 막히면 안 된다."""
    live = _manifest(tmp_path / "live.json", ALL_PUBLIC_BUT_HR)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    root = _dataroot(tmp_path, fixture_root, live, [PRIVATE[1]])

    probe = (
        "import fs from 'node:fs';"
        "const c = await import('./src/config.js');"
        "fs.writeFileSync(process.env.SWAP_TO_PATH, fs.readFileSync(process.env.SWAP_FROM_PATH));"
        f"process.stdout.write(JSON.stringify(c.canSee(c.FULL_ACCESS, {json.dumps(FUNDS[1], ensure_ascii=False)})));"
    )
    env = dict(os.environ, HERMES_MODE="pf-archiver", HERMES_DATA_ROOT=str(root),
               SWAP_TO_PATH=str(live), SWAP_FROM_PATH=str(broken))
    for key in ("HERMES_PRIVACY_MANIFEST", "HERMES_ARCHIVER_ROOT",
                "HERMES_ARCHIVER_WORKSPACE"):
        env.pop(key, None)
    done = subprocess.run([NODE, "--input-type=module", "-e", probe],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(HERMES), env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "true"


# --- ⑤ 두 쪽이 같은 판정을 쓴다 ----------------------------------------------

@needs_node
def test_the_gate_and_the_runtime_share_one_loader():
    """⑪ 관문과 런타임이 **같은 검증**을 써야 한다.

    두 벌이면 관문은 통과시키고 런타임은 닫는(또는 그 반대) 조합이 생기고, 그 상태는
    「전환했는데 봇이 아무것도 못 본다」 로만 드러난다.
    """
    loader = HERMES / "src" / "archive-reader" / "privacy-manifest.js"
    assert loader.is_file(), "공통 loader 가 없다"
    gate = (HERMES / "scripts" / "check-archiver-privacy.js").read_text(encoding="utf-8")
    cfg = (HERMES / "src" / "config.js").read_text(encoding="utf-8")
    for body in (gate, cfg):
        assert "privacy-manifest.js" in body
    # 관문이 자기 검증을 또 짜지 않는다.
    assert "generated_at" not in gate or "loadPrivacyManifest" in gate
