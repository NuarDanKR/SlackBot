"""`HERMES_MODE=pf-archiver` 의 **Archiver reader 어댑터** — 기능·격리·좌표.

설계: `docs/design/hermes-write-entrypoints.md` §7

## fixture 를 손으로 적지 않는다

정본 경로와 줄 모양은 **계약**이고, 깨져도 에러가 안 난다 — reader 가 0건을 읽고 봇은
「자료가 없습니다」 라고 정상 답변한다. 그 계약을 손으로 적은 fixture 로 고정하면
**적은 사람이 생각한 모양**만 고정되고, 정본이 바뀌어도 시험은 계속 통과한다.

그래서 `scripts/make_archiver_fixture.py` 가 **운영이 쓰는 수집기**(`archiving_bot.
ShadowCollector` + `backfill.run`)를 그대로 돌려 fixture 를 만든다. Slack 클라이언트만
가짜다 — 경로는 `shadow_paths`, 줄 모양은 `archive.writer`, 첨부 정본은
`attachment_writer` 가 만든다.

## 여기서 재는 것

1. **좌표가 TYBot 과 같다** — 같은 파일의 같은 줄에서 locator·message_ts·hash 가
   **바이트 단위로** 같아야 한다. 이게 이 모드의 값이다: 좌표계가 둘이던 동안
   불가능했던 사내↔PF 대조가 성립한다(`summary-approval-ports.md` §3.2)
2. **격리** — 다른 워크스페이스와 DM 이 한 글자도 안 나온다
3. **롤백** — `HERMES_MODE=pf` 는 slack-export 를 그대로 읽는다
4. **기능** — 질문·채널 읽기·첨부 문서·색인·요약 입력이 **기존 공개 API** 로 돈다
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "subbots" / "hermes"
MAKE_FIXTURE = ROOT / "scripts" / "make_archiver_fixture.py"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None or not HERMES.is_dir(), reason="node 또는 subbots/hermes 가 없다"
)
# CLI 는 `dotenv`·`@slack/bolt` 를 import 한다. 없으면 ESM 이 **링크 단계에서** 실패해
# 어떤 최상위 코드도 돌지 못한다 — 환경 문제이지 결함이 아니다.
needs_deps = pytest.mark.skipif(
    not (HERMES / "node_modules").is_dir(),
    reason="subbots/hermes/node_modules 가 없어 실행 시험은 건너뛴다",
)


def _config(archiver: dict | None) -> dict:
    """`config.example.json` 에서 시험용 설정을 만든다.

    예시 파일을 **읽어서** 만든다 — 손으로 적으면 `limits` 같은 필수 키가 빠졌을 때
    「reader 가 고장났다」 와 「설정이 모자라다」 가 같은 오류로 보인다.
    """
    raw = json.loads((HERMES / "config.example.json").read_text(encoding="utf-8"))
    cfg = {k: v for k, v in raw.items() if not k.startswith("_")}
    for key in ("digest", "limits", "models", "search", "log"):
        if isinstance(cfg.get(key), dict):
            cfg[key] = {k: v for k, v in cfg[key].items() if not k.startswith("_")}
    cfg["digest"] = {k: v for k, v in cfg["digest"].items() if not k.startswith("_")}
    for sub in ("health", "healthPre", "ingest", "ingestPre", "daily", "weekly"):
        if isinstance(cfg["digest"].get(sub), dict):
            cfg["digest"][sub] = {
                k: v for k, v in cfg["digest"][sub].items() if not k.startswith("_")
            }
    cfg["workspace"] = "태영건설 전산팀"          # 사람이 읽는 이름 — 경로 키가 아니다
    cfg["org"] = "태영건설 전산팀"
    cfg["owner"] = {"name": "담당", "label": "", "slackUserId": "U0000000001"}
    cfg["privateChannels"] = ["팀_인사(HRA100)_비공개"]
    cfg["digest"]["channelId"] = "C0000000001"
    cfg["digest"]["deliverTo"] = "dm"
    cfg["digest"]["skipChannels"] = []
    if archiver is not None:
        cfg["archiver"] = archiver
    return cfg


@pytest.fixture(scope="module")
def fixture_root(tmp_path_factory) -> Path:
    """**실제 수집기**로 만든 Archiver 정본."""
    out = tmp_path_factory.mktemp("archiver") / "canonical"
    done = subprocess.run(
        [sys.executable, str(MAKE_FIXTURE), str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
    )
    if done.returncode != 0:
        pytest.skip(f"fixture 를 못 만들었다: {done.stderr.strip().splitlines()[-3:]}")
    return out


@pytest.fixture(scope="module")
def archiver_dataroot(fixture_root, tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("dataroot-archiver")
    (root / "config.json").write_text(
        json.dumps(_config({"root": str(fixture_root), "workspace": "tyit"}),
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return root


def _node(args: list[str], *, dataroot: Path, mode: str) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ, HERMES_MODE=mode, HERMES_DATA_ROOT=str(dataroot))
    env.pop("HERMES_ARCHIVER_ROOT", None)
    env.pop("HERMES_ARCHIVER_WORKSPACE", None)
    # `errors="replace"` — node 가 윈도우 콘솔 코드페이지로 쓰는 줄이 섞이면
    # strict 디코딩이 **읽기 스레드에서** 터지고, 그 예외는 시험 실패가 아니라 경고로만
    # 보인다. 못 읽은 글자 때문에 「시험이 조용히 덜 돌았다」 가 되면 안 된다.
    return subprocess.run(
        [NODE, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(HERMES), env=env,
    )


# --- ① 기능·격리·좌표를 봇이 쓰는 함수로 잰다 --------------------------------

@needs_node
@needs_deps
def test_the_reader_check_passes(fixture_root, archiver_dataroot):
    """① 질문·채널 읽기·첨부 문서·색인·격리·좌표 — 전부 **기존 공개 API** 로.

    세부 항목은 `scripts/check-archiver-reader.js` 가 들고 있다. 여기서 두 벌로 적지
    않는다 — 적으면 한쪽만 고쳐지고, 그때 어느 쪽이 맞는지 알 수 없다.
    """
    done = _node(["scripts/check-archiver-reader.js", str(fixture_root)],
                 dataroot=archiver_dataroot, mode="pf-archiver")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "전부 통과" in done.stdout


# --- ② 좌표가 TYBot 과 같은가 (이 모드의 값) ---------------------------------

@needs_node
@needs_deps
def test_the_coordinates_match_tybot_byte_for_byte(fixture_root, archiver_dataroot):
    """② **같은 파일의 같은 줄**에서 locator·message_ts·hash 가 같아야 한다.

    같지 않으면 두 인터페이스가 같은 원문을 보고도 다른 좌표를 내고, 대조는
    `no_match` 또는 `evidence_changed` 로 끝난다 — 「원문이 바뀌었다」 로 보이는데
    아무것도 바뀌지 않았다. 그 상태는 「상대편이 아직 아무것도 안 했다」 와 화면에서
    구별되지 않는다.
    """
    from tybot.archive.store import ArchiveStore
    from tybot.evidence_refs import content_hash

    probe = (
        "const c = await import('./src/config.js');"
        "const out = {};"
        "for (const id of c.archiveSource.channelIds())"
        " out[id] = c.archiveSource.coordinates(id).map(x =>"
        " ({ locator: x.locator, ts: x.messageTs, hash: x.evidenceHash }));"
        "process.stdout.write(JSON.stringify(out));"
    )
    done = _node(["--input-type=module", "-e", probe],
                 dataroot=archiver_dataroot, mode="pf-archiver")
    assert done.returncode == 0, done.stderr
    hermes = json.loads(done.stdout)

    manifest = json.loads((fixture_root / "fixture.json").read_text(encoding="utf-8"))
    declared = {c["channel_id"] for c in manifest["channels"]}

    store = ArchiveStore(fixture_root)
    tybot: dict[str, dict[str, dict]] = {}
    for doc in store.source_docs():
        # **정본이 선언한 채널만 본다.** TYBot 의 store 는 뿌리 아래를 전부 읽고 격리는
        # 그 위의 ACL 층(`can_access`)이 한다 — 다른 워크스페이스도, 채널 디렉터리에
        # 잘못 놓인 DM 문서도 거기서는 「문서」 로 보인다. Hermes reader 는 **경로와
        # 내용 둘로** 그것을 거르므로, 여기서 범위를 안 맞추면 「Hermes 가 덜 읽는다」
        # 로 보인다 — 실제로는 읽지 않아야 하는 것을 안 읽은 것이다(요구사항 3).
        if doc.workspace != "tyit" or str(doc.channel_id or "") not in declared:
            continue
        for line in doc.raw_lines:
            source = line.source_path or doc.path
            rel = source.relative_to(fixture_root).as_posix()
            tybot.setdefault(str(doc.channel_id), {})[f"{rel}:{line.lineno}"] = {
                "ts": str(getattr(line, "message_ts", "") or ""),
                "hash": content_hash(line.ts, line.speaker, line.text),
            }

    assert hermes, "Hermes 가 좌표를 하나도 못 냈다"
    assert set(hermes) == set(tybot), (sorted(hermes), sorted(tybot))
    for channel_id, rows in hermes.items():
        for row in rows:
            mine = tybot[channel_id].get(row["locator"])
            assert mine is not None, f"TYBot 에 없는 좌표: {row['locator']}"
            assert row["ts"] == mine["ts"], row["locator"]
            assert row["hash"] == mine["hash"], row["locator"]


@needs_node
@needs_deps
def test_misplaced_canonical_files_are_refused_by_content(fixture_root, archiver_dataroot):
    """④ **경로 판정이 틀린 날**에도 막힌다 — 내용으로 한 번 더 거른다.

    reader 는 `<id>__<이름>` 디렉터리만 읽으므로 DM(`dm/<user>`)과 다른 워크스페이스는
    경로만으로 빠진다. 그 한 겹에만 기대면 **판정 하나가 틀리는 날** 개인 기록이 채널
    근거로 섞이고, 그건 오류를 내지 않는다.

    fixture 는 실제 writer 가 만든 DM·타 워크스페이스 raw 파일을 **채널 디렉터리에
    옮겨 둔다.** 프론트매터의 `dm_user`·`workspace` 가 그것을 거른다.
    """
    manifest = json.loads((fixture_root / "fixture.json").read_text(encoding="utf-8"))
    assert manifest["misplaced_in_channel"] == ["dm", "other-workspace"], (
        "fixture 가 잘못 놓인 정본을 안 만들었다 — 2차 방어를 재는 시험이 비어 있다"
    )
    probe = (
        "const c = await import('./src/config.js');"
        "const a = await import('./src/archive.js');"
        "process.stdout.write(JSON.stringify({"
        " text: a.listArchivedChannels().map(n =>"
        "   c.readCached(c.archiveSource.channelKeyOf(n))).join('\\n'),"
        " channels: a.listArchivedChannels() }));"
    )
    done = _node(["--input-type=module", "-e", probe],
                 dataroot=archiver_dataroot, mode="pf-archiver")
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    for leaked in ("개인 DM 에만", "PF 내부 논의"):
        assert leaked not in got["text"], f"잘못 놓인 정본이 채널 근거로 섞였다: {leaked}"
    # 그 파일들이 실제로 그 자리에 있는지도 확인한다 — 없으면 위 단정이 공허하다.
    raw = next((fixture_root / "tyit").glob("C1000FUNDS__*/archive/raw"))
    assert (raw / "2026-01-02.md").is_file() and (raw / "2026-01-03.md").is_file()

    # **세 겹이 서로 중복으로 막는다.** 실측: 셋을 전부 끄면 샌다. 하나만 끄면 남은
    # 둘이 잡는다 — 그게 겹의 뜻이다. 그래서 「하나를 지우면 빨개지는」 시험은 만들 수
    # 없고(실제 DM 파일은 세 조건을 동시에 어긴다), 대신 세 판정이 **소스에 남아 있는지**
    # 를 본다. 겹 하나가 조용히 사라지는 쪽이 이 자리의 실패 방식이다.
    source = (HERMES / "src" / "archive-reader" / "archiver.js").read_text(encoding="utf-8")
    for guard in (
        "meta.workspace !== key",
        "meta.dm_user",
        "meta.channel_id !== channelId",
    ):
        assert guard in source, f"내용 기반 방어가 사라졌다: {guard}"


@needs_node
@needs_deps
def test_a_changed_source_line_changes_the_hash(fixture_root, archiver_dataroot, tmp_path):
    """③ 원문이 바뀌면 해시가 바뀌어야 한다 — 안 바뀌면 생략이 **낡은 승인**으로 일어난다."""
    copy = tmp_path / "canonical"
    shutil.copytree(fixture_root, copy)
    target = next((copy / "tyit").glob("C1000FUNDS__*/archive/raw/2026-10-02.md"))
    target.write_text(
        target.read_text(encoding="utf-8").replace("12억원", "11억원"), encoding="utf-8")

    root = tmp_path / "dataroot"
    root.mkdir()
    (root / "config.json").write_text(
        json.dumps(_config({"root": str(copy), "workspace": "tyit"}),
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    probe = (
        "const c = await import('./src/config.js');"
        "process.stdout.write(JSON.stringify("
        "c.archiveSource.coordinates('C1000FUNDS').map(x => x.evidenceHash)));"
    )
    after = json.loads(_node(["--input-type=module", "-e", probe],
                             dataroot=root, mode="pf-archiver").stdout)
    before = json.loads(_node(["--input-type=module", "-e", probe],
                              dataroot=archiver_dataroot, mode="pf-archiver").stdout)
    assert len(before) == len(after)
    assert before != after, "원문을 고쳤는데 해시가 그대로다"


# --- ③ 롤백 — `pf` 는 지금까지와 같다 ----------------------------------------

@needs_node
@needs_deps
def test_pf_mode_still_reads_slack_export(tmp_path):
    """④ `pf` 는 **slack-export 를 그대로** 읽는다. 롤백 경로다.

    여기가 깨지면 Archiver 전환을 되돌릴 수가 없다. 되돌릴 수 없는 전환은 전환이
    아니라 단방향 이관이고, 그건 오너가 승인한 범위가 아니다.
    """
    data = tmp_path / "dataroot"
    channels = data / "slack-export" / "channels"
    channels.mkdir(parents=True)
    (data / "slack-export" / "index.md").write_text("# 색인\n", encoding="utf-8")
    (channels / "자금.md").write_text(
        "# #자금\n\n> 메타\n\n## 2026-10\n\n**2026-10-02 09:00 · 김과장**\n"
        "슬랙익스포트 전용 문장입니다.\n\n---\n",
        encoding="utf-8",
    )
    (data / "config.json").write_text(
        json.dumps(_config(None), ensure_ascii=False, indent=2), encoding="utf-8")

    probe = (
        "const c = await import('./src/config.js');"
        "const a = await import('./src/archive.js');"
        "a.assertArchive();"
        "process.stdout.write(JSON.stringify({ source: c.ARCHIVE_SOURCE,"
        " hasReader: Boolean(c.archiveSource), channels: a.listArchivedChannels(),"
        " hit: (a.searchArchive({ query: '슬랙익스포트', access: c.FULL_ACCESS }).hits||[]).length }));"
    )
    done = _node(["--input-type=module", "-e", probe], dataroot=data, mode="pf")
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got["source"] == "slack-export"
    assert got["hasReader"] is False
    assert got["channels"] == ["자금"]
    assert got["hit"] == 1


@needs_node
@needs_deps
def test_pf_archiver_without_settings_refuses_to_start(tmp_path):
    """⑤ 설정이 없으면 **던진다.** 조용히 slack-export 로 물러서지 않는다.

    물러서면 Hermes writer 를 끈 상태의 **낡은 자료**를 최신으로 읽는다. 날짜가
    멈춘 것은 사람이 한참 뒤에야 안다.
    """
    data = tmp_path / "dataroot"
    data.mkdir()
    (data / "config.json").write_text(
        json.dumps(_config(None), ensure_ascii=False, indent=2), encoding="utf-8")

    # **기동 관문은 `assertArchive()` 다** — `src/index.js` 가 뜨면서 부른다.
    # 판정을 `config.js` import 에 두면 아카이브를 안 읽는 자리(쓰기 차단 확인·doctor)
    # 까지 전부 죽고, 「쓰기가 막혔다」 는 정확한 사유가 「루트가 없다」 로 덮인다.
    done = _node(["--input-type=module", "-e",
                  "const a = await import('./src/archive.js'); a.assertArchive();"],
                 dataroot=data, mode="pf-archiver")
    assert done.returncode != 0
    assert "Archiver 루트" in done.stderr or "archiver.root" in done.stderr

    # 그래도 **import 자체는 선다** — 그 자리의 사유가 가려지면 안 된다.
    ok = _node(["--input-type=module", "-e",
                "const m = await import('./src/mode.js');"
                "await import('./src/config.js');"
                "process.stdout.write(String(m.archiveWritesBlocked()));"],
               dataroot=data, mode="pf-archiver")
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == "true"


@needs_node
@needs_deps
def test_a_human_readable_workspace_name_is_refused_as_a_path_key(fixture_root, tmp_path):
    """⑥ 사람이 읽는 이름을 경로 키로 받지 않는다.

    받아 주면 디렉터리를 못 찾아 **빈 아카이브**가 되는데, 빈 아카이브는 오류가 아니라
    「자료가 없습니다」 로 나가서 아무도 못 알아챈다.
    """
    data = tmp_path / "dataroot"
    data.mkdir()
    (data / "config.json").write_text(
        json.dumps(_config({"root": str(fixture_root), "workspace": "태영건설 전산팀"}),
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    done = _node(["--input-type=module", "-e",
                  "const a = await import('./src/archive.js'); a.assertArchive();"],
                 dataroot=data, mode="pf-archiver")
    assert done.returncode != 0
    assert "workspace" in done.stderr
    # **빈 아카이브로 보이면 안 된다.** 「자료가 없습니다」 는 오류가 아니라 정상 답변이라
    # 아무도 못 알아챈다 — 그래서 사유가 workspace 키여야 한다.
    assert "자료가 없" not in done.stderr


# --- ④ archive-inbox 결정 좌표가 정본을 가리킨다 (요구사항 10) ----------------

@needs_node
@needs_deps
def test_archive_inbox_records_the_canonical_coordinate(fixture_root, archiver_dataroot):
    """⑧ 스킬의 결정 좌표가 **정본 상대경로 + message_ts** 다.

    `pf-archiver` 에서 채널 본문은 투영된 것이라 파이썬이 파일을 열 수 없다. 그래서
    투영과 좌표표를 `scripts/archiver-channel.js` 에서 받고, **인용을 찾는 것은**
    `review_work.evidence_block()` 하나로 한다 — 투영을 파이썬에서 다시 파싱하면
    두 벌이 되고, 갈리면 화면이 보여 준 원문과 좌표가 다른 메시지를 가리킨다.

    그리고 그 좌표는 TYBot 이 같은 줄에 매기는 것과 **같아야** 한다. 같지 않으면
    대조가 `no_match` 로 끝나고, 그 상태는 「상대편이 아직 아무것도 안 했다」 와
    화면에서 구별되지 않는다.
    """
    import os

    scripts = HERMES / ".claude" / "skills" / "archive-inbox" / "scripts"
    probe = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(scripts)!r})\n"
        "import decision_export as X\n"
        "item = {'id': 'x', 'kind': 'summary', 'channel': '팀_자금(ABB540)_주간보고',\n"
        "        'file': '팀_자금(ABB540)_주간보고',\n"
        '        \'evidence\': \'"금주 집행액은 12억원으로 확정되었습니다"\'}\n'
        "print(json.dumps({'archiver': X.archiver_mode(),\n"
        " 'coord': X.archiver_coordinate(item),\n"
        " 'workspace': X.workspace_label(),\n"
        " 'ids': X.channel_ids({})}, ensure_ascii=False))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(HERMES),
        env=dict(os.environ, HERMES_MODE="pf-archiver",
                 HERMES_DATA_ROOT=str(archiver_dataroot)),
    )
    assert done.returncode == 0, done.stdout + done.stderr
    got = json.loads(done.stdout.strip().splitlines()[-1])

    assert got["archiver"] is True
    locator, digest, workspace, channel_id, message_ts, why = got["coord"]
    assert locator, f"좌표를 못 냈다: {why}"
    assert locator.endswith(":15"), locator        # 그 채널 2026-10-02 의 첫 메시지 줄
    assert "/archive/raw/" in locator
    assert workspace == "tyit"                     # 사람이 읽는 이름이 아니다
    assert channel_id == "C1000FUNDS"
    assert message_ts == "1790899200.000100"
    assert len(digest) == 64

    # **TYBot 이 같은 줄에 매기는 것과 같은가.** 이게 이 모드의 값이다.
    from tybot.archive.store import ArchiveStore
    from tybot.evidence_refs import content_hash

    found = False
    for doc in ArchiveStore(fixture_root).source_docs():
        if str(doc.channel_id or "") != "C1000FUNDS":
            continue
        for line in doc.raw_lines:
            source = line.source_path or doc.path
            rel = source.relative_to(fixture_root).as_posix()
            if f"{rel}:{line.lineno}" != locator:
                continue
            found = True
            assert content_hash(line.ts, line.speaker, line.text) == digest
            assert str(getattr(line, "message_ts", "") or "") == message_ts
    assert found, f"TYBot 쪽에 같은 좌표가 없다: {locator}"

    # 사람이 읽는 workspace 이름이 아니라 **경로 키**를 썼는가.
    assert got["workspace"] == "tyit"
    assert got["ids"].get("팀_자금(ABB540)_주간보고") == "C1000FUNDS"


# --- ⑤ 쓰기는 계속 막힌다 ----------------------------------------------------

@needs_node
def test_archive_writes_stay_blocked_in_pf_archiver():
    """⑦ reader 를 갈아 끼웠다고 writer 가 열리면 안 된다.

    진입점별 회귀는 `tests/test_hermes_tybot_mode.py` 가 들고 있다. 여기서는 **이
    작업이 그 판정을 건드리지 않았는지**만 본다 — reader 작업이 모드 판정을 흔들면
    원문 쓰기가 조용히 열린다.
    """
    probe = (
        "const m = await import('./src/mode.js');"
        "process.stdout.write(JSON.stringify({ blocked: m.archiveWritesBlocked(),"
        " roles: [...m.rolesFor()].sort() }));"
    )
    import os

    done = subprocess.run(
        [NODE, "--input-type=module", "-e", probe], capture_output=True, text=True,
        encoding="utf-8", cwd=str(HERMES), env=dict(os.environ, HERMES_MODE="pf-archiver"),
    )
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got["blocked"] is True
    assert got["roles"] == ["answer", "digest-publish", "health"]
    assert "ingest" not in got["roles"]


# --- ⑤ fixture 가 실제 writer 에서 나왔는가 ----------------------------------

def test_the_fixture_comes_from_the_real_writer():
    """⑧ fixture 생성기는 **운영 수집기**를 부른다.

    경로를 손으로 적으면 적은 사람이 생각한 모양만 고정되고, 정본이 바뀌어도 시험은
    통과한다 — 그때 reader 는 운영에서만 깨진다.
    """
    body = MAKE_FIXTURE.read_text(encoding="utf-8")
    for needed in (
        "archiving_bot.ShadowCollector",
        "backfill.run",
        "backfill_adapter.make_ingest",
        "shadow_paths.archive_dir",
        "shadow_paths.dm_archive_dir",
        "writer.ingest",
    ):
        assert needed in body, f"fixture 가 {needed} 를 안 쓴다 — 손으로 적은 경로다"
    # 경로를 직접 조립하지 않는다. `shadow_paths` 가 규칙의 정본이다.
    assert '"archive" /' not in body
    assert "/ 'archive'" not in body
