"""Explicit or long-form answers use Canvas; messages use Slack mrkdwn."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("tybot.canvas_answer")

# 예전 고정 제목. **새로 만들지 않는다** — 제목이 전부 같으면 Slack 목록에서
# 어느 문서인지 구별할 수 없었다. 수집 제외 판정에는 계속 쓴다(옛 문서가 남아 있다).
TITLE = "TYBot 정식 답변"
AUTO_CANVAS_LINES = 25
#: Canvas 를 기본으로 쓰는 봇(VIPBot)의 자동 전환 기준. 임원 보고는 짧아도 문서로
#: 받는 편이 읽기 쉽다는 오너 결정(2026-11-19)이다.
PREFERRED_CANVAS_LINES = 8

# 본문 **첫 블록**. 제목 자리가 아니라 여기에 둔다 — Slack 은 Canvas 제목을 따로
# 보여 주므로 본문 H1 은 같은 제목을 두 번 보여 줄 뿐이었다.
#
# 이 문장은 **생성 문서 표식**이기도 하다. 제목이 질문마다 달라지면 제목만으로는
# 우리 문서를 못 알아본다 — 그 순간 봇 답변이 다시 근거로 수집된다(원칙 1).
DISCLAIMER = (
    "> 이 문서는 TYBot이 요청 시점에 확인 가능한 사내 자료를 바탕으로 생성한 AI 문서입니다.\n"
    "> 중요한 일정·금액·의사결정은 아래 출처 원문을 확인하세요."
)
# 수집 경로가 본문에서 찾을 조각. 문장 전체를 비교하면 줄바꿈 한 번에 어긋난다.
DISCLAIMER_MARK = "TYBot이 요청 시점에 확인 가능한 사내 자료를 바탕으로 생성한 AI 문서"
# 제목 접미사. **한 곳에서만 정의한다** — 만드는 쪽과 제외하는 쪽이 다른 값을
# 들고 있으면 우리가 만든 문서를 우리가 다시 수집한다(원칙 1).
TITLE_SUFFIX = " · TYBot"
VIP_TITLE_SUFFIX = " · VIPBot"
TITLE_SUFFIXES = (TITLE_SUFFIX, VIP_TITLE_SUFFIX)
DISCLAIMER_MARKS = (
    DISCLAIMER_MARK,
    "VIPBot이 요청 시점에 확인 가능한 사내 자료를 바탕으로 생성한 AI 문서",
)


def display_name(bot_name: str = "tybot") -> str:
    """Return a known product name instead of accepting arbitrary title text."""
    return "VIPBot" if (bot_name or "").strip().lower() == "vipbot" else "TYBot"


def title_suffix(bot_name: str = "tybot") -> str:
    return f" · {display_name(bot_name)}"


def disclaimer(bot_name: str = "tybot") -> str:
    bot = display_name(bot_name)
    return (
        f"> 이 문서는 {bot}이 요청 시점에 확인 가능한 사내 자료를 바탕으로 생성한 AI 문서입니다.\n"
        "> 중요한 일정·금액·의사결정은 아래 출처 원문을 확인하세요."
    )


# 「표로」 를 원한다는 표현. `대표로`·`발표를` 처럼 앞 글자가 붙은 낱말은 뺀다.
TABLE_REQUEST_RE = re.compile(
    r"(?<![가-힣])표\s*(?:로|를|을|와|과|랑|\s*형태|\s*형식)|테이블|\btable\b",
    re.IGNORECASE,
)
# 「자세히·문서로」 받고 싶다는 표현. Canvas 를 기본으로 쓰는 봇에서만 본다.
DETAIL_REQUEST_RE = re.compile(
    r"상세|자세히|자세하게|정리|순위|top\s*\d+|보고서|리포트|report|비교|목록|일람",
    re.IGNORECASE,
)
# 사용자가 Canvas 를 **원하지 않는다**고 말한 경우. 이것이 모든 규칙보다 세다.
OPT_OUT_RE = re.compile(
    r"(메시지로|메시지에|캔버스\s*(말고|쓰지|사용하지)|canvas\s*(말고|없이))",
    re.IGNORECASE,
)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def has_markdown_table(body: str) -> bool:
    """본문에 Markdown 표가 있나. 메시지로 보내면 글머리표로 풀려 표가 사라진다."""
    lines = (body or "").splitlines()
    return any(
        _TABLE_ROW_RE.match(lines[i]) and _TABLE_RULE_RE.match(lines[i + 1])
        for i in range(len(lines) - 1)
    )


def automatic(
    body: str,
    request: str,
    *,
    prefer_canvas: bool = False,
    avoid_canvas: bool = False,
    table_preferred: bool = False,
) -> bool:
    """명시 요청이 없어도 Canvas 로 보낼지.

    - 사용자가 「메시지로」·「캔버스 말고」 라고 했으면 언제나 메시지다
    - **표를 원했고 답에 표가 있으면** 모든 봇이 Canvas 로 보낸다. 메시지는 표를
      글머리표로 풀어 버려(`evidence_view.fix_markdown_tables`), 요청한 표가 없어진다
    - `prefer_canvas`(VIPBot) 는 상세·순위·보고 요청이거나 답이 조금만 길어도 Canvas
    - 그 밖에는 답이 길 때만(`AUTO_CANVAS_LINES`)

    `avoid_canvas`·`table_preferred` 는 본인이 남긴 형식 선호(B-91)다. 이번 요청에
    명시한 말이 없을 때만 기본값을 바꾼다.
    """
    request = request or ""
    if OPT_OUT_RE.search(request) or avoid_canvas:
        return False
    table = has_markdown_table(body)
    if table and (TABLE_REQUEST_RE.search(request) or table_preferred):
        return True
    lines = answer_line_count(body)
    if prefer_canvas:
        if table or TABLE_REQUEST_RE.search(request) or DETAIL_REQUEST_RE.search(request):
            return True
        return lines > PREFERRED_CANVAS_LINES
    return lines > AUTO_CANVAS_LINES


def answer_line_count(body: str) -> int:
    """출처 블록을 제외한 실제 답변 줄 수."""
    count = 0
    in_sources = False
    for raw in (body or "").splitlines():
        line = raw.strip()
        # 복합 질문의 다음 답변이 시작된다. 각 하위 답변의 출처를 따로 제외한다.
        if line == "───":
            in_sources = False
            continue
        if line == "출처:":
            in_sources = True
            continue
        if not line or in_sources:
            continue
        count += 1
    return count


def message(body: str) -> str:
    """Normalize Markdown prose without rewriting fenced or inline code."""
    from .evidence_view import fix_markdown_tables

    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", body)
    for index in range(0, len(parts), 2):
        text = parts[index]
        if "|" in text:
            leading = text[:len(text) - len(text.lstrip())]
            trailing = text[len(text.rstrip()):]
            text = leading + fix_markdown_tables(text).strip() + trailing
        text = re.sub(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*$", r"*\1*", text)
        text = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", text)
        parts[index] = text
    return "".join(parts)
# 명시적인 Canvas 지시. 예전에는 「캔버스로 답변/작성」 한 모양만 잡아서
# 「canvas와 표를 써서 정리해줘」·「Canvas로 정리」·「캔버스에 만들어줘」 가 전부
# 새고, 그 뒤 판단이 LLM 의 「확신 없으면 message」 에 맡겨졌다(2026-11-19 VIPBot).
#
# 모양: (캔버스|canvas) + 조사 + [표/문서/양식 + 조사] + 만드는 동사(어미까지).
# 「캔버스 내용도 읽어?」 처럼 **읽는** 동사는 넣지 않는다 — 그건 근거 범위 질문이다.
_CANVAS_VERB = r"(?:써|쓰|사용|작성|정리|만들|보여|답변|답해|답하|올려|그려|담아|넣어|옮겨)"
REQUEST_RE = re.compile(
    r"(?:(?:캔버스|canvas)\s*(?:으로|로|에다|에|와|과|이랑|랑|를|을)?\s*"
    r"(?:(?:표|문서|양식|형식|형태)\s*(?:으로|로|를|을|와|과|랑)?\s*)?"
    + _CANVAS_VERB + r"[가-힣]*|"
    r"메시지\s*말고\s*정식\s*답변(?:해\s*줘|해주세요|해줘|해)?|"
    r"양식으로\s*답변(?:해\s*줘|해주세요|해줘|해)?)",
    re.IGNORECASE,
)
# 지시 **바로 뒤**가 이런 말이면 지시가 아니다. 「canvas 사용해 답변하라는데 왜
# 안 해?」 는 불만이고, 「캔버스 쓰는 법 알려줘」 는 사용법 질문이다. 둘 다 Canvas
# 를 만들 일이 아니고, 지시로 읽어 문장에서 지우면 그 질문의 뜻이 망가진다.
_NOT_A_REQUEST_TAIL_RE = re.compile(
    r"^\s*(?:법|방법)(?![가-힣])|"
    r"^.{0,12}?(?:라는데|라고\s*했|라니까|랬는데|했는데|하는데|왜|이유|"
    r"안\s*(?:해|하|돼|되|써)|못\s*(?:해|하)|는\s*법|는\s*방법|어떻게|"
    r"할\s*수|가능|되나|되니|되냐|돼\?)"
)
SLACK_LINK_RE = re.compile(r"<(?P<url>https?://[^>|]+)\|(?P<label>[^>]+)>")
TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
MAX_TABLE_CELLS = 300


@dataclass(frozen=True)
class CanvasResult:
    canvas_id: str
    permalink: str


def parse_request(text: str) -> tuple[bool, str]:
    """명시적 Canvas 지시를 제거해 검색어를 오염시키지 않는다.

    불만·사용법 질문 안의 「canvas 사용해」 는 지시가 아니다 — 지우지도 않는다.
    """
    source = text or ""
    kept: list[str] = []
    matched = False
    cursor = 0
    for found in REQUEST_RE.finditer(source):
        if _NOT_A_REQUEST_TAIL_RE.match(source[found.end():]):
            continue
        matched = True
        kept.append(source[cursor:found.start()])
        kept.append(" ")
        cursor = found.end()
    kept.append(source[cursor:])
    cleaned = re.sub(r"\s+", " ", "".join(kept)).strip(" ,.!?\t\r\n")
    return matched, cleaned


def _question_block(question: str) -> str:
    cleaned = " ".join((question or "").split()).strip()
    if not cleaned:
        return ""
    # 질문은 표시용이다. 질문 안의 문법이 Canvas 구조를 바꾸지 못하게 한다.
    escaped = re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", cleaned[:2000])
    return f"## 요청한 질문\n> {escaped}"


def markdown(
    body: str, *, bot_name: str = "tybot", question: str = "", flat_lists: bool = False
) -> str:
    """Slack mrkdwn을 Canvas용 Markdown으로 바꾸고 답변·근거 구조를 보존한다.

    **본문에 H1 제목을 넣지 않는다.** Slack 이 Canvas 제목을 따로 보여 주므로
    예전 형식은 같은 제목을 두 번 보여 줬다. 대신 첫 블록에 Disclaimer 를 둔다.
    """
    converted = SLACK_LINK_RE.sub(r"[\g<label>](\g<url>)", body.strip())
    converted = re.sub(r"(?m)^•\s+", "- ", converted)
    converted = re.sub(r"(?m)^\*([^*\n]+)\*:?\s*$", r"## \1", converted)
    converted = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"**\1**", converted)
    converted = _split_large_tables(converted)
    converted = _flatten_lists(converted) if flat_lists else _unmix_nested_lists(converted)
    blocks = [disclaimer(bot_name)]
    if asked := _question_block(question):
        blocks.append(asked)
    blocks.append(converted)
    return "\n\n".join(blocks) + "\n"


# --- Canvas 목록 ---------------------------------------------------------------
#
# Slack Canvas 는 **목록 안에 다른 종류의 목록**을 받지 않는다. 번호 목록 아래
# 글머리표가 오면 `canvas_creation_failed: Unsupported list type (bullet) within
# numbered list` 로 문서 생성 자체가 실패한다(2026-11-20 VIPBot 운영). 모델은 이
# 모양(「1. 사안 / - 세부」)을 자주 쓰므로 보내기 전에 맞춘다.
LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d{1,3}[.)])\s+(?P<text>.*)$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _list_kind(marker: str) -> str:
    return "number" if marker[0].isdigit() else "bullet"


def _indent_width(indent: str) -> int:
    return len(indent.replace("\t", "    "))


def _has_mixed_nesting(lines: list[str]) -> bool:
    stack: list[tuple[int, str]] = []
    fenced = False
    for line in lines:
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        item = LIST_ITEM_RE.match(line)
        if not item:
            if line.strip() and not line.startswith((" ", "\t")):
                stack.clear()
            continue
        width = _indent_width(item.group("indent"))
        kind = _list_kind(item.group("marker"))
        while stack and stack[-1][0] >= width:
            stack.pop()
        if stack and stack[-1][1] != kind:
            return True
        stack.append((width, kind))
    return False


def _numbered_as_bullet(line: str) -> str:
    item = LIST_ITEM_RE.match(line)
    if not item or _list_kind(item.group("marker")) != "number":
        return line
    number = item.group("marker").rstrip(".)")
    return f"{item.group('indent')}- **{number}.** {item.group('text')}"


def _unmix_nested_lists(text: str) -> str:
    """섞인 중첩이 있으면 번호 항목을 「- **1.** 내용」 글머리표로 바꾼다.

    번호는 글자로 남아 읽는 사람에게는 같고, 목록 종류는 하나가 된다. 섞인 곳이
    없으면 손대지 않는다 — 평범한 번호 목록은 그대로 번호 목록이다.
    """
    lines = text.split("\n")
    if not _has_mixed_nesting(lines):
        return text
    out, fenced = [], False
    for line in lines:
        if FENCE_RE.match(line):
            fenced = not fenced
        out.append(line if fenced else _numbered_as_bullet(line))
    return "\n".join(out)


def _flatten_lists(text: str) -> str:
    """마지막 수단 — 모든 목록을 한 단계 글머리표로 편다. 들여쓰기는 「· 」 로 남긴다."""
    out, fenced = [], False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        item = None if fenced else LIST_ITEM_RE.match(line)
        if not item:
            out.append(line)
            continue
        depth = _indent_width(item.group("indent")) // 2
        marker = item.group("marker")
        label = f"**{marker.rstrip('.)')}.** " if _list_kind(marker) == "number" else ""
        out.append(f"- {'· ' * depth}{label}{item.group('text')}")
    return "\n".join(out)


def _table_cells(line: str) -> list[str]:
    """Split a conventional Markdown table row, preserving escaped pipes."""
    marker = "\x00TYBOT_PIPE\x00"
    protected = line.strip().replace(r"\|", marker).strip("|")
    return [cell.strip().replace(marker, r"\|") for cell in protected.split("|")]


def _is_table_separator(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(TABLE_SEPARATOR_RE.fullmatch(cell) for cell in cells)


def _split_large_tables(text: str) -> str:
    """Keep each Canvas Markdown table within Slack's 300-cell limit."""
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 >= len(lines) or "|" not in lines[index] or not _is_table_separator(lines[index + 1]):
            out.append(lines[index])
            index += 1
            continue
        header, separator = lines[index], lines[index + 1]
        columns = len(_table_cells(header))
        data: list[str] = []
        cursor = index + 2
        while cursor < len(lines) and "|" in lines[cursor] and lines[cursor].strip():
            data.append(lines[cursor])
            cursor += 1
        # The header counts as one row. Tables wider than the API limit cannot
        # be split by rows without changing their meaning, so Canvas creation
        # will fail and the existing readable-message fallback will be used.
        rows_per_table = (MAX_TABLE_CELLS // columns) - 1 if columns else 0
        if rows_per_table < 1 or (len(data) + 1) * columns <= MAX_TABLE_CELLS:
            out.extend([header, separator, *data])
        else:
            for start in range(0, len(data), rows_per_table):
                if start:
                    out.append("")
                out.extend([header, separator, *data[start:start + rows_per_table]])
        index = cursor
    return "\n".join(out)


def create(
    client,
    body: str,
    *,
    title: str = "",
    bot_name: str = "tybot",
    question: str = "",
    provenance: dict | None = None,
) -> CanvasResult:
    """독립 Canvas를 만들고 링크를 반환한다. 공유 범위는 호출자가 별도로 설정한다.

    `title` 은 이미 검증된 값이다(`master_planner.artifact_for`). 비어 있으면
    예전 고정 제목으로 돌아간다 — **여기서 제목을 지어내지 않는다.**

    `provenance` 는 `{workspace, channel_id, qa_record_id}`. 생성한 Canvas 의
    ID 를 기록해 **다시 수집하지 않게** 한다(원칙 1). 기록에 실패해도 Canvas 는
    만든다 — 제목 접미사와 Disclaimer 가 남은 방어선이다.
    """
    canvas_title = title.strip() or f"{display_name(bot_name)} 정식 답변"
    try:
        response = client.canvases_create(
            title=canvas_title,
            document_content={
                "type": "markdown",
                "markdown": markdown(body, bot_name=bot_name, question=question),
            },
        )
    except Exception as exc:
        # Slack 이 Markdown 구조를 거부했다(`canvas_creation_failed`). 내용은 그대로
        # 두고 목록만 평평하게 펴서 **한 번만** 다시 보낸다. 다른 오류는 그대로 올린다.
        if _slack_error(exc) != "canvas_creation_failed":
            raise
        response = client.canvases_create(
            title=canvas_title,
            document_content={
                "type": "markdown",
                "markdown": markdown(
                    body, bot_name=bot_name, question=question, flat_lists=True
                ),
            },
        )
    canvas_id = str(response.get("canvas_id") or response.get("file_id") or "")
    if not canvas_id:
        raise RuntimeError("Slack canvases.create 응답에 canvas_id가 없습니다")
    info = client.files_info(file=canvas_id)
    permalink = str((info.get("file") or {}).get("permalink") or "")
    if not permalink:
        raise RuntimeError("생성된 Canvas의 permalink를 찾지 못했습니다")
    if provenance:
        remember_generated(canvas_id, **provenance)
    return CanvasResult(canvas_id, permalink)


def _slack_error(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    try:
        return str((response.get("error") if response is not None else "") or "")
    except Exception:  # noqa: BLE001 - 응답 모양을 모르면 코드 없음
        return ""


# --- 생성 Canvas 기록 ---------------------------------------------------------
#
# 동적 제목을 쓰면 `title.startswith("TYBot 정식 답변")` 검사가 더 이상 우리
# 문서를 못 알아본다. 그 검사 하나에 기대고 있던 것이 **원칙 1(요약 재귀 금지)**
# 이라, 제목을 바꾸기 전에 방어선을 셋으로 늘린다.
#
#   1. 제목 접미사 ` · TYBot`
#   2. 본문 첫 블록의 Disclaimer
#   3. 여기 기록한 canvas_id
#
# 셋 중 **하나만 맞아도** 제외한다. 기록은 유실될 수 있고(디스크 오류·재설치),
# 제목은 사람이 고칠 수 있다. 한 겹으로는 못 막는다.
GENERATED_LOG = "generated-canvases.jsonl"
# 한 파일에 무한히 쌓이면 읽는 쪽이 느려진다. 최근 것부터 이만큼만 읽는다.
MAX_GENERATED_ROWS = 20_000


def generated_log_path() -> Path:
    from .heartbeat import state_dir

    return state_dir() / GENERATED_LOG


def remember_generated(
    canvas_id: str, *, workspace: str = "", channel_id: str = "", qa_record_id: str = ""
) -> bool:
    """생성한 Canvas 의 좌표만 남긴다. **제목도 본문도 넣지 않는다.**

    실패해도 예외를 올리지 않는다 — 기록 실패로 답변을 못 보내면 그게 더 나쁘다.
    """
    if not canvas_id:
        return False
    row = {
        "canvas_id": canvas_id,
        "workspace": workspace,
        "channel_id": channel_id,
        "qa_record_id": qa_record_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        path = generated_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        log.warning("생성 Canvas 기록 실패 %s: %s", canvas_id, exc)
        return False


def is_generated(canvas_id: str) -> bool:
    """우리가 만든 Canvas 인가. 기록을 못 읽으면 **False** 다.

    여기서 True 로 기울면 사람이 만든 Canvas 가 수집에서 빠진다 — 자료가 조용히
    사라지는 쪽이 중복 수집보다 나쁘다. 못 읽었을 때의 방어선은 제목과
    Disclaimer 다.
    """
    if not canvas_id:
        return False
    try:
        path = generated_log_path()
        if not path.exists():
            return False
        with path.open(encoding="utf-8") as fh:
            rows = fh.readlines()[-MAX_GENERATED_ROWS:]
    except OSError as exc:
        log.warning("생성 Canvas 기록을 읽지 못했습니다: %s", exc)
        return False
    needle = f'"canvas_id": "{canvas_id}"'
    return any(needle in line for line in rows)


def grant_channel(client, canvas_id: str, channel_id: str) -> None:
    client.canvases_access_set(
        canvas_id=canvas_id, access_level="read", channel_ids=[channel_id]
    )


def grant_user(client, canvas_id: str, user_id: str) -> None:
    client.canvases_access_set(
        canvas_id=canvas_id, access_level="read", user_ids=[user_id]
    )


def grant_users(client, canvas_id: str, user_ids: list[str]) -> None:
    """여러 사람에게 **한 번의 호출로** 읽기 권한을 준다.

    한 사람씩 부르면 중간에 실패했을 때 「일부만 볼 수 있는 문서」 가 남는다 —
    누가 보고 누가 못 보는지 DB 에 흔적도 없다. 한 번에 주고, 실패하면 전부
    실패로 다룬다.

    **채널 공유를 하지 않는다.** 검토 문서는 그 회차 수신자만 본다(설계 §6).
    """
    unique = [uid for uid in dict.fromkeys(user_ids or []) if uid]
    if not unique:
        raise ValueError("권한을 줄 사용자가 없습니다")
    client.canvases_access_set(
        canvas_id=canvas_id, access_level="read", user_ids=unique
    )
