"""`npm run check` 의 **관측성** — 어느 검사가 돌고 있는지, 얼마나 걸렸는지.

배경: `docs/verification/2026-10-07-hermes-archiver-reader-performance.md`

## 무엇이 문제였나

2026-10-07 TYIT 사전 검사에서 `check-brief-split.js` 가 12분 넘게 CPU 한 코어를
먹었다. 화면의 마지막 줄은 **이미 끝난 항목**이었다. 사람이 본 것은 「멈춘 것 같은
화면」이고, 실제로는 돌고 있었다.

두 겹이 겹쳤다.

1. `check-setup.js` 가 `spawnSync(..., {encoding})` 로 자식 출력을 **버퍼에 가둔다.**
   자식이 끝나기 전에는 한 글자도 안 보이고, 끝나도 통과한 검사는 `[보임]`·`[못잼]`
   줄만 올라온다. 중간에 죽으면 그때까지의 출력도 **통째로 사라진다**
2. 자식에 **상한이 없다.** 영원히 돌아도 그대로 기다린다

둘 다 오류가 아니라 **침묵**으로 나타난다. 침묵은 「빠르다」 와 화면에서 같다.

## 여기서 고정하는 것

| 무엇 | 왜 |
|---|---|
| 시작 **즉시** 이름을 찍는다 | 돌고 있는 것이 무엇인지 그 자리에서 보여야 한다 |
| 끝나면 소요 시간을 찍는다 | 「느리다」 를 다음 사람이 숫자로 받는다 |
| 상한을 넘기면 **어느 검사인지 적고** 실패한다 | 이름 없는 타임아웃은 다시 재현해야 안다 |
| 중단돼도 **그때까지의 출력**을 보인다 | 12분을 기다린 사람이 빈 화면을 받으면 안 된다 |
| `[보임]`·`[못잼]` 판독은 그대로 | 통과한 검사의 「못 쟀다」 를 삼키지 않는 장치다 |

## 왜 helper 를 따로 두나

`check-setup.js` 는 자식 100여 개를 돌리는 941줄짜리라 통째로 시험에 넣을 수 없다
(네트워크 없이도 수 분이 걸린다). 그래서 **그 판정만** `scripts/_child-run.js` 로
떼어내 여기서 직접 몬다. `check-setup.js` 는 그것을 부르기만 한다 — 두 벌로 두면
한쪽만 고쳐지고, 그때 조용해지는 쪽은 늘 운영이다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "subbots" / "hermes"
HELPER = HERMES / "scripts" / "_child-run.js"
SETUP = HERMES / "scripts" / "check-setup.js"
SPLIT = HERMES / "scripts" / "check-brief-split.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None or not HERMES.is_dir(), reason="node 또는 subbots/hermes 가 없다"
)
needs_deps = pytest.mark.skipif(
    not (HERMES / "node_modules").is_dir(),
    reason="subbots/hermes/node_modules 가 없어 실행 시험은 건너뛴다",
)


def _node(code: str, *, env_extra: dict | None = None,
          cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(
        [NODE, "--input-type=module", "-e", code], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(cwd or HERMES), env=env,
    )


def _child(tmp_path: Path, name: str, body: str) -> Path:
    """자식 검사 흉내. **실제 파일로 둔다** — helper 가 경로를 받아 돌리기 때문이다."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


#: helper 를 몰고 수집한 줄을 JSON 으로 뱉는 탐침.
_PROBE = """
const {{ runCheck }} = await import({helper});
const log = [], ok = [], bad = [];
const r = runCheck({{
  file: {file}, label: {label}, limitMs: {limit},
  log: (m) => log.push(String(m)),
  ok: (m) => ok.push(String(m)),
  bad: (m) => bad.push(String(m)),
}});
process.stdout.write(JSON.stringify({{
  log, ok, bad,
  ok_: r.ok, timedOut: r.timedOut, ms: r.ms, visible: r.visible, output: r.output,
}}));
"""


def _run_helper(child: Path, label: str, limit_ms: int = 5000) -> dict:
    code = _PROBE.format(
        helper=json.dumps(HELPER.as_uri()),
        file=json.dumps(str(child)),
        label=json.dumps(label, ensure_ascii=False),
        limit=limit_ms,
    )
    done = _node(code)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


# --- ① 시작 즉시 이름 ---------------------------------------------------------

@needs_node
def test_the_name_is_printed_before_the_child_runs(tmp_path):
    """① 시작 줄이 **자식보다 먼저** 나온다.

    끝난 뒤에 찍으면 화면의 마지막 줄은 늘 **이미 끝난 항목**이고, 돌고 있는 것이
    무엇인지 알 방법이 없다 — 2026-10-07 에 사람이 본 화면이 그것이다.
    """
    child = _child(tmp_path, "fast.js", "console.log('자식이 적은 줄');\n")
    got = _run_helper(child, "빠른 검사")

    assert got["log"], "시작 줄이 없다"
    assert any("빠른 검사" in line for line in got["log"]), got["log"]
    # 시작 줄은 **결과보다 앞**이다. helper 가 먼저 찍고 돌린다.
    assert got["ok_"] is True
    assert got["ok"] and "빠른 검사" in got["ok"][0]


@needs_node
def test_the_elapsed_time_is_printed_on_completion(tmp_path):
    """② 끝나면 소요 시간이 보인다 — 「느리다」 를 다음 사람이 숫자로 받는다."""
    child = _child(tmp_path, "slow.js",
                   "const t=Date.now(); while(Date.now()-t<250);\n")
    got = _run_helper(child, "조금 느린 검사")

    assert got["ms"] >= 200, got["ms"]
    done = " ".join(got["ok"])
    assert "초" in done or "ms" in done, done
    # 숫자가 실제로 실린다 — 「(0초)」 같은 고정 문구가 아니다.
    assert any(ch.isdigit() for ch in done), done


# --- ② 상한 ------------------------------------------------------------------

@needs_node
def test_a_child_over_the_limit_fails_and_is_named(tmp_path):
    """③ 상한을 넘기면 **어느 검사인지 적고** 실패한다.

    이름 없는 타임아웃은 다시 재현해야 알 수 있다. 12분을 기다린 사람에게 그걸
    또 시키지 않는다.
    """
    child = _child(tmp_path, "hang.js",
                   "setInterval(()=>{}, 1000);\n")   # 영원히 안 끝난다
    got = _run_helper(child, "멈춘 검사", limit_ms=700)

    assert got["timedOut"] is True
    assert got["ok_"] is False
    assert not got["ok"], got["ok"]
    joined = " ".join(got["bad"])
    assert "멈춘 검사" in joined, joined        # 어느 검사인지
    assert "hang.js" in joined, joined          # 어느 파일인지
    assert "상한" in joined, joined


@needs_node
def test_partial_output_survives_the_kill(tmp_path):
    """④ 중단돼도 **그때까지의 출력**을 보인다.

    지금 동작은 끝까지 버퍼에 가두는 것이라, 죽으면 아무것도 안 남는다. 12분을
    기다린 사람이 빈 화면을 받으면 어디까지 갔는지조차 모른다.
    """
    child = _child(tmp_path, "chatty-hang.js",
                   "console.log('여기까지 갔습니다');\n"
                   "setInterval(()=>{}, 1000);\n")
    got = _run_helper(child, "말하다 멈춘 검사", limit_ms=700)

    assert got["timedOut"] is True
    assert "여기까지 갔습니다" in got["output"], got["output"]


@needs_node
def test_the_limit_comes_from_the_environment(tmp_path):
    """⑤ 상한은 환경변수로 조정된다 — 느린 서버에서 관문을 통째로 끄게 두지 않는다."""
    code = (
        f"const m = await import({json.dumps(HELPER.as_uri())});"
        "process.stdout.write(JSON.stringify("
        "{ d: m.DEFAULT_LIMIT_MS, env: m.childLimitMs({ HERMES_CHECK_TIMEOUT_MS: '4321' }),"
        "  bad: m.childLimitMs({ HERMES_CHECK_TIMEOUT_MS: '아니오' }) }));"
    )
    got = json.loads(_node(code).stdout)
    assert got["d"] > 0
    assert got["env"] == 4321
    # 읽을 수 없는 값은 **기본값으로 돌아간다.** 0 으로 읽으면 모든 검사가 즉시 실패한다.
    assert got["bad"] == got["d"]


# --- ③ 판독은 그대로 ---------------------------------------------------------

@needs_node
def test_the_visible_and_unmeasured_markers_still_surface(tmp_path):
    """⑥ `[보임]`·`[못잼]` 판독을 잃지 않는다.

    통과한 검사가 「한 항목도 못 쟀다」 를 적어도 종료코드는 0 이다. 그 줄을 삼키면
    안전망이 꺼진 것이 어디에도 안 보인다(2026-09-01 실측).
    """
    child = _child(tmp_path, "tagged.js",
                   "console.log('[보임] 대본 것 12건');\n"
                   "console.log('[못잼] 질의 파일이 없어 한 항목도 못 쟀습니다');\n"
                   "console.log('이 줄은 안 올라와야 한다');\n")
    got = _run_helper(child, "표시를 단 검사")

    assert got["ok_"] is True
    joined = " ".join(got["visible"])
    assert "대본 것 12건" in joined, got["visible"]
    assert "질의 파일이 없어" in joined, got["visible"]
    assert "안 올라와야" not in joined, got["visible"]


@needs_node
def test_a_failing_child_shows_its_whole_output(tmp_path):
    """⑦ 실패한 검사는 사유 전문을 그대로 보인다 — 지금 동작 그대로다."""
    child = _child(tmp_path, "fails.js",
                   "console.log('첫 줄');\nconsole.error('사유 줄');\nprocess.exit(1);\n")
    got = _run_helper(child, "실패한 검사")

    assert got["ok_"] is False and got["timedOut"] is False
    assert "첫 줄" in got["output"] and "사유 줄" in got["output"]
    assert "실패한 검사" in " ".join(got["bad"])


# --- ④ check-setup 이 그 판정을 쓴다 -----------------------------------------

@needs_node
def test_check_setup_delegates_to_the_shared_runner():
    """⑧ 판정이 두 벌이면 한쪽만 고쳐지고, 조용해지는 쪽은 늘 운영이다."""
    assert HELPER.is_file(), "공통 러너가 없다"
    body = SETUP.read_text(encoding="utf-8")
    assert "_child-run.js" in body, "check-setup 이 공통 러너를 안 쓴다"
    assert "runCheck(" in body
    # 자기 `spawnSync` 로 자식 검사를 또 돌리지 않는다. (다른 용도의 spawnSync 는 남는다.)
    start = body.index("CROSS_CHECKS")
    chunk = body[start : body.index("[2/", start) if "[2/" in body[start:] else len(body)]
    assert "spawnSync(process.execPath" not in chunk, "자식 검사를 아직 직접 돌린다"


# --- ⑤ check-brief-split 의 구간 표시 ----------------------------------------

@pytest.fixture(scope="module")
def pf_dataroot(tmp_path_factory) -> Path:
    """`pf` 모드 자료 저장소. 권한 조합이 **둘 이상** 나오게 비공개 채널을 둔다."""
    root = tmp_path_factory.mktemp("pfdata")
    channels = root / "slack-export" / "channels"
    channels.mkdir(parents=True)
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
    cfg["workspace"] = "시험"
    cfg["org"] = "시험"
    cfg["owner"] = {"name": "담당", "label": "", "slackUserId": "U0000000001"}
    cfg["privateChannels"] = ["인사비공개", "법무비공개"]
    cfg["digest"]["channelId"] = "C0000000001"
    cfg["digest"]["deliverTo"] = "dm"
    cfg["digest"]["skipChannels"] = []
    cfg.pop("archiver", None)
    (root / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    (root / "slack-export" / "index.md").write_text("# 색인\n\n## #자금\n", encoding="utf-8")
    for name in ("자금", "인사비공개", "법무비공개"):
        (channels / f"{name}.md").write_text(
            f"# #{name}\n\n> 메타\n\n## 2026-10\n\n"
            f"**2026-10-02 09:00 · 김과장**\n{name} 채널 문장.\n\n---\n",
            encoding="utf-8",
        )
    (root / "slack-export" / ".sync-state.json").write_text(json.dumps({"channels": {
        "C1": {"name": "자금", "file": "자금", "last_ts": "1", "threads": {}},
        "C2": {"name": "인사비공개", "file": "인사비공개", "last_ts": "1", "threads": {}},
        "C3": {"name": "법무비공개", "file": "법무비공개", "last_ts": "1", "threads": {}},
    }}), encoding="utf-8")
    return root


@needs_node
@needs_deps
def test_brief_split_names_each_case_before_doing_the_work(pf_dataroot):
    """⑨ 권한 조합마다 **일을 시작하기 전에** 그 이름이 보인다.

    이 검사의 느린 자리가 바로 조합별 색인 생성이다. 끝난 뒤에만 찍으면 화면은
    늘 한 조합 뒤처지고, 사람은 **이미 끝난 것**을 「지금 도는 것」 으로 읽는다.
    """
    done = subprocess.run(
        [NODE, "scripts/check-brief-split.js"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(HERMES),
        env=dict(os.environ, HERMES_DATA_ROOT=str(pf_dataroot)),
    )
    out = done.stdout + done.stderr
    # **종료코드는 보지 않는다.** 여기서 재는 것은 관측성이고, 합격 여부는 그 자료가
    # 얼마나 풍부한가에 달려 있다 — 둘을 묶으면 fixture 가 얇아질 때 관측성 시험이
    # 엉뚱한 사유로 빨개진다.
    # 권한 조합 수를 **시작할 때** 적는다 — 몇 번 돌 일인지 알아야 기다릴지 판단한다.
    assert "권한 조합" in out, out
    # 조합 이름이 일 전에 나온다: 시작 표시(`…`)가 그 조합의 결과(`✓`)보다 앞이다.
    for label in ("공개 전용", "전체"):
        starts = [i for i, line in enumerate(out.split("\n"))
                  if label in line and "…" in line]
        results = [i for i, line in enumerate(out.split("\n"))
                   if label in line and "✓" in line]
        assert starts, f"{label} 시작 줄이 없다:\n{out}"
        assert results, f"{label} 결과 줄이 없다:\n{out}"
        assert min(starts) < min(results), f"{label} 가 끝난 뒤에만 보인다:\n{out}"


@needs_node
@needs_deps
def test_brief_split_reports_its_own_duration_and_combination_count(pf_dataroot):
    """⑩ 전체 소요 시간과 권한 조합 수를 **스스로** 적는다.

    PF 인계 문서가 요구하는 숫자다(§PF 개발자 확인 사항 1·3). 사람이 초시계를 들고
    재게 하면 아무도 안 잰다. `[보임]` 으로 적으면 `check-setup` 이 통과 화면에도
    올려 준다.
    """
    done = subprocess.run(
        [NODE, "scripts/check-brief-split.js"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(HERMES),
        env=dict(os.environ, HERMES_DATA_ROOT=str(pf_dataroot)),
    )
    out = done.stdout + done.stderr   # 종료코드는 보지 않는다 — 위 시험과 같은 이유
    visible = [line for line in out.split("\n") if line.strip().startswith("[보임]")]
    assert visible, f"[보임] 줄이 없다 — check-setup 통과 화면에 안 올라온다:\n{out}"
    joined = " ".join(visible)
    assert "조합" in joined, joined
    assert any(ch.isdigit() for ch in joined), joined
    # 비공개 둘 → 공개 전용 + 멤버 2 + DM 2개 + 전체 = 5 조합
    assert "5" in joined, joined


# --- ⑥ 실패·중단해도 「못 쟀다」 는 안 묻힌다 ---------------------------------

@needs_node
def test_markers_survive_a_failing_child(tmp_path):
    """⑪ **실패한 검사의 `[못잼]` 도 올라온다.**

    전에는 실패하면 `visible` 을 비우고 출력 전문만 찍었다. 전문 속에 섞인 그 줄은
    사유 줄들 사이에 묻힌다 — 그런데 「한 항목도 못 쟀다」 는 **실패와 별개의 사실**
    이다. 검사가 왜 떨어졌는지와, 그 검사가 애초에 무엇을 못 쟀는지는 다른 조치로 이어진다.
    """
    child = _child(tmp_path, "fail-tagged.js",
                   "console.log('[못잼] 질의 파일이 없어 절반은 못 쟀습니다');\n"
                   "console.error('그리고 이 단정이 깨졌습니다');\n"
                   "process.exit(1);\n")
    got = _run_helper(child, "반쯤 못 잰 검사")

    assert got["ok_"] is False
    assert any("절반은 못 쟀" in line for line in got["visible"]), got["visible"]
    # 전문도 그대로 보인다 — 하나를 얻으려고 다른 하나를 잃지 않는다.
    assert "그리고 이 단정이 깨졌습니다" in got["output"]


@needs_node
def test_markers_survive_a_timed_out_child(tmp_path):
    """⑫ 중단된 검사가 죽기 전에 적은 표시도 올라온다."""
    child = _child(tmp_path, "tagged-hang.js",
                   "console.log('[보임] 여기까지 42건 대봤습니다');\n"
                   "setInterval(()=>{}, 1000);\n")
    got = _run_helper(child, "표시 뒤 멈춘 검사", limit_ms=700)

    assert got["timedOut"] is True
    assert any("42건" in line for line in got["visible"]), got["visible"]


# --- ⑦ 구간 예산 -------------------------------------------------------------

_PHASE_PROBE = """
const m = await import({helper});
const log = [], bad = [];
const okFlag = m.reportPhase({{
  label: {label}, ms: {ms}, budgetMs: {budget},
  children: {children},
  log: (x) => log.push(String(x)), bad: (x) => bad.push(String(x)),
}});
process.stdout.write(JSON.stringify({{ log, bad, ok: okFlag }}));
"""


def _phase(label: str, ms: int, budget: int, children: list[dict]) -> dict:
    code = _PHASE_PROBE.format(
        helper=json.dumps(HELPER.as_uri()),
        label=json.dumps(label, ensure_ascii=False),
        ms=ms, budget=budget,
        children=json.dumps(children, ensure_ascii=False),
    )
    done = _node(code)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


@needs_node
def test_a_phase_within_budget_only_reports_its_total():
    """⑬ 예산 안이면 합계만 적는다. 늘 빨개지는 관문은 사람이 끄는 법부터 배운다."""
    got = _phase("코드 일관성", 5000, 60000,
                 [{"label": "가", "ms": 3000}, {"label": "나", "ms": 2000}])

    assert got["ok"] is True
    assert not got["bad"], got["bad"]
    joined = " ".join(got["log"])
    assert "[보임]" in joined and "코드 일관성" in joined, joined
    assert "2" in joined, joined          # 자식 수


@needs_node
def test_a_phase_over_budget_fails_and_names_the_slowest():
    """⑭ 구간이 예산을 넘기면 **구간 이름과 가장 느린 자식**을 적고 실패한다.

    자식 상한으로는 못 잡는 종류가 있다 — 자식 하나하나는 상한 안인데 **수가 늘어**
    구간이 느려지는 경우다. 그때 「어느 검사가」 가 없으면 다음 사람은 전체를 다시
    재는 것 말고 할 수 있는 일이 없다.
    """
    got = _phase("코드 일관성", 90000, 60000, [
        {"label": "느린 검사", "ms": 40000},
        {"label": "둘째", "ms": 30000},
        {"label": "셋째", "ms": 15000},
        {"label": "빠른 검사", "ms": 5000},
    ])

    assert got["ok"] is False
    joined = " ".join(got["bad"])
    assert "코드 일관성" in joined, joined
    assert "예산" in joined, joined
    assert "느린 검사" in joined, joined
    assert "둘째" in joined, joined
    # 빠른 것까지 다 적으면 느린 것이 묻힌다.
    assert "빠른 검사" not in joined, joined


@needs_node
def test_the_phase_budget_can_be_turned_off_and_comes_from_the_environment():
    """⑮ 예산은 환경변수로 조정하고 `0` 으로 끈다.

    끌 수 있어야 한다 — 느린 서버에서 못 끄면 사람이 관문 자체를 지운다.
    """
    code = (
        f"const m = await import({json.dumps(HELPER.as_uri())});"
        "process.stdout.write(JSON.stringify({"
        " d: m.DEFAULT_PHASE_BUDGET_MS,"
        " env: m.phaseBudgetMs({ HERMES_CHECK_PHASE_BUDGET_MS: '1234' }),"
        " off: m.phaseBudgetMs({ HERMES_CHECK_PHASE_BUDGET_MS: '0' }),"
        " bad: m.phaseBudgetMs({ HERMES_CHECK_PHASE_BUDGET_MS: '아니오' }) }));"
    )
    got = json.loads(_node(code).stdout)
    assert got["d"] > 0
    assert got["env"] == 1234
    assert got["off"] == 0
    # 읽을 수 없는 값은 **기본값**이다 — 0 으로 읽으면 예산이 조용히 꺼진다.
    assert got["bad"] == got["d"]


@needs_node
def test_a_disabled_budget_never_fails():
    """⑯ 꺼 두면 아무리 느려도 실패하지 않는다. 합계는 그대로 적는다."""
    got = _phase("코드 일관성", 10_000_000, 0, [{"label": "가", "ms": 10_000_000}])
    assert got["ok"] is True
    assert not got["bad"]
    assert any("[보임]" in line for line in got["log"])


@needs_node
def test_check_setup_uses_the_phase_budget():
    """⑰ 구간 예산을 `check-setup` 이 실제로 쓴다 — 안 부르면 helper 만 있는 것이다."""
    body = SETUP.read_text(encoding="utf-8")
    assert "reportPhase(" in body, "구간 예산을 안 쓴다"
    assert "phaseBudgetMs(" in body


# --- ⑧ 신호 종료를 상한 초과로 잘못 말하지 않는다 ------------------------------

_CLASSIFY_PROBE = """
const m = await import({helper});
process.stdout.write(JSON.stringify({{
  timeout: m.classifyExit({{ error: {{ code: 'ETIMEDOUT' }}, status: null, signal: 'SIGKILL' }}),
  selfSignal: m.classifyExit({{ status: null, signal: 'SIGTERM' }}),
  abort: m.classifyExit({{ status: null, signal: 'SIGABRT' }}),
  ok: m.classifyExit({{ status: 0, signal: null }}),
  failed: m.classifyExit({{ status: 1, signal: null }}),
  spawnError: m.classifyExit({{ error: {{ code: 'ENOENT' }}, status: null, signal: null }}),
  names: {{ ok: m.EXIT_OK, failed: m.EXIT_FAILED, timeout: m.EXIT_TIMEOUT, signal: m.EXIT_SIGNAL }},
}}));
"""


@needs_node
def test_a_signal_death_is_not_a_timeout():
    """⑱ `status === null && signal` 만 보고 **상한 초과**라고 말하면 안 된다.

    자식이 스스로 `SIGTERM`·`SIGABRT` 로 죽는 경우가 바로 그 모양이다. 그때
    「상한을 넘겼습니다」 라고 적으면 다음 사람은 **상한을 올린다** — 고칠 곳은
    그 검사가 왜 죽었는지인데, 엉뚱한 데를 고치고 증상은 그대로 남는다.

    윈도우는 자기 신호 종료를 `status:1, signal:null` 로 보고해서 이 결함이 안
    드러난다. **서버(POSIX)에서만** 드러나므로 판정을 순수 함수로 떼어 잰다.
    """
    code = _CLASSIFY_PROBE.format(helper=json.dumps(HELPER.as_uri()))
    got = json.loads(_node(code).stdout)

    assert got["timeout"] == got["names"]["timeout"]
    assert got["selfSignal"] == got["names"]["signal"], "자기 신호 종료를 상한으로 읽었다"
    assert got["abort"] == got["names"]["signal"]
    assert got["ok"] == got["names"]["ok"]
    assert got["failed"] == got["names"]["failed"]
    # spawn 자체가 실패한 것(ENOENT)은 신호도 상한도 아니다.
    assert got["spawnError"] == got["names"]["failed"]


#: `spawnSync` 를 주입해 신호 종료를 **어느 플랫폼에서나** 만든다.
_SIGNAL_PROBE = """
const {{ runCheck }} = await import({helper});
const log = [], ok = [], bad = [];
const r = runCheck({{
  file: 'scripts/가짜검사.js', label: '스스로 죽은 검사', limitMs: 60000,
  spawnFn: () => ({{ status: null, signal: {signal}, stdout: {out}, stderr: {err} }}),
  log: (m) => log.push(String(m)), ok: (m) => ok.push(String(m)), bad: (m) => bad.push(String(m)),
}});
process.stdout.write(JSON.stringify({{
  log, ok, bad, ok_: r.ok, timedOut: r.timedOut,
  killedBySignal: r.killedBySignal, signal: r.signal,
  output: r.output, visible: r.visible,
}}));
"""


#: 죽기 전 출력. **heredoc 을 거치지 않게** 여기서 JSON 으로 넘긴다 — 이스케이프가
#: 중간에 한 번이라도 풀리면 JS 문자열 안에 진짜 줄바꿈이 들어가 문법 오류가 된다.
_DYING_STDOUT = "\n".join(["[보임] 여기까지 7건 대봤습니다", "죽기 전 마지막 줄", ""])
_DYING_STDERR = "[못잼] 나머지는 못 쟀습니다\n"


def _signal_probe(signal: str) -> str:
    return _SIGNAL_PROBE.format(
        helper=json.dumps(HELPER.as_uri()),
        signal=json.dumps(signal),
        out=json.dumps(_DYING_STDOUT, ensure_ascii=False),
        err=json.dumps(_DYING_STDERR, ensure_ascii=False),
    )


@needs_node
@pytest.mark.parametrize("signal", ["SIGTERM", "SIGABRT"])
def test_a_signal_death_names_the_signal_and_the_file(signal):
    """⑲ 실패 문구에 **신호 이름과 검사 파일**이 보인다.

    「죽었습니다」 만으로는 다음 사람이 할 수 있는 일이 없다. 어느 신호인지가
    메모리 부족(`SIGKILL`)·단정 실패(`SIGABRT`)·누가 끊었나(`SIGTERM`)를 가른다.
    """
    code = _signal_probe(signal)
    got = json.loads(_node(code).stdout)

    assert got["ok_"] is False
    assert got["timedOut"] is False, "신호 종료를 상한 초과로 분류했다"
    assert got["killedBySignal"] is True
    assert got["signal"] == signal
    joined = " ".join(got["bad"])
    assert signal in joined, joined
    assert "스스로 죽은 검사" in joined, joined
    assert "가짜검사.js" in joined, joined
    assert "상한" not in joined, f"상한 초과라고 말한다: {joined}"


@needs_node
def test_a_signal_death_keeps_its_output_and_markers():
    """⑳ 죽기 전 stdout·stderr 와 `[보임]`·`[못잼]` 이 보존된다.

    어디까지 갔는지와 무엇을 못 쟀는지는 **죽은 뒤에도 알아야 하는 것**이다.
    """
    code = _signal_probe("SIGTERM")
    got = json.loads(_node(code).stdout)

    assert "죽기 전 마지막 줄" in got["output"]
    joined = " ".join(got["visible"])
    assert "7건" in joined, got["visible"]
    assert "나머지는 못 쟀습니다" in joined, got["visible"]
    printed = "\n".join(got["log"])
    assert "죽기 전 마지막 줄" in printed, printed
    assert "스스로 지목한 줄" in printed, printed


@needs_node
def test_a_real_timeout_is_still_a_timeout(tmp_path):
    """㉑ 진짜 `ETIMEDOUT` 은 **그대로 상한 초과**다 — 고치면서 잃으면 안 된다."""
    child = _child(tmp_path, "hang2.js", "setInterval(()=>{}, 1000);\n")
    got = _run_helper(child, "진짜 멈춘 검사", limit_ms=700)

    assert got["timedOut"] is True
    assert got["ok_"] is False
    assert "상한" in " ".join(got["bad"])


@needs_node
@pytest.mark.skipif(os.name == "nt",
                    reason="윈도우는 자기 신호 종료를 status:1 로 보고해 이 갈래가 안 생긴다")
def test_a_real_self_killed_child_is_not_reported_as_a_timeout(tmp_path):
    """㉒ 실제 프로세스로도 확인한다 — 주입한 가짜만 맞고 실물이 틀리면 뜻이 없다."""
    child = _child(tmp_path, "selfkill.js",
                   "console.log('죽기 전 줄');\n"
                   "process.kill(process.pid, 'SIGTERM');\n"
                   "setInterval(()=>{}, 1000);\n")
    got = _run_helper(child, "스스로 끝낸 검사", limit_ms=10000)

    assert got["timedOut"] is False, got
    assert got["killedBySignal"] is True
    assert "SIGTERM" in " ".join(got["bad"])
    assert "죽기 전 줄" in got["output"]
