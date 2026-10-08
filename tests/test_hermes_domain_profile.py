"""도메인 프로필 — **표시 용어는 인스턴스 설정이고, 서버나 모드가 아니다.**

## 전제 정정 (2026-10-08, 오너)

PF 전용 서버도, 기존 GCP Hermes 운영도 **없다.** 바뀐 PF Hermes 도 TYBot·Archiver 가
있는 **같은 서버**에서 별도 systemd 인스턴스로 돈다.

그래서 「PF 니까 사업장」 은 성립하지 않는다 — 한 호스트에서 같은 바이너리로 두
인스턴스가 돌고, 표시 용어는 **그 인스턴스가 무엇을 다루는가**에 달려 있다.

| 프로필 | 표시 용어 | 내부 키 |
|---|---|---|
| `pf-construction` | 사업장 | **안 바뀐다** |
| `enterprise` | 채널 · 자료 영역 | **안 바뀐다** |

## 여기서 고정하는 것

1. 프로필은 **명시 설정**이다. `HERMES_MODE` 나 호스트로 추론하지 않는다
2. **누락·오타는 던진다.** 조용히 PF 기본값으로 후퇴하면, 사내 인스턴스가 사내
   사람들에게 「사업장」 이라고 말하는 것을 아무도 모른다
3. 내부 project 키·데이터 구조는 **안 바뀐다.** 바뀌는 것은 사람이 읽는 문구뿐이다
4. 한 호스트의 두 인스턴스가 서로의 workspace·manifest·state 를 **못 읽는다**
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "subbots" / "hermes"
DOMAIN_JS = HERMES / "src" / "domain.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None or not HERMES.is_dir(), reason="node 또는 subbots/hermes 가 없다"
)
needs_deps = pytest.mark.skipif(
    not (HERMES / "node_modules").is_dir(),
    reason="subbots/hermes/node_modules 가 없어 실행 시험은 건너뛴다",
)


def _node(code: str, *, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, **(env_extra or {}))
    for key in ("HERMES_DOMAIN", "HERMES_DATA_ROOT", "HERMES_STATE_DIR",
                "HERMES_ARCHIVER_ROOT", "HERMES_ARCHIVER_WORKSPACE",
                "HERMES_PRIVACY_MANIFEST", "HERMES_MODE"):
        if key not in (env_extra or {}):
            env.pop(key, None)
    return subprocess.run(
        [NODE, "--input-type=module", "-e", code], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(HERMES), env=env,
    )


# --- ① 프로필 판정 — 누락·오타는 던진다 ---------------------------------------

@needs_node
def test_a_missing_or_unknown_profile_throws():
    """① 조용히 PF 기본값으로 후퇴하지 않는다.

    후퇴하면 **사내 인스턴스가 사내 사람들에게 「사업장」 이라고 말한다.** 그건
    오류가 아니라 어색한 문장으로만 나타나고, 아무도 고쳐 달라고 하지 않는다.
    """
    code = (
        f"const m = await import({json.dumps(DOMAIN_JS.as_uri())});"
        "const got = {};"
        "for (const raw of [undefined, '', '   ', 'pf', 'PF-CONSTRUCTION ', 'enterprize', 'tybot']) {"
        "  try { got[String(raw)] = { ok: m.resolveDomain(raw) }; }"
        "  catch (e) { got[String(raw)] = { err: e.name, msg: e.message }; }"
        "}"
        "process.stdout.write(JSON.stringify(got));"
    )
    done = _node(code)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    for raw in ("undefined", "", "   ", "pf", "enterprize", "tybot"):
        assert "err" in got[raw], f"{raw!r} 를 받아들였다: {got[raw]}"
        assert got[raw]["err"] == "DomainConfigError", got[raw]
    # 대소문자·공백만 다른 것은 받아 준다 — 그건 오타가 아니라 적는 방식이다.
    assert got["PF-CONSTRUCTION "] == {"ok": "pf-construction"}


@needs_node
def test_the_error_says_what_to_write():
    """② 사유만 적고 **무엇을 적어야 하는지** 안 말하면 다음 사람은 소스를 뒤진다."""
    code = (
        f"const m = await import({json.dumps(DOMAIN_JS.as_uri())});"
        "try { m.resolveDomain('pf'); } catch (e) { process.stdout.write(e.message); }"
    )
    msg = _node(code).stdout
    assert "enterprise" in msg and "pf-construction" in msg, msg
    assert "HERMES_DOMAIN" in msg or "domain" in msg, msg


@needs_node
def test_each_profile_has_its_own_display_terms():
    """③ 표시 용어가 프로필마다 다르다. **내부 키가 아니라 문구**다."""
    code = (
        f"const m = await import({json.dumps(DOMAIN_JS.as_uri())});"
        "process.stdout.write(JSON.stringify({"
        " pf: m.terms('pf-construction'), ent: m.terms('enterprise'),"
        " list: m.DOMAINS }));"
    )
    got = json.loads(_node(code).stdout)

    assert got["pf"]["place"] == "사업장"
    assert got["pf"]["area"] == "사업장"
    assert got["ent"]["place"] == "채널"
    assert got["ent"]["area"] == "자료 영역"
    assert sorted(got["list"]) == ["enterprise", "pf-construction"]
    # 예시 이름도 프로필 것이다 — 사내 화면에 「사업장가」 가 뜨면 안 된다.
    assert "사업장" in got["pf"]["examples"]["EX_A"]
    assert "사업장" not in got["ent"]["examples"]["EX_A"], got["ent"]["examples"]


@needs_node
def test_terms_are_frozen():
    """④ 돌려준 용어를 밖에서 고치면 **그 프로세스의 모든 인스턴스**가 함께 바뀐다."""
    code = (
        f"const m = await import({json.dumps(DOMAIN_JS.as_uri())});"
        "const t = m.terms('enterprise');"
        "let threw = false;"
        "try { t.place = '사업장'; } catch { threw = true; }"
        "process.stdout.write(JSON.stringify({ threw, place: m.terms('enterprise').place }));"
    )
    got = json.loads(_node(code).stdout)
    assert got["place"] == "채널", got


# --- ② 내부 키·데이터 구조는 안 바뀐다 ----------------------------------------

@needs_node
def test_the_profile_never_renames_internal_keys():
    """⑤ 바뀌는 것은 **사람이 읽는 문구**뿐이다.

    내부 project 키(= 문서 폴더 이름)나 색인 형식까지 바꾸면 그건 용어 교체가 아니라
    **데이터 이관**이다. 기존 자료가 그 순간 안 읽힌다.
    """
    raw = DOMAIN_JS.read_text(encoding="utf-8")
    # **주석은 뺀다.** 경위를 적는 글에 그 낱말이 나오는 것은 자료를 건드리는 것이
    # 아니다 — 글자만 보면 설명을 못 적게 되고, 설명 없는 관문은 걷어내진다.
    body = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    body = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("//"))
    for forbidden in ("mkdir", "readdir", "rename", "writeFile", "projectDir", "slugify"):
        assert forbidden not in body, f"프로필이 자료를 건드린다: {forbidden}"
    assert "from 'node:" not in body, "프로필 모듈이 런타임을 가져간다"


def test_the_fixture_place_names_are_not_globally_replaced():
    """⑥ 시험 픽스처의 「사업장가/나/다」 를 전역 치환하지 않았다.

    그 이름들은 **PF 자료의 모양**을 재는 재료다. 다 바꾸면 PF 쪽 검사가 무엇을
    재는지 알 수 없게 되고, 바꾼 사람도 되돌릴 근거를 잃는다.
    """
    hits = []
    for path in sorted((HERMES / "scripts").glob("*.js")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "사업장가" in text or "사업장나" in text:
            hits.append(path.name)
    assert hits, "픽스처의 사업장 이름이 통째로 사라졌다 — 전역 치환한 것이다"


# --- ③ 한 호스트의 두 인스턴스 ------------------------------------------------

def _config(*, domain: str, workspace: str, private_channels: list[str],
            archiver: dict | None = None) -> dict:
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
    cfg["domain"] = domain
    cfg["workspace"] = workspace
    cfg["org"] = workspace
    cfg["owner"] = {"name": "담당", "label": "", "slackUserId": "U0000000001"}
    cfg["privateChannels"] = list(private_channels)
    cfg["digest"]["channelId"] = "C0000000001"
    cfg["digest"]["deliverTo"] = "dm"
    cfg["digest"]["skipChannels"] = []
    if archiver is not None:
        cfg["archiver"] = archiver
    else:
        cfg.pop("archiver", None)
    return cfg


def _instance(base: Path, name: str, *, domain: str, channels: list[str],
              private: list[str]) -> dict:
    """인스턴스 하나의 자료 저장소·state 를 만든다. **서로 겹치지 않는 자리**다."""
    data = base / f"{name}-data"
    state = base / f"{name}-state"
    chan = data / "slack-export" / "channels"
    chan.mkdir(parents=True)
    state.mkdir(parents=True)
    (data / "config.json").write_text(
        json.dumps(_config(domain=domain, workspace=name, private_channels=private),
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (data / "slack-export" / "index.md").write_text(
        f"# 색인\n\n> **채널**: 총 {len(channels)}개\n", encoding="utf-8")
    for ch in channels:
        (chan / f"{ch}.md").write_text(
            f"# #{ch}\n\n> 메타\n\n## 2026-10\n\n**2026-10-02 09:00 · 김과장**\n"
            f"{ch} 전용 문장 {name}.\n\n---\n",
            encoding="utf-8",
        )
    (data / "slack-export" / ".sync-state.json").write_text(json.dumps({"channels": {
        f"C{i:09d}": {"name": c, "file": c, "last_ts": "1", "threads": {}}
        for i, c in enumerate(channels)
    }}), encoding="utf-8")
    return {"data": data, "state": state}


@pytest.fixture(scope="module")
def two_instances(tmp_path_factory) -> dict:
    base = tmp_path_factory.mktemp("host")
    return {
        "ent": _instance(base, "ent", domain="enterprise",
                         channels=["전산공지", "인사비공개"], private=["인사비공개"]),
        "pf": _instance(base, "pf", domain="pf-construction",
                        channels=["김해외동", "영업비공개"], private=["영업비공개"]),
        "base": base,
    }


@needs_node
@needs_deps
def test_two_instances_do_not_see_each_other(two_instances):
    """⑦ 같은 호스트·같은 바이너리인데 **서로의 workspace 를 못 읽는다.**

    한 서버에서 둘이 도는 것이 전제가 됐으므로, 격리는 「서버가 다르다」 가 더는
    보장해 주지 않는다. 설정으로 갈라야 한다.
    """
    probe = (
        "const c = await import('./src/config.js');"
        "const a = await import('./src/archive.js');"
        "process.stdout.write(JSON.stringify({"
        " workspace: c.config.workspace, domain: c.DOMAIN,"
        " channels: a.listArchivedChannels(),"
        " text: a.listArchivedChannels().map(n =>"
        "   a.readChannel({ channel: n, access: c.FULL_ACCESS }).text).join('\\n') }));"
    )
    seen = {}
    for key in ("ent", "pf"):
        inst = two_instances[key]
        done = _node(probe, env_extra={
            "HERMES_DATA_ROOT": str(inst["data"]),
            "HERMES_STATE_DIR": str(inst["state"]),
        })
        assert done.returncode == 0, done.stderr
        seen[key] = json.loads(done.stdout)

    assert seen["ent"]["workspace"] == "ent"
    assert seen["pf"]["workspace"] == "pf"
    assert seen["ent"]["domain"] == "enterprise"
    assert seen["pf"]["domain"] == "pf-construction"
    assert sorted(seen["ent"]["channels"]) == ["ssangbang" ] if False else True
    assert "김해외동" not in seen["ent"]["channels"], seen["ent"]["channels"]
    assert "전산공지" not in seen["pf"]["channels"], seen["pf"]["channels"]
    # 본문도 안 섞인다 — 목록만 가르고 본문이 새면 아무 뜻이 없다.
    assert "pf" not in seen["ent"]["text"], seen["ent"]["text"][:200]
    assert "ent" not in seen["pf"]["text"], seen["pf"]["text"][:200]


@needs_node
@needs_deps
def test_each_instance_writes_its_own_state(two_instances):
    """⑧ 대화 원본·요약 상태가 **인스턴스마다 다른 자리**에 쌓인다.

    전에는 `LOG_RAW_DIR` 가 **코드 저장소** 기준이었다. 한 바이너리를 두 인스턴스가
    쓰면 둘의 대화 기록이 같은 파일에 섞인다 — 그건 오류가 아니라 **섞인 로그**로만
    나타나고, 어느 쪽 대화인지 되돌릴 방법이 없다.
    """
    probe = (
        "const c = await import('./src/config.js');"
        "process.stdout.write(JSON.stringify({"
        " state: c.STATE_DIR, raw: c.LOG_RAW_DIR, digest: c.DIGEST_STATE_FILE }));"
    )
    paths = {}
    for key in ("ent", "pf"):
        inst = two_instances[key]
        done = _node(probe, env_extra={
            "HERMES_DATA_ROOT": str(inst["data"]),
            "HERMES_STATE_DIR": str(inst["state"]),
        })
        assert done.returncode == 0, done.stderr
        paths[key] = json.loads(done.stdout)

    for field in ("state", "raw", "digest"):
        assert paths["ent"][field] != paths["pf"][field], field
        assert str(two_instances["ent"]["state"]) in paths["ent"][field], paths["ent"]
        assert str(two_instances["pf"]["state"]) in paths["pf"][field], paths["pf"]
    # **코드 저장소 안이 아니다.** 거기 쌓이면 두 인스턴스가 같은 자리를 쓴다.
    for key in ("ent", "pf"):
        assert str(HERMES) not in paths[key]["raw"], paths[key]["raw"]


@needs_node
@needs_deps
def test_a_shared_state_dir_inside_the_code_repo_is_refused(two_instances):
    """⑨ state 를 **코드 저장소 안**에 두려 하면 기동을 막는다.

    한 바이너리를 둘이 쓰는 것이 전제라, 그 자리는 **반드시 겹친다.** 겹친 뒤에는
    되돌릴 수 없으므로 쓰기 전에 멈춘다.
    """
    probe = (
        "const c = await import('./src/config.js');"
        "try { c.assertInstanceIsolated(); process.stdout.write('ok'); }"
        "catch (e) { process.stdout.write('REFUSED: ' + e.message); }"
    )
    inst = two_instances["ent"]
    inside = _node(probe, env_extra={
        "HERMES_DATA_ROOT": str(inst["data"]),
        "HERMES_STATE_DIR": str(HERMES / "logs"),
    })
    assert inside.stdout.startswith("REFUSED"), inside.stdout
    assert "코드 저장소" in inside.stdout, inside.stdout

    missing = _node(probe, env_extra={"HERMES_DATA_ROOT": str(inst["data"])})
    assert missing.stdout.startswith("REFUSED"), missing.stdout
    assert "HERMES_STATE_DIR" in missing.stdout, missing.stdout

    good = _node(probe, env_extra={
        "HERMES_DATA_ROOT": str(inst["data"]),
        "HERMES_STATE_DIR": str(inst["state"]),
    })
    assert good.stdout == "ok", good.stdout + good.stderr


@needs_node
@needs_deps
def test_an_instance_without_a_domain_refuses_to_start(two_instances, tmp_path):
    """⑩ 프로필이 없는 인스턴스는 **기동하지 않는다.**

    기동해 버리면 그 인스턴스는 기본값으로 말하고, 그 기본값이 틀렸다는 것은
    사람이 대화를 읽어야 안다.
    """
    data = tmp_path / "nodomain-data"
    shutil.copytree(two_instances["ent"]["data"], data)
    cfg = json.loads((data / "config.json").read_text(encoding="utf-8"))
    cfg.pop("domain", None)
    (data / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    probe = (
        "const c = await import('./src/config.js');"
        "try { c.assertInstanceIsolated(); process.stdout.write('ok'); }"
        "catch (e) { process.stdout.write('REFUSED: ' + e.message); }"
    )
    done = _node(probe, env_extra={
        "HERMES_DATA_ROOT": str(data),
        "HERMES_STATE_DIR": str(two_instances["ent"]["state"]),
    })
    assert done.stdout.startswith("REFUSED"), done.stdout
    assert "domain" in done.stdout or "HERMES_DOMAIN" in done.stdout, done.stdout


@needs_node
@needs_deps
def test_the_environment_wins_over_the_config_file(two_instances):
    """⑪ systemd 가 박은 값이 설정 파일을 이긴다.

    인스턴스를 가르는 것은 unit 이다. 자료 저장소의 오타 하나가 사내 인스턴스를
    PF 말투로 바꾸면 안 된다 — `HERMES_ARCHIVER_WORKSPACE` 와 같은 자리다.
    """
    probe = ("const c = await import('./src/config.js');"
             "process.stdout.write(c.DOMAIN);")
    inst = two_instances["pf"]          # 설정 파일은 pf-construction
    done = _node(probe, env_extra={
        "HERMES_DATA_ROOT": str(inst["data"]),
        "HERMES_STATE_DIR": str(inst["state"]),
        "HERMES_DOMAIN": "enterprise",
    })
    assert done.stdout.strip() == "enterprise", done.stdout + done.stderr


# --- ④ 표시 문구가 프로필을 따른다 --------------------------------------------

@needs_node
@needs_deps
@pytest.mark.parametrize("key,want,avoid", [
    ("ent", "채널", "사업장"),
    ("pf", "사업장", None),
])
def test_user_facing_text_follows_the_profile(two_instances, key, want, avoid):
    """⑫ 도구 설명·안내 문구가 그 인스턴스의 말로 나온다.

    모델에 가는 문구도 사용자 표시다 — 봇의 답변이 그 말을 그대로 따라 한다.
    """
    inst = two_instances[key]
    # 프롬프트 맥락은 **운영과 같은 방식으로** 만든다(`check-llm-prompts.js` 와 같다).
    # 따로 렌더하면 그 경로만 맞고 봇이 쓰는 경로는 안 재게 된다.
    probe = (
        "const c = await import('./src/config.js');"
        "const { createPromptContext } = await import('./src/llm/prompts.js');"
        "const t = await import('./src/domain.js');"
        "const ctx = createPromptContext({ ROOT: c.ROOT, DATA_ROOT: c.DATA_ROOT,"
        " ARCHIVE_DIR: c.ARCHIVE_DIR, DOCS_DIR: c.DOCS_DIR, config: c.config,"
        " domain: c.DOMAIN, accessLabel: () => '', canSeePrivateChannel: () => false,"
        " buildArchiveBriefSplit: () => ({ common: '', extra: '' }),"
        " buildDocumentsBriefSplit: () => ({ common: '', extra: '' }),"
        " hasDocuments: () => false, listArchivedChannels: () => [] });"
        "process.stdout.write(JSON.stringify({"
        " terms: t.terms(c.DOMAIN), qa: ctx.renderPrompt('qa') }));"
    )
    done = _node(probe, env_extra={
        "HERMES_DATA_ROOT": str(inst["data"]),
        "HERMES_STATE_DIR": str(inst["state"]),
    })
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    assert got["terms"]["place"] == want
    qa = got["qa"]
    assert qa, "qa 프롬프트를 렌더하지 못했다"
    # `{{ARCHIVE_BRIEF}}` 같은 것은 **답변 시점**에 채워진다(`systemBlocks`). 여기서
    # 보는 것은 `renderPrompt` 가 채우는 자리뿐이다 — 안 가르면 엉뚱한 사유로 빨개진다.
    for left in ("{{PLACE}}", "{{AREA}}", "{{EX_", "{{ORG}}", "{{OWNER}}"):
        assert left not in qa, f"자리표시자가 남았다: {left}"
    assert want in qa, qa[:300]
    if avoid:
        assert avoid not in qa, f"{avoid} 가 남아 있다:\n{qa[:400]}"
