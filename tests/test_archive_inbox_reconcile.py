"""archive-inbox ↔ TYBot DM **공통 결정 기록의 계약** — 양쪽이 같은 말을 쓰는가.

설계: `docs/design/summary-approval-ports.md` §3

## 왜 이 시험이 있나

두 구현이 다른 언어·다른 저장소 뿌리에서 돈다. Hermes 는 tybot 패키지를 import 할
수 없으므로 형식 판정이 **두 벌** 있다. 두 벌은 갈릴 수 있고, **갈려도 오류가 안
난다** — 대조가 조용히 0건이 되고, 그 상태는 「상대편이 아직 아무것도 안 했다」 와
화면에서 똑같이 보인다.

그래서 여기서 둘을 맞대어 본다. 키 이름 하나, 스키마 문자열 하나가 어긋나면 실패한다.

## 좌표계는 **일부러** 안 맞춘다

Hermes 의 좌표는 `slack-export/channels/<채널>.md` 의 **메시지 블록**이고 TYBot 은
`workspaces/.../raw/<날짜>.md` 의 **줄**이다. 그래서 두 체계 사이에서는 지금
`no_match` 가 나오고, 그러면 다시 묻는다 — 그게 맞는 동작이다. 변환층은 만들지
않는다: 변환이 어긋난 날 다른 메시지를 같은 것으로 보게 되고, 그건 조용히 틀린다.
여기서 재는 것은 **형식의 일치**이고 좌표의 일치가 아니다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tybot import summary_review_reconcile as rec

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "subbots" / "hermes" / ".claude" / "skills" / "archive-inbox" / "scripts"

needs_scripts = pytest.mark.skipif(
    not SCRIPTS.is_dir(), reason="subbots/hermes 의 archive-inbox 스킬이 없다"
)

#: Hermes 쪽 상수를 **별 프로세스에서** 읽는다. `review_work` 는 자료 저장소 경로와
#: Slack 쪽 모듈을 끌어오므로 pytest 프로세스에 들이지 않는다 — 들이면 이 파일의
#: 실패가 「계약이 갈렸다」 인지 「환경이 없다」 인지 구별되지 않는다.
#: 경로만 f-string 으로 앞에 붙인다. 본문을 f-string 으로 두면 아래 JSON 의 `{`
#: 전부가 서식 자리로 읽혀 수집 단계에서 터진다.
_PATH_LINE = f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"

PROBE = _PATH_LINE + """
import json, sys
import decision_export as X
json.dump({
    "schema": X.EXPORT_SCHEMA,
    "source": X.SOURCE,
    "env": X.EXPORT_DIR_ENV,
    "record_keys": sorted(X.RECORD_KEYS),
    "bucket_states": X.BUCKET_STATES,
    "reasons": {
        "skip": X.SKIP, "records_unreadable": X.RECORDS_UNREADABLE,
        "no_match": X.NO_MATCH, "not_final": X.NOT_FINAL,
        "source_mismatch": X.SOURCE_MISMATCH, "evidence_changed": X.EVIDENCE_CHANGED,
        "no_coordinate": X.NO_COORDINATE, "conflict": X.CONFLICT,
    },
    "final_states": sorted(X.FINAL_STATES),
    "path": X.export_path("/root", workspace="TEC").as_posix(),
}, sys.stdout)
"""


@pytest.fixture(scope="module")
def hermes() -> dict:
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    if out.returncode != 0:
        pytest.skip(f"Hermes 쪽 모듈을 못 불렀다: {out.stderr.strip().splitlines()[-1:]}")
    return json.loads(out.stdout)


@needs_scripts
def test_both_sides_name_the_same_schema(hermes):
    """① 스키마 문자열이 다르면 서로의 파일을 **「못 읽음」** 으로 본다."""
    assert hermes["schema"] == rec.EXPORT_SCHEMA


@needs_scripts
def test_both_sides_name_the_same_source_and_env(hermes):
    """② 출처 이름이 다르면 자기 파일을 상대편 것으로 읽어 자기 결정으로 자기를 막는다."""
    assert hermes["source"] == rec.SOURCE_HERMES_INBOX
    assert hermes["env"] == rec.EXPORT_DIR_ENV


@needs_scripts
def test_both_sides_write_the_same_record_keys(hermes):
    """③ 키 하나가 달라도 그 값은 받는 쪽에서 **빈 문자열**이 된다 — 오류가 아니다."""
    mine = rec.to_record({
        "id": "x", "workspace": "T1", "channel_id": "C1",
        "evidence_locator": "a.md:1", "evidence_hash": "h", "state": "approved",
    })
    assert sorted(mine) == hermes["record_keys"]


@needs_scripts
def test_both_sides_map_decision_words_the_same(hermes):
    """④ 「반영·빼」 를 승인·거절로 옮기는 말이 양쪽에서 같아야 한다."""
    for bucket, common in hermes["bucket_states"].items():
        assert rec.STATE_ALIASES.get(bucket) == common, bucket


@needs_scripts
def test_both_sides_use_the_same_reason_codes(hermes):
    """⑤ 사유 코드가 갈리면 한쪽 로그로 다른 쪽을 설명할 수 없다."""
    assert hermes["reasons"] == {
        "skip": rec.SKIP, "records_unreadable": rec.RECORDS_UNREADABLE,
        "no_match": rec.NO_MATCH, "not_final": rec.NOT_FINAL,
        "source_mismatch": rec.SOURCE_MISMATCH, "evidence_changed": rec.EVIDENCE_CHANGED,
        "no_coordinate": rec.NO_COORDINATE, "conflict": rec.CONFLICT,
    }
    assert hermes["final_states"] == sorted(rec.FINAL_STATES)


@needs_scripts
def test_both_sides_put_the_file_in_the_same_place(hermes):
    """⑥ 자리가 다르면 서로의 파일을 아예 못 본다."""
    assert hermes["path"] == rec.export_path(
        "/root", source=rec.SOURCE_HERMES_INBOX, workspace="TEC").as_posix()


@needs_scripts
def test_a_hermes_written_file_is_readable_by_tybot(tmp_path):
    """⑦ 실제 왕복. 모양만 같고 **읽히지 않으면** 아무 뜻이 없다."""
    path = rec.export_path(tmp_path, source=rec.SOURCE_HERMES_INBOX, workspace="TEC")
    code = (
        _PATH_LINE
        + "import decision_export as X\n"
        + f"X.write(X.export_path({str(tmp_path)!r}, workspace='TEC'), [{{"
        "'candidate_id':'자금|summary|수치|집행액','workspace':'TEC','channel_id':'C1',"
        "'evidence_locator':'slack-export/channels/자금.md#L10-L12',"
        "'evidence_hash':'deadbeef','evidence_message_ts':'','kind':'summary',"
        "'state':'approved','generation':1,'decided_at':'2026-10-05T09:00:00+09:00',"
        "'decided_by':'','source':X.SOURCE}])\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", cwd=str(ROOT))
    if out.returncode != 0:
        pytest.skip(f"Hermes 쪽 쓰기를 못 했다: {out.stderr.strip().splitlines()[-1:]}")

    rows = rec.load_decisions(path)
    assert rows is not None, "TYBot 이 Hermes 가 쓴 파일을 못 읽는다"
    assert len(rows) == 1
    assert rows[0].state == "approved"
    assert rows[0].usable


@needs_scripts
def test_a_tybot_written_file_is_readable_by_hermes(tmp_path):
    """⑧ 반대 방향도 본다. 한쪽만 읽히면 생략은 한쪽으로만 일어난다."""
    path = rec.export_path(tmp_path, source=rec.SOURCE_TYBOT_DM, workspace="TEC")
    rec.write_export(path, [{
        "id": "c1", "workspace": "TEC", "channel_id": "C1",
        "evidence_locator": "2026-10-01.md:12", "evidence_hash": "abc", "state": "rejected",
    }])
    code = (
        f"import json, sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import decision_export as X\n"
        f"print(json.dumps(X.load_decisions({str(path)!r})))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", cwd=str(ROOT))
    if out.returncode != 0:
        pytest.skip(f"Hermes 쪽 읽기를 못 했다: {out.stderr.strip().splitlines()[-1:]}")
    rows = json.loads(out.stdout)
    assert rows is not None and len(rows) == 1
    assert rows[0]["state"] == "rejected"


# --- Hermes 쪽 실제 호출부 ----------------------------------------------------

@needs_scripts
@pytest.mark.parametrize("funnel", ["def approve(", "def drop(", "def later(", "def clear("])
def test_every_hermes_decision_funnel_exports(funnel):
    """⑨ 정하는 문이 넷이다. 한 문이라도 안 내보내면 그 결정은 상대편에 안 보인다.

    `clear`(취소)도 내보낸다 — 안 내보내면 **풀린 결정이 상대편 파일에 남아**
    그쪽이 이미 풀린 결정으로 후보를 생략한다.
    """
    body = (SCRIPTS / "decide_work.py").read_text(encoding="utf-8")
    start = body.index(funnel)
    end = body.find("\ndef ", start + 1)
    chunk = body[start : end if end > start else len(body)]
    assert "export_now()" in chunk, f"{funnel} 에서 결정을 내보내지 않습니다."


@needs_scripts
@pytest.mark.parametrize("funnel", ["def approve(", "def drop(", "def later("])
def test_every_hermes_decision_records_the_coordinate(funnel):
    """⑩ 좌표는 **정하는 순간** 적는다. 나중에 재면 그 뒤 바뀐 원문의 지문이 나온다."""
    body = (SCRIPTS / "decide_work.py").read_text(encoding="utf-8")
    start = body.index(funnel)
    end = body.find("\ndef ", start + 1)
    chunk = body[start : end if end > start else len(body)]
    assert "coords(it, state)" in chunk, f"{funnel} 이 원문 좌표를 안 적습니다."
    assert '"state":' in chunk, f"{funnel} 이 공통 상태 이름을 안 적습니다."


@needs_scripts
def test_the_item_list_consults_the_counterpart():
    """⑪ 읽는 쪽이 없으면 상대편이 끝낸 검토를 또 묻는다."""
    body = (SCRIPTS / "review_work.py").read_text(encoding="utf-8")
    start = body.index("def load_items(")
    end = body.find("\ndef ", start + 1)
    assert "_settled_elsewhere(" in body[start:end]


@needs_scripts
def test_the_block_finder_lives_in_one_place():
    """⑫ 화면과 기록이 **같은 블록**을 봐야 한다.

    따로 찾으면 사람이 본 원문과 좌표가 다른 메시지를 가리킬 수 있고, 그러면 승인한
    것과 기록이 가리키는 것이 달라져 대조의 전제가 무너진다.
    """
    review = (SCRIPTS / "review_work.py").read_text(encoding="utf-8")
    export = (SCRIPTS / "decision_export.py").read_text(encoding="utf-8")
    assert "def evidence_block(" in review
    # `context_for`(화면)도 그 함수를 쓴다 — 자기 규칙으로 다시 찾지 않는다.
    start = review.index("def context_for(")
    assert "evidence_block(md, evidence)" in review[start : start + 4000]
    assert "R.evidence_block(" in export
    assert "QUOTE_RE" not in export, "내보내기 쪽이 인용 찾기를 다시 구현했습니다."


# --- Hermes 쪽 자체 시험을 CI 에 들인다 --------------------------------------

@needs_scripts
def test_the_hermes_side_suite_passes():
    """⑬ 스킬 쪽 시험은 자기 러너로 돈다. pytest 가 그걸 **불러 준다.**

    안 부르면 그 파일은 사람이 생각났을 때만 돌고, 생각나지 않으면 영영 안 돈다.
    """
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "test_decision_export.py")],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(ROOT / "subbots" / "hermes"),
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "전부 통과" in out.stdout
