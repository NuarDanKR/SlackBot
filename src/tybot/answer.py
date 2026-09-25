"""질의응답 파이프라인 — 환각방지 4겹을 코드로 강제한다.

순서(바꾸지 말 것): 권한 필터 → 원문 검색 → (0건이면 목록만, LLM 호출 안 함) → LLM → 출처 부착 → 로깅.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import document_evidence, documents, summary_guide
from .access import RequestContext
from .archive.store import ArchiveStore, SearchHit
from .attachment_review import find_sendable, status_line
from .channels import source_label
from .evidence_refs import MAX_REFS, live_ref, refs_from_hits
from .gateway.base import Message, Sensitivity
from .gateway.cost import CostLimitExceeded
from .gateway.router import ModelNotAllowed, Router, UnknownModel
from .intent import (
    DEFAULT_DAYS,
    Intent,
    classify,
    parse_period,
    plan,
)

logger = logging.getLogger("tybot.answer")

SYSTEM_PROMPT = """그럴듯하게 지어내는 것은 모른다고 하는 것보다 나쁘다.

너는 태영건설 사내 아카이브 봇 'TYBot'이다. 아래 <원문> 블록에 실제로 적힌 내용만 근거로 답한다.
규칙:
1. <원문>에 없는 사실은 절대 추가하지 않는다. 일반 상식·추측·외부 지식 금지.
2. 금액·날짜·기관명·사람 이름은 원문 그대로 옮긴다. 반올림·환산·추론 금지.
3. 사람 발언과 문서 내용은 구분해서 쓴다. 숫자가 엇갈리면 두 시점을 함께 표기한다.
   누군가의 평가·의견·판단을 옮길 때는 발언자와 날짜를 함께 적고, 봇 자신의
   평가처럼 바꾸어 쓰지 않는다.
4. <원문>으로 답할 수 없으면 "아카이브에서 근거를 찾지 못했습니다"라고만 답한다.
5. 답변은 한국어, 간결하게. 출처 줄은 시스템이 붙이므로 네가 쓰지 않는다.
6. 출력은 Slack 메시지다. `#` 제목과 `**굵게**`는 Slack에서 글자 그대로 보이니 쓰지 않는다.
   굵게는 별표 하나(*굵게*), 목록은 `• `, 구분선은 쓰지 않는다.
"""

SUMMARY_PROMPT = """그럴듯하게 지어내는 것은 모른다고 하는 것보다 나쁘다.

너는 태영건설 사내 아카이브 봇이다. 아래 <원문>은 여러 채널의 실제 대화 기록이다.
이걸로 진행 상황을 정리한다.
규칙:
1. <원문>에 없는 사실·추측·전망을 절대 추가하지 않는다. 진척률·완료 여부를 임의 판단하지 않는다.
2. 금액·날짜·기관명·사람 이름은 원문 그대로. 반올림·환산 금지.
3. **채널별로 묶어서** 정리한다. 각 항목은 `- 내용 (발언자, 날짜)` 형식.
   특히 평가·의견·판단은 발언자와 날짜를 생략하지 않는다.
4. 결정된 것 / 진행 중 / 미해결·대기 를 구분한다. 원문에서 판단이 안 되면 그 구분을 비운다.
5. 원문이 빈약하면 "이 기간 원문이 N줄뿐이라 정리가 제한적입니다"를 먼저 밝힌다.
6. 한국어, 간결. 출처 줄은 시스템이 붙이므로 쓰지 않는다.
7. 출력은 Slack 메시지다. `#` 제목·`**굵게**`·`---` 구분선은 Slack에서 글자 그대로 보이니 금지.
   채널 이름은 `*#채널명*`, 하위 항목은 `• `, 들여쓰기는 공백 2칸으로만 표현한다.
8. **주관적인 요청도 원문으로 답한다.** "중요한 내용", "핵심만" 같은 요청에
   "판단할 수 없다"고 답하지 않는다. 중요도를 임의로 매기지 말고, 원문에 있는 것을
   그대로 정리해 보인다 — 무엇이 중요한지는 읽는 사람이 정한다.
9. 정리를 거부하는 것은 **원문이 이 기간에 하나도 없을 때뿐**이다. 그때는 5번을 따른다.
"""

ADVICE_PROMPT = """너는 태영건설 사내 Slack 아카이브 봇 'TYBot'이다.
지금은 **사실 조회가 아니라 업무 판단·권고** 요청을 받았다.

지켜야 할 경계:
1. **사내 사실**(금액·날짜·조직·인원·결정사항·현장 상태)은 <원문>에 적힌 것만 말한다.
   원문에 없으면 "아카이브에 근거가 없다"고 밝히고, 절대 추정치나 예시를 사실처럼 쓰지 않는다.
2. **일반 원칙·장단점·권고**는 네 지식으로 제시해도 된다. 단 그것이 일반적 판단임이 드러나게 쓴다.
3. <원문>에 관련 내용이 있으면 그것을 우선 근거로 삼고, 우리 상황에 맞춰 판단한다.
4. 사실과 판단을 섞어 쓰지 않는다. 무엇이 원문 근거이고 무엇이 일반 판단인지 구분된 문장으로.

형식(Slack mrkdwn — `#` 제목, `**굵게**`, `---` 금지. 굵게는 *별표 하나*):
• 결론 한 줄부터 시작한다.
• 선택지가 둘 이상이면 각각 장단점을 2~3개씩. 각 항목은 한 줄.
• 마지막에 *권고*: 어떤 조건이면 어느 쪽인지 명시한다.
• 판단의 전제나 확인이 필요한 사항이 있으면 한 줄로 덧붙인다.
한국어, 간결하게. 출처 줄은 시스템이 붙이므로 쓰지 않는다."""

INTERIM_ADVICE_PROMPT = """너는 TYBot 마스터다. 판단 전문 봇이 도입되기 전까지
사용자의 판단·조언 요청에 **임시 코멘트만** 작성한다.

규칙:
1. 사내 사실은 <원문>에 있는 것만 사용한다. 숫자·날짜·조직·현장 상태를 추측하지 않는다.
2. 원문 사실과 일반적인 판단을 분리한다. 일반 원칙은 반드시 판단 또는 제안으로 표현한다.
3. 사용자가 요청한 판단, 위험, 다음 확인사항에 직접 답한다.
4. 법률·세무·안전·외부 최신정보를 확정적으로 판단하지 않는다. 전문 검토 필요성을 밝힌다.
5. Hermes 답변을 다시 요약하지 않는다. 3~7개의 간결한 bullet로 코멘트만 쓴다.
6. Slack mrkdwn 형식으로 쓰고 출처 줄은 만들지 않는다.

출력은 코멘트 본문만 작성한다."""

MODEL_FLAG_RE = re.compile(r"--model=([A-Za-z0-9._\-]+)")
# 아카이브 범위 자체를 묻는 표현. 분류기가 out_of_scope 로 잘못 보내도 여기서 되돌린다.
ARCHIVE_SCOPE_RE = re.compile(
    r"(워크스페이스|채널|아카이브|자료|문서|기록|대화|수집|공유|본부|팀|현장|프로젝트)"
)
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _mentioned_workspaces(question: str) -> frozenset[str]:
    """질문에 명시된 워크스페이스 키·표시 이름을 DB 메타데이터에서 찾는다."""
    import os

    from .workspaces import env_suffix

    labels: dict[str, str] = {}
    keys = [key.strip() for key in (os.getenv("WORKSPACES") or "").split(",") if key.strip()]
    for key in keys:
        labels[key] = (os.getenv(f"WORKSPACE_LABEL_{env_suffix(key)}") or key).strip()
    try:
        from .console.workspace_store import list_workspaces

        db_rows = list_workspaces()
        if db_rows:
            labels = {
                str(row["key"]): str(row.get("label") or row["key"])
                for row in db_rows
                if row.get("state") != "disabled"
            }
    except Exception:  # noqa: BLE001 - environment metadata remains the emergency fallback
        pass

    found: set[str] = set()
    for key, label in labels.items():
        if label and label in question:
            found.add(key)
            continue
        if re.search(rf"(?<![A-Za-z0-9-]){re.escape(key)}(?![A-Za-z0-9-])", question, re.I):
            found.add(key)
    return frozenset(found)


def _euro(word: str) -> str:
    """받침에 맞는 조사(으로/로). 한 글자 차이지만 매 답변에 보이는 문구다."""
    ch = (word or "").strip()[-1:]
    if not ch or not ("가" <= ch <= "힣"):
        return "로"
    jong = (ord(ch) - 0xAC00) % 28
    # 받침이 없거나 ㄹ 이면 '로'.
    return "로" if jong in (0, 8) else "으로"


@dataclass
class Answer:
    text: str
    citations: list[str]
    model: str | None
    cost_usd: float
    hit_count: int
    reason: str  # answered | advice | no_hits | no_access | smalltalk | out_of_scope | error
    # 사용자에게 보여줄 근거 요약용 검색어. 새로 저장하는 값이 아니라 이미 쓴 값이다.
    terms: list[str] = field(default_factory=list)
    # 근거에 언급됐지만 자동 변환하지 못한 첨부.
    withheld: list[str] = field(default_factory=list)
    # 변환은 됐는데 **일부만** 읽은 첨부. `이름 (확인 3/10쪽)` 모양이다.
    # 실패(`withheld`)와 다르다 — 이쪽은 답이 나가므로 범위를 밝히지 않으면
    # 사람이 전부 본 줄 안다.
    partial_attachments: list[str] = field(default_factory=list)
    # 어느 전문가가 문장을 만들었나. 비면 마스터다.
    #
    # **모델명으로는 구별할 수 없다.** 전문가에게 지정한 모델이 마스터 기본 모델과
    # 같으면 화면에 같은 이름이 뜨고, 그러면 「누가 답했나」 를 물어도 알 수 없다
    # (2026-09-07 실제로 그랬다). 값은 이미 가지고 있었고 표시만 안 했다.
    specialist: str = ""
    # --- 후속 질문이 이어 갈 것 (설계: thread-follow-up-evidence.md §5.3) -----
    #
    # **다음 질문은 이 좌표를 다시 열어 읽는다.** 답변 문장을 이어 가면 요약을
    # 근거로 요약하게 되고(원칙 1), 한 번 잘못 읽은 숫자가 대화 내내 사실로 굳는다.
    evidence_refs: list = field(default_factory=list)
    attachment_refs: list = field(default_factory=list)
    subject_terms: list[str] = field(default_factory=list)
    context_parent_ids: list[str] = field(default_factory=list)
    # 이 답의 **범위를 무엇으로 정했나**.
    #   none                 - 새 질문. 이번 검색으로 범위를 정했다
    #   transmitted_evidence - 모델에 실제 전달한 근거를 좌표로 남겼다
    #   prior_turn/topic/attachments - 이전 결과의 좌표를 다시 열어 좁혔다
    context_resolution: str = "none"
    # --- 추적 (설계: master-specialist-orchestration.md §7) -------------------
    #
    # **업무 답변의 최종 주체를 값으로 남긴다.** 예전에는 `specialist` 가 비어
    # 있으면 마스터가 답한 것이었는데, 그게 정상인지 고장인지 구별할 값이 없었다.
    # 지금은 업무 답변에서 비어 있다는 것 자체가 정책 위반이고, 왜 그랬는지는
    # `specialist_error_code` 와 `attempted_specialists` 가 말한다.
    #
    # 질문·근거 본문은 여기 넣지 않는다. 코드와 이름만이다.
    specialist_error_code: str = ""
    attempted_specialists: list[str] = field(default_factory=list)
    required_capability: str = ""
    format_retry_count: int = 0
    guardrail_result: str = ""
    # 판단 전문 봇 도입 전 마스터가 같은 원문 범위로 조언만 덧붙였는가.
    # 감사 기록의 최종 응답 주체를 `master-interim` 으로 남기는 표식이다.
    master_interim: bool = False

    @property
    def doc_count(self) -> int:
        return len(dict.fromkeys(self.citations))

    def evidence_note(self) -> str:
        """무엇으로 검색해 몇 건 중 몇 줄을 썼는지 한 줄.

        사내 피드백: "봇이 어떤 과정을 거쳐 이 답을 냈는지 알 수 없다." 값은 이미
        전부 가지고 있었고 **표시만 하지 않았다.** 이 줄이 "믿을 수 있나" 를
        "이 답이 맞나" 로 바꾼다 — 후자는 사람이 검증할 수 있는 질문이다.
        """
        if self.reason not in ("answered", "advice"):
            return ""
        bits: list[str] = []
        if self.terms:
            query = " ".join(self.terms)
            bits.append(f"「{query}」{_euro(query)} 검색")
        if self.doc_count:
            bits.append(f"문서 {self.doc_count}건")
        if self.hit_count:
            bits.append(f"원문 {self.hit_count}줄 사용")
        elif self.reason == "advice":
            # 근거가 0건인 판단은 그 사실이 가장 중요한 정보다.
            bits.append("아카이브 근거 없음")
        if self.model:
            bits.append(self.model)
        if self.specialist:
            bits.append(f"{self.specialist} 전문봇")
        if self.withheld:
            names = ", ".join(self.withheld[:3])
            more = f" 외 {len(self.withheld) - 3}건" if len(self.withheld) > 3 else ""
            bits.append(f"자동 변환 실패로 내용을 읽지 못한 첨부: {names}{more}")
        if self.partial_attachments:
            # **변환은 됐는데 다 읽지는 못한 첨부.** 실패와 다르다 — 본문이 있어서
            # 답이 나가고, 그 답에 우리 출처가 붙는다. 어디까지 읽었는지 안 밝히면
            # 사람은 전부 본 줄 안다.
            shown = ", ".join(self.partial_attachments[:3])
            more = (
                f" 외 {len(self.partial_attachments) - 3}건"
                if len(self.partial_attachments) > 3
                else ""
            )
            bits.append(f"일부만 읽은 첨부: {shown}{more}")
        return f"_근거: {' · '.join(bits)}_" if bits else ""

    def to_slack(self, *, preserve_markdown: bool = False) -> str:
        # Slack 에는 표 문법이 없다. 모델이 마크다운 표를 뱉으면 파이프가 그대로 보이고
        # 열이 어긋난다 - 프롬프트로 금지해도 새는 경우가 있어 여기서 다시 그린다.
        from .evidence_view import fix_markdown_tables

        parts = [self.text if preserve_markdown else fix_markdown_tables(self.text)]
        note = self.evidence_note()
        if note:
            parts.append(note)
        if self.citations:
            srcs = "\n".join(
                f"• {_display_citation(c)}" for c in dict.fromkeys(self.citations)
            )
            parts.append(f"출처:\n{srcs}")
        return "\n\n".join(parts)


ARCHIVE_MD_CITATION_RE = re.compile(
    r"^(?P<channel>.*?),\s*📄[^,]+?\.md(?:\((?P<date>\d{4}-\d{2}-\d{2})\))?$"
)


def _display_citation(citation: str) -> str:
    """Slack에는 내부 아카이브 MD 파일명 대신 채널과 대화 날짜를 표시한다."""
    match = ARCHIVE_MD_CITATION_RE.match(citation.strip())
    if not match:
        return citation
    date = match.group("date")
    return f"{match.group('channel')} ({date})" if date else match.group("channel")


def parse_model_flag(text: str) -> tuple[str | None, str]:
    """`--model=xxx 질문...` → (모델, 질문)."""
    m = MODEL_FLAG_RE.search(text)
    if not m:
        return None, text.strip()
    return m.group(1), (text[: m.start()] + text[m.end() :]).strip()


# 원문 줄에 남는 첨부 표시. Slack 원본 링크가 있으면 함께 보존한다.
ATTACHMENT_RE = re.compile(
    r"^\[첨부:[^\]]*\]\s*(?P<name>.+?)\s*\([^)]*\)"
    r"(?:\s*·\s*<(?P<url>[^>|]+)\|[^>]+>)?\s*$"
)
EXTRACTED_ATTACHMENT_RE = re.compile(r"^\[첨부(?:본문|추출):(?P<name>[^\]]+)\]")


def _attachment_names(hits: list[SearchHit]) -> list[tuple[str, str, str]]:
    """검색에 걸린 줄에서 (워크스페이스, 채널ID, 파일명)을 뽑는다.

    답변 근거로 이미 고른 문서에서 자동 변환하지 못한 파일을 식별할 때 사용한다.
    """
    out: list[tuple[str, str, str]] = []
    for h in hits:
        m = ATTACHMENT_RE.match((h.line.text or "").strip())
        if not m:
            extracted = EXTRACTED_ATTACHMENT_RE.match((h.line.text or "").strip())
            if not extracted:
                continue
            name = extracted.group("name")
            m = next(
                (
                    marker
                    for line in h.doc.raw_lines
                    if (marker := ATTACHMENT_RE.match((line.text or "").strip()))
                    and marker.group("name") == name
                ),
                None,
            )
            if not m:
                continue
        channel_id = h.doc.channel_id or ""
        if not channel_id:
            continue
        key = (h.doc.workspace, channel_id, m.group("name"))
        if key not in out:
            out.append(key)
    return out


def _attachment_source_links(hits: list[SearchHit]) -> list[str]:
    """추출문을 근거로 쓴 답변에 같은 문서의 Slack 원본 링크를 붙인다."""
    links: list[str] = []
    for hit in hits:
        hit_text = (hit.line.text or "").strip()
        direct = ATTACHMENT_RE.match(hit_text)
        if direct and direct.group("url"):
            citation = f"📎<{direct.group('url')}|{direct.group('name')} 원본>"
            if citation not in links:
                links.append(citation)
            continue
        extracted = EXTRACTED_ATTACHMENT_RE.match(hit_text)
        if not extracted:
            continue
        name = extracted.group("name")
        for line in hit.doc.raw_lines:
            marker = ATTACHMENT_RE.match((line.text or "").strip())
            if marker and marker.group("name") == name and marker.group("url"):
                citation = f"📎<{marker.group('url')}|{name} 원본>"
                if citation not in links:
                    links.append(citation)
                break
    return links


class _Outcome:
    """훅 결과를 한 모양으로 읽는다.

    새 훅(`specialist_router.serve`)은 `SpecialistOutcome` 을 준다. 옛 훅은 답
    하나 또는 `None` 을 준다. **`None` 은 「마스터가 답하라」 가 아니다** — 전문
    봇이 못 답한 것이고, 그러면 우리는 못 답한다고 말한다(설계 §5.2).
    """

    __slots__ = ("_attempted", "answer", "clarification", "error_code", "status")

    def __init__(self, status, answer=None, error_code="", clarification="", attempted=()):
        self.status = status
        self.answer = answer
        self.error_code = error_code
        self.clarification = clarification
        self._attempted = tuple(attempted or ())

    @property
    def ok(self) -> bool:
        return self.status == "success" and self.answer is not None

    @property
    def attempted(self) -> list:
        return list(self._attempted)

    @classmethod
    def read(cls, value) -> _Outcome:
        if value is None:
            return cls("unavailable", error_code="specialist-no-answer")
        status = getattr(value, "status", None)
        if status is not None:
            return cls(
                str(status),
                getattr(value, "answer", None),
                str(getattr(value, "error_code", "") or ""),
                str(getattr(value, "clarification", "") or ""),
                tuple(getattr(value, "attempted", ()) or ()),
            )
        # 옛 훅: 답 객체를 그대로 준다.
        if str(getattr(value, "text", "") or "").strip():
            return cls("success", value)
        return cls("unavailable", error_code="empty-output")


# 전문 봇이 못 답했을 때 **코드가** 내는 문구. 모델을 부르지 않는다.
#
# 예전에는 이 자리에서 마스터 LLM 이 같은 근거로 직접 답했다. 그래서 전문 봇이
# 꺼져 있어도, 시간이 초과돼도, 계약을 어겨도 사용자 눈에는 정상 답이 나갔고
# **아무도 고장을 몰랐다**(2026-09-13 검증). 답이 나가지 않는 편이 낫다 —
# 사람이 고칠 수 있는 상태가 되기 때문이다.
UNAVAILABLE_HEAD = "요청한 문서 답변을 생성할 수 없습니다."


def unavailable_text(outcome, *, stage: str = "전문 봇 호출") -> str:
    code = outcome.error_code or outcome.status or "unknown"
    return (
        f"{UNAVAILABLE_HEAD}\n"
        f"처리 단계: {stage}\n"
        f"사유 코드: {code}\n"
        "원문과 첨부는 변경되지 않았습니다."
    )


def _specialist_documents_ok(special, ctx, store) -> bool:
    """전문가가 근거로 든 문서가 **지금 이 요청자에게 보이는 것**인가.

    설계 §12. 도구를 쓰는 전문가는 마스터가 고른 것과 다른 문서를 열 수 있고, 그
    자체는 정상이다 — 도구가 우리 `RequestContext` 를 통과하기 때문이다. 그래도
    돌아온 목록을 한 번 더 대조하는 이유는, **그 통과를 우리가 확인할 수 있는
    유일한 지점**이 여기이기 때문이다. 어느 날 도구가 바뀌어 권한을 건너뛰면
    오류는 나지 않고, 보이면 안 되는 내용에 우리 출처가 붙어 나간다.

    하나라도 어긋나면 계약 위반으로 보고 마스터로 폴백한다. 일부만 빼면 답은
    남고 근거만 빠져서, 답변과 출처가 어긋난 채로 나간다.
    """
    docs = list(getattr(special, "documents", ()) or ())
    if not docs:
        return True
    try:
        allowed = {
            (d.workspace, str(d.path)) for d in store.visible_docs(ctx)
        }
    except Exception as exc:  # noqa: BLE001 - 검사 실패는 통과가 아니라 폴백이다
        logger.warning("전문가 출처 대조 실패: %s", exc)
        return False
    for doc in docs:
        if (getattr(doc, "workspace", ""), str(getattr(doc, "path", ""))) not in allowed:
            logger.error(
                "전문가 계약 위반 — 권한 밖 출처 specialist=%s ws=%s user=%s",
                getattr(special, "specialist", "?"),
                ctx.workspace,
                ctx.role,
            )
            return False
    return True


def _specialist_citations(special, hits: list[SearchHit], ctx) -> list[str]:
    """전문가 답에 붙일 출처.

    **전문가가 직접 읽었으면 그것으로 만든다.** 도구를 쓰는 전문가는 마스터가
    고른 것과 다른 문서를 열 수 있는데, 그때 마스터 검색 결과로 출처를 붙이면
    답과 출처가 어긋난다 — 사람이 확인하러 갔다가 그 내용을 못 찾고, 그 순간
    출처는 신뢰를 만드는 것이 아니라 깎는다.

    실시간으로 읽은 대화는 **Slack 링크**로 붙는다(2026-09-11 원칙 개정).
    아카이브 문서로 붙이면 그 문서에는 아직 없는 내용이다.
    """
    documents = getattr(special, "documents", ()) or ()
    if documents:
        out = []
        for doc in documents[:5]:
            tail = f" ({doc.workspace})" if doc.workspace != ctx.workspace else ""
            out.append(f"{source_label(doc.channel)}{tail}, 📄{doc.path.name}")
    else:
        out = [
            h.citation(with_workspace=h.doc.workspace != ctx.workspace)
            for h in hits[:5]
        ]
    for link in (getattr(special, "live_links", ()) or ())[:3]:
        out.append(f"🔴실시간 <{link}|Slack 원문>")
    return out


def _specialist_evidence_refs(special, hits: list[SearchHit], root) -> list:
    """전문 봇이 실제로 본 좌표와 seed 좌표를 한 목록으로 만든다.

    도구형 전문가는 마스터의 최초 검색 밖에서 문서를 열 수 있다. 출처는 그
    문서로 바꾸면서 `근거 보기`만 최초 검색 좌표를 저장하면 서로 다른 자료를
    가리킨다. 실제 도구 결과를 먼저 두고, seed는 뒤에서 보완한다.
    """
    touched = list(getattr(special, "evidence_hits", ()) or ())
    refs = refs_from_hits([*touched, *(hits or ())], root, limit=MAX_REFS)
    for workspace, channel_id, message_ts in (
        getattr(special, "live_messages", ()) or ()
    ):
        ref = live_ref(workspace, channel_id, message_ts)
        if ref is not None and ref not in refs:
            refs.append(ref)
        if len(refs) >= MAX_REFS:
            break
    return refs


def _extracted_names(hits: list[SearchHit]) -> set[str]:
    """근거 문서에 **변환본이 들어간** 첨부 이름.

    변환본이 아카이브에 있으면 그 텍스트를 근거로 사용한다. 원본 바이트는 변환
    성공 여부와 관계없이 답변 모델에 보내지 않는다.
    """
    out: set[str] = set()
    for hit in hits:
        for line in hit.doc.raw_lines:
            got = EXTRACTED_ATTACHMENT_RE.match((line.text or "").strip())
            if got:
                out.add(got.group("name"))
    return out


def _partial_attachments(root: Path, hits: list[SearchHit]) -> list[str]:
    """근거에 쓰인 첨부 중 **일부만 읽은 것**. `이름 (확인 3/10쪽)`.

    `_withheld_attachments()` 는 아예 못 읽은 것이고 이쪽은 읽긴 읽은 것이다.
    둘을 같은 줄로 묶으면 「읽었는데 모자란 것」 이 「못 읽은 것」 으로 보이고,
    사용자가 할 일이 달라진다 — 하나는 원본을 열어 보는 것이고 하나는 재변환이다.
    """
    names = _attachment_names(hits)
    if not names:
        return []
    from . import attachment_review

    try:
        staged = attachment_review.scan(root)
    except Exception as exc:  # noqa: BLE001 - 표시 한 줄이 답변을 막지 않는다
        logger.warning("첨부 범위를 읽지 못했습니다: %s", exc)
        return []
    by_key = {(a.workspace, a.channel_id, a.name): a for a in staged}
    out: list[str] = []
    for workspace, channel_id, name in names:
        item = by_key.get((workspace, channel_id, name))
        if item is None or item.conversion_state != "partial":
            continue
        note = item.coverage_note
        label = f"{name} ({note})" if note else name
        if label not in out:
            out.append(label)
    return out


def _withheld_attachments(hits: list[SearchHit]) -> list[str]:
    """근거에 언급됐지만 자동 변환 텍스트가 없는 첨부 이름.

    원본은 외부 LLM에 보내지 않는다. 자동 변환이 실패한 사실과 파일명을 알려
    사용자가 Slack 원본 또는 콘솔 진단에서 확인할 수 있게 한다.
    """
    extracted = _extracted_names(hits)
    out: list[str] = []
    for _workspace, _channel_id, name in _attachment_names(hits):
        if name in extracted:
            continue
        if name not in out:
            out.append(name)
    return out


def _visual_originals(root: Path, hits: list[SearchHit]) -> documents.Attached:
    """권한 필터와 OCR·PII 검사를 통과한 검색 결과의 이미지 원본만 고른다."""
    items = []
    for workspace, channel_id, name in _attachment_names(hits):
        suffix = Path(name).suffix.lstrip(".").lower()
        if suffix not in documents.IMAGE_TYPES:
            continue
        text_extracted = any(
            hit.doc.workspace == workspace
            and hit.doc.channel_id == channel_id
            and any(
                (match := EXTRACTED_ATTACHMENT_RE.match((line.text or "").strip()))
                and match.group("name") == name
                for line in hit.doc.raw_lines
            )
            for hit in hits
        )
        item = find_sendable(
            root,
            workspace=workspace,
            channel_id=channel_id,
            name=name,
            text_extracted=text_extracted,
        )
        if item is not None:
            items.append(item)
    return documents.collect(items)

def _guardrail_result(root: Path, hits: list[SearchHit]) -> str:
    """선택 근거에 연결된 첨부의 가장 강한 PII 판정 결과.

    QA에는 상태 코드만 남긴다. 구형 첨부처럼 판정 metadata가 없거나 파일을
    정확히 하나로 잇지 못하면 빈 값으로 두어, 모르는 상태를 ``passed``로
    만들지 않는다.
    """
    names = set(_attachment_names(hits))
    if not names:
        return ""
    from . import attachment_review

    try:
        staged = attachment_review.scan(root)
    except Exception as exc:  # noqa: BLE001 - 추적 실패가 답변을 막으면 안 된다
        logger.warning("첨부 가드레일 결과 조회 실패: %s", exc)
        return ""
    rank = {"passed": 1, "passed_with_notice": 2, "blocked": 3}
    states: list[str] = []
    for coordinates in names:
        matches = [
            item for item in staged
            if (item.workspace, item.channel_id, item.name) == coordinates
        ]
        # 같은 이름으로 여러 파일이 올라온 경우 어느 판정이 이번 근거 것인지
        # 알 수 없다. 가장 강한 값을 임의 선택하지 않고 unknown(빈 값)으로 둔다.
        if len(matches) == 1 and matches[0].screen_result in rank:
            states.append(matches[0].screen_result)
    return max(states, key=rank.get) if states else ""


def _attachment_refs(root: Path, hits: list[SearchHit]):
    """근거 줄에 보이는 첨부를 **staging 메타데이터의 file_id** 로 잇는다.

    설계 §6.1. 이름을 참조값으로 쓰지 않는 이유는 하나다 — 같은 이름의 파일이
    여러 개 올라오는 것은 드문 일이 아니고, 그때 후속 질문은 다른 파일의 상태를
    그 파일의 상태로 답한다. 틀렸다는 신호가 어디에도 안 난다.

    그래서 **정확히 하나로 좁혀지는 것만** 참조로 남긴다.
    """
    from .evidence_refs import AttachmentRef

    names = _attachment_names(hits)
    if not names:
        return []
    from . import attachment_review

    try:
        staged = attachment_review.scan(root)
    except Exception as exc:  # noqa: BLE001 - 참조 하나 때문에 답변을 막지 않는다
        logger.warning("첨부 메타데이터 조회 실패: %s", exc)
        return []
    out = []
    for workspace, channel_id, name in names:
        matches = [
            a
            for a in staged
            if a.workspace == workspace and a.channel_id == channel_id and a.name == name
        ]
        if len(matches) != 1:
            continue
        ref = AttachmentRef(
            workspace=workspace, channel_id=channel_id, file_id=matches[0].file_id
        )
        if ref not in out:
            out.append(ref)
    return out


# 후속 질문의 좌표를 **권한 때문에** 못 연 경우의 사유 코드
# (`ArchiveStore.resolve_refs`). 이때는 넓히지 않는다 — 넓혀도 같은 권한으로
# 찾으므로 결과가 같고, 「다시 찾아봤다」 는 말만 늘어 사람이 권한 문제를
# 검색 문제로 읽는다. 주제가 안 맞아 못 찾은 것과는 사람이 할 일이 다르다.
PERMISSION_MISS_CODES = frozenset({
    "permission_changed", "channel_scope", "workspace_scope", "path_rejected",
})

MISSING_ATTACHMENT_STATUS = "관련 파일의 현재 변환 상태를 확인하지 못했습니다."


def _attachment_status_block(attachments, *, asked: bool = False) -> str:
    """관련 첨부의 **현재** 상태. 아카이브 문장이 아니라 메타데이터가 기준이다.

    설계 §11. 과거 대화에 「처리실패」라고 적혀 있어도 지금 변환됐으면 변환된
    것이다 — 옛 문장을 사실 근거로 쓰면 이미 고친 것을 계속 고장으로 답한다.

    묻지 않았으면 빈 문자열이다. 물었는데 알 수 없으면 **그 사실을 말한다** —
    아무 말도 안 하면 사용자는 문제가 없다고 읽는다.
    """
    items = list(attachments or ())
    # **묻지 않았으면 붙이지 않는다.** 예전에는 첨부가 있기만 하면 블록을 돌려줘서,
    # 요약·검색 답변 끝에 파일 변환 상태가 따라 붙었다. 근거가 0건인 순간에는 그
    # 블록이 답을 통째로 대체했다(2026-09-22 운영). 문서가 말하던 계약을 코드가
    # 지키지 않고 있었다.
    #
    # 근거로 쓴 첨부의 변환 상태는 이 블록이 아니라 `evidence_note()` 의
    # `withheld`·`partial_attachments` 가 알린다 — 그쪽은 답과 함께 나간다.
    if not asked:
        return ""
    if not items:
        return MISSING_ATTACHMENT_STATUS
    lines = [f"• {status_line(a)}" for a in items[:10]]
    if len(items) > 10:
        lines.append(f"… 외 {len(items) - 10}건")
    return "*관련 파일 상태*\n" + "\n".join(lines)


def _evidence_block(hits: list[SearchHit]) -> str:
    return "\n".join(
        f"[{h.line.ts}] ({h.doc.channel}) {h.line.speaker}: {h.line.text}" for h in hits
    )


def _match_terms(lines, terms: list[str] | None):
    """주제어가 걸린 줄만. 걸린 게 없으면 **빈 목록**을 돌려준다.

    빈 목록을 원래 목록으로 되돌리면 "미수금 관련만" 이라는 한정이 조용히
    사라지고, 사용자는 다른 주제 문서를 자기 질문의 답으로 받는다(설계 §10).
    """
    if not terms:
        return list(lines)
    from . import search_index

    tokens = search_index.tokens_of(" ".join(terms))
    if not tokens:
        return list(lines)
    return [ln for ln in lines if search_index.score_line(tokens, "", ln.speaker, ln.text)]


def _block_title(block: str) -> str:
    """근거 블록의 제목. `### 이름 (채널 …)` 의 이름만 쓴다."""
    head = block.split("\n", 1)[0].lstrip("# ").strip()
    name, sep, _ = head.partition(" (채널 ")
    return name if sep else head


def _with_omitted(coverage: str, omitted: list[str]) -> str:
    """입력 한도로 못 보낸 문서를 범위에 더한다.

    **건수와 파일을 밝힌다.** 밝히지 않으면 일부만 본 종합이 완전한 것처럼 읽히고,
    사람은 없는 내용을 봇이 봤다고 믿는다(설계 §12).
    """
    if not omitted:
        return coverage
    names = ", ".join(omitted[:10])
    if len(omitted) > 10:
        names += f" 외 {len(omitted) - 10}건"
    line = f"- 분량 한도로 이번 답변에 넣지 못한 문서 {len(omitted)}건: {names}"
    if not coverage:
        return "*확인 범위*\n" + line
    return f"{coverage}\n{line}"


def _with_coverage(text: str, coverage: str) -> str:
    """답변 끝에 「확인 범위」 를 붙인다.

    일부 문서가 빠졌는데 그 사실이 안 보이면, 불완전한 종합이 완전한 것처럼 읽힌다
    (설계 §13). 모델이 이 줄을 쓰게 하지 않는다 — **건수는 코드가 센 사실**이고,
    모델에게 맡기면 근거 없이 바뀐다.
    """
    body = (text or "").strip()
    if not coverage:
        return body
    return f"{body}\n\n{coverage}" if body else coverage


def _blocks_from_hits(hits: list[SearchHit], ctx) -> tuple[list, list, list, int]:
    """복원된 근거 줄을 채널별 블록으로 묶는다.

    `summarize()` 의 기간 스캔과 **같은 모양**을 만든다. 모양이 다르면 후속 질문만
    다른 프롬프트를 받게 되고, 그 차이는 답변 품질 차이로 나타나면서 원인을
    찾기 어렵다.
    """
    grouped: dict[tuple[str, str], list[SearchHit]] = {}
    for hit in hits:
        grouped.setdefault((hit.doc.workspace, hit.doc.channel), []).append(hit)
    blocks: list[str] = []
    citations: list[str] = []
    parts: list[tuple[str, list[str], list[SearchHit]]] = []
    total = 0
    for (workspace, channel), group in grouped.items():
        group = sorted(group, key=lambda h: (h.line.ts or "", h.line.lineno))
        ws_tag = "" if workspace == ctx.workspace else f"[{workspace}] "
        # 출처에서는 **조직 이름이 앞자리**라, 워크스페이스는 뒤로 민다(원칙 4).
        ws_suffix = "" if workspace == ctx.workspace else f" ({workspace})"
        body = "\n".join(f"[{h.line.ts}] {h.line.speaker}: {h.line.text}" for h in group)
        block = f"### {ws_tag}채널 {channel}\n{body}"
        blocks.append(block)
        total += len(group)
        doc_citations: list[str] = []
        seen: set[tuple[str, str]] = set()
        for hit in group:
            source = hit.line.source_path or hit.doc.path
            date = (hit.line.ts or "").split()[0] if hit.line.ts else ""
            key = (str(source), date)
            if key in seen:
                continue
            seen.add(key)
            doc_citations.append(
                f"{source_label(channel)}{ws_suffix}, 📄{source.name}({date})"
            )
        citations.extend(doc_citations)
        parts.append((block, doc_citations, group))
    return blocks, citations, parts, total


class AnswerEngine:
    def __init__(
        self,
        store: ArchiveStore,
        router: Router,
        *,
        sensitivity: Sensitivity = Sensitivity.CONFIDENTIAL,
        max_hits: int = 20,
        max_lines_per_channel: int = 60,
        specialist=None,
        allow_master_business_answers: bool = False,
    ) -> None:
        self._store = store
        self._router = router
        # 전문가 훅. `(question, ctx, evidence) -> SpecialistAnswer | None`.
        #
        # **엔진은 전문가를 모른다.** 라우팅·DB·계약은 호출부(`slack/pilot.py`)가
        # 넣어 준다. 그래야 엔진 테스트가 DB 없이 돌고, 전문가가 없는 설치에서도
        # 이 파일이 그대로 쓰인다.
        self._specialist = specialist
        self._allow_master_business_answers = bool(allow_master_business_answers)
        if specialist is None and self._allow_master_business_answers:
            # 비교 측정과 구형 단위 테스트에서만 명시적으로 여는 호환 경로다.
            logger.warning(
                "전문 봇 계층 없이 마스터 업무 답변을 명시적으로 허용했습니다. "
                "운영 서비스에서는 사용하면 안 됩니다."
            )
        self._sensitivity = sensitivity
        self._max_hits = max_hits
        self._max_lines_per_channel = max_lines_per_channel

    @classmethod
    def from_env(cls, archive_dir: str | Path, **kw) -> AnswerEngine:
        import os

        from .config import cost_state_path
        from .gateway.budget import WorkspaceLimits

        router = Router.from_default_registry(
            daily_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
            default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
            cost_state_path=cost_state_path(),
            workspace_limits=WorkspaceLimits(),
        )
        return cls(ArchiveStore(archive_dir), router, **kw)

    def model_info(self) -> str:
        return self._router.default_model

    def spent_today(self) -> float:
        return self._router.spent_today

    def spent_today_for(self, workspace: str) -> float:
        return self._router.spent_today_for(workspace)

    def limit_for(self, workspace: str) -> float | None:
        return self._router.limit_for(workspace)

    def _document_set_blocks(
        self, visible_docs, ctx, *, cutoff: str, terms, wanted: list[str]
    ):
        """문서 집합 요약의 근거 블록. 보고서마다 몫을 나눠 준다(설계 §11).

        채널당 최근 N줄과 다른 점은 하나다 — **자르는 단위가 문서**다. 첨부 하나가
        수백 줄이면 채널 예산을 통째로 먹어서 나머지 보고서가 통째로 밀려난다.

        후보는 **파일명**으로 고른다. 파일명에 `주간보고` 가 있고 본문에 그 문구가
        없는 보고서를 놓치지 않으려면 그래야 한다(§2.6). 다만 파일명은 후보 선정까지만
        쓴다 — 내용은 추출된 줄에서만 나온다.
        """
        pairs = []
        for doc in visible_docs:
            lines = [
                ln for ln in doc.raw_lines
                if TS_RE.match(ln.ts) and (not cutoff or ln.ts[:10] >= cutoff)
            ]
            if lines:
                pairs.append((doc, lines))

        documents = document_evidence.group_documents(pairs)
        # 이름이 맞는 첨부만 남긴다. 일반 대화는 이 질문의 후보가 아니다 —
        # 「주간 보고를 종합해줘」 에 잡담을 섞으면 문서 종합이 아니게 된다.
        matched = [
            d for d in documents
            if d.is_attachment and any(w in d.title for w in wanted)
        ]
        if not matched:
            # 파일명으로 못 찾으면 본문에서 찾은 것으로 되돌아간다. 여기서 포기하면
            # 「파일명이 다른 보고서」 가 통째로 사라진다.
            matched = [d for d in documents if d.is_attachment]

        got = document_evidence.allocate(matched, list(terms or []))

        blocks: list[str] = []
        citations: list[str] = []
        parts: list[tuple[str, list[str], list[SearchHit]]] = []
        hits: list[SearchHit] = []
        total = 0
        by_key = {f"{d.workspace}/{d.channel}": d for d, _ in got.selected}
        del by_key  # 키는 문서별이라 채널 사전은 쓰지 않는다. 의도를 남겨 둔다.

        doc_by_key = {}
        for doc, lines in pairs:
            for item in document_evidence.group_documents([(doc, lines)]):
                doc_by_key[item.key] = doc

        for item, picked in got.selected:
            if not picked:
                continue
            source_doc = doc_by_key.get(item.key)
            ws_tag = "" if item.workspace == ctx.workspace else f"[{item.workspace}] "
            body = "\n".join(
                f"[{ln.ts}] {ln.speaker}: {ln.text}" for ln in picked
            )
            # 채널이 아니라 **문서**가 블록의 제목이다. 그래야 모델이 보고서별로
            # 읽고, 답변에서도 보고서를 구별해 쓴다.
            block = f"### {ws_tag}{item.title} (채널 {item.channel})\n{body}"
            blocks.append(block)
            total += len(picked)

            doc_citations: list[str] = []
            seen: set[tuple[str, str]] = set()
            for line in picked:
                source = line.source_path or (source_doc.path if source_doc else None)
                if source is None:
                    continue
                date = (line.ts or "").split()[0] if line.ts else ""
                key = (str(source), date)
                if key in seen:
                    continue
                seen.add(key)
                doc_citations.append(
                    f"{ws_tag}{item.channel}, 📄{item.title}({date})"
                )
                if source_doc is not None:
                    hits.append(SearchHit(doc=source_doc, line=line, score=1))
            citations.extend(doc_citations)
            parts.append((block, doc_citations, list(hits[-len(picked):])))

        return blocks, citations, parts, hits, total, document_evidence.coverage_block(got)

    def _ask_specialist(self, task, question: str, ctx, evidence: str, *, visual=()):
        """전문 봇에게 한 번 맡긴다. 훅이 없으면 `None`.

        **훅이 있으면 반드시 결말이 나온다.** 성공이 아니면 실패지, 마스터가
        답할 신호가 아니다 — 그 구분이 없어서 모든 고장이 정상 답으로 보였다.
        """
        if self._specialist is None:
            return None
        try:
            target = task if task is not None else question
            if visual:
                raw = self._specialist(target, ctx, evidence, visual=tuple(visual))
            else:
                raw = self._specialist(target, ctx, evidence)
        except Exception as exc:  # noqa: BLE001 - 훅 장애도 결말이다
            logger.warning("전문 봇 훅 실패: %s", exc)
            return _Outcome("unavailable", error_code="hook-error")
        return _Outcome.read(raw)

    def _specialist_no_hits(self, outcome, q: str, ctx, *, terms=None, task=None) -> Answer:
        """마스터도 전문 봇도 근거를 못 찾았다.

        **「자료가 없다」 고 단정하지 않는다.** 검색 예산을 다 썼을 수도, 낱말이
        어긋났을 수도 있다. 어디까지 찾아봤는지 밝혀야 사람이 이어서 찾는다.
        """
        if outcome.status == "clarify":
            return self._unavailable(outcome, terms=terms, task=task)
        if outcome.status == "evidence_insufficient":
            return Answer(
                f"「{q}」에 답할 권한 범위의 근거를 확보하지 못했습니다. "
                "자료가 없다고 단정하지 않습니다.",
                [], None, 0.0, 0, "evidence_insufficient",
                terms=list(terms or []),
                specialist_error_code="evidence_insufficient",
                attempted_specialists=list(getattr(outcome, "attempted", ()) or ()),
                required_capability=str(getattr(task, "required_capability", "") or ""),
            )
        if outcome.error_code in ("search-budget-exhausted",):
            return Answer(
                f"「{q}」 를 찾다가 검색 한도에 닿았습니다. 자료가 없다는 뜻은 아닙니다. "
                "채널이나 기간을 좁혀 다시 물어보세요.",
                [], None, 0.0, 0, "search_budget",
                terms=list(terms or []),
                specialist_error_code="search-budget-exhausted",
                attempted_specialists=list(getattr(outcome, "attempted", ()) or ()),
                required_capability=str(getattr(task, "required_capability", "") or ""),
            )
        if outcome.status in ("unavailable", "no_capability", "registry_error"):
            return self._unavailable(outcome, terms=terms, task=task)
        titles = self._store.titles(ctx)
        if not titles:
            return Answer(
                "열람 권한 범위에 아카이브된 문서가 없습니다. "
                "채널에 봇을 초대(`/invite`)하고 대화가 쌓이길 기다려 주세요.",
                [], None, 0.0, 0, "no_access",
            )
        return Answer(
            f"「{q}」 에 해당하는 원문을 찾지 못했습니다. 검색과 문서 읽기를 모두 "
            "거쳤고, 추측으로 답하지 않습니다.",
            [], None, 0.0, 0, "no_hits",
            terms=list(terms or []),
        )

    def _unavailable(
        self, outcome, *, hits=0, terms=None, stage="전문 봇 호출", task=None
    ) -> Answer:
        """전문 봇이 못 답했다. **마스터가 대신 쓰지 않는다.**"""
        trace = {
            "specialist_error_code": outcome.error_code,
            "attempted_specialists": list(getattr(outcome, "attempted", ()) or ()),
            "required_capability": str(
                getattr(task, "required_capability", "") or ""
            ),
        }
        if outcome.status == "clarify":
            return Answer(
                outcome.clarification
                or "어떤 자료를 기준으로 답해야 할지 확실하지 않습니다. 대상을 조금 더 알려주세요.",
                [], None, 0.0, hits, "clarify",
                terms=list(terms or []),
                **trace,
            )
        logger.warning(
            "전문 봇 답변 불가 status=%s code=%s attempted=%s",
            outcome.status, outcome.error_code, trace["attempted_specialists"],
        )
        return Answer(
            unavailable_text(outcome, stage=stage),
            [], None, 0.0, hits, "specialist_unavailable",
            terms=list(terms or []),
            **trace,
        )

    def summarize(
        self,
        ctx: RequestContext,
        *,
        days: int = 7,
        model: str | None = None,
        workspace_filter: frozenset[str] | None = None,
        question: str | None = None,
        terms: list[str] | None = None,
        evidence_hits: list[SearchHit] | None = None,
        task=None,
        document_query: list[str] | None = None,
        all_time: bool = False,
    ) -> Answer:
        """기간 요약 — 권한 내 전 채널의 최근 원문을 정리한다.

        `document_query` 가 있으면 **문서 집합 요약**이다. 채널당 최근 N줄이 아니라
        보고서마다 근거 몫을 나눠 준다(설계 §11) — 첨부 하나가 수백 줄이면 채널 예산을
        통째로 먹어서 「11건을 종합해줘」 가 「한 건의 꼬리」 가 되기 때문이다.

        `all_time` 이면 기간을 자르지 않는다. 「여태까지」 를 7일로 줄이면 파일이
        아카이브에 있어도 후보에 못 들어온다(§9).

        검색이 아니라 기간 스캔이므로, 근거는 여전히 원문 라인 그대로만 넣는다.

        `evidence_hits` 를 주면 **그 범위를 벗어나 새로 검색하지 않는다.** 후속
        질문("방금 그 내용 다시 요약해줘")의 유효 범위는 「현재 권한 ∩ 현재 채널 ∩
        이전 답변의 원문 참조 ∩ 현재 주제」 의 교집합이고, 여기서 기간 스캔으로
        되돌아가면 그 교집합이 조용히 사라진다(설계 §10).
        """
        import datetime as _dt

        blocks: list[str] = []
        specialist_parts: list[tuple[str, list[str], list[SearchHit]]] = []
        summary_hits: list[SearchHit] = []
        citations: list[str] = []
        total = 0
        scoped = evidence_hits is not None
        if scoped:
            summary_hits = list(evidence_hits or ())
            blocks, citations, specialist_parts, total = _blocks_from_hits(summary_hits, ctx)
            visible_docs = []
        else:
            visible_docs = [
                doc
                for doc in self._store.visible_docs(ctx)
                if not workspace_filter or doc.workspace in workspace_filter
            ]
        # 「여태까지」 는 자르지 않는다. 사용자가 요청한 범위와 실제 조회 범위가
        # 다른데 답변은 「자료가 없다」 로 나가면, 그 차이가 어디에도 안 보인다.
        cutoff = (
            "" if all_time
            else (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
        )
        wanted_docs = [d for d in (document_query or []) if d]
        coverage = ""
        if wanted_docs and not scoped:
            blocks, citations, specialist_parts, summary_hits, total, coverage = (
                self._document_set_blocks(
                    visible_docs, ctx, cutoff=cutoff, terms=terms, wanted=wanted_docs
                )
            )
            visible_docs = []

        for doc in visible_docs:
            recent = [
                ln for ln in doc.raw_lines
                if TS_RE.match(ln.ts) and (not cutoff or ln.ts[:10] >= cutoff)
            ]
            recent = _match_terms(recent, terms)
            if not recent:
                continue
            recent = recent[-self._max_lines_per_channel :]
            total += len(recent)
            recent_hits = [SearchHit(doc=doc, line=line, score=1) for line in recent]
            summary_hits.extend(recent_hits)
            body = "\n".join(f"[{ln.ts}] {ln.speaker}: {ln.text}" for ln in recent)
            # 다른 워크스페이스 자료임을 근거와 출처 양쪽에 밝힌다.
            ws_tag = "" if doc.workspace == ctx.workspace else f"[{doc.workspace}] "
            ws_suffix = "" if doc.workspace == ctx.workspace else f" ({doc.workspace})"
            block = f"### {ws_tag}채널 {doc.channel}\n{body}"
            blocks.append(block)

            doc_citations: list[str] = []
            seen_sources: set[tuple[str, str]] = set()
            for line in recent:
                source = line.source_path or doc.path
                date = line.ts.split()[0]
                source_key = (str(source), date)
                if source_key in seen_sources:
                    continue
                seen_sources.add(source_key)
                doc_citations.append(
                    f"{source_label(doc.channel)}{ws_suffix}, 📄{source.name}({date})"
                )
            citations.extend(doc_citations)
            specialist_parts.append((block, doc_citations, recent_hits))

        if not blocks and scoped:
            # 좁혀 물었는데 근거가 안 남았다. **채널 목록으로 넓히지 않는다** —
            # 넓히면 사용자는 자기 질문의 답이 아닌 것을 답으로 받는다(설계 §13).
            return Answer(
                "이전 답변이 근거로 쓴 원문을 현재 권한으로 다시 확인하지 못했습니다. "
                "추측으로 답하지 않습니다.",
                [], None, 0.0, 0, "no_hits",
                context_resolution="scoped_empty",
                required_capability=str(getattr(task, "required_capability", "") or ""),
            )
        if not blocks:
            titles = [doc.channel for doc in visible_docs]
            if not titles:
                return Answer(
                    "열람 권한 범위에 아카이브된 문서가 없습니다. 채널에 봇을 초대하고 수집을 기다려 주세요.",
                    [], None, 0.0, 0, "no_access",
                )
            hint = ""
            if ctx.readable_workspaces:
                names = ", ".join(sorted(ctx.readable_workspaces))
                hint = (
                    f"\n\n참고: 다른 워크스페이스({names}) 자료는 상위(root) 워크스페이스로서 "
                    "전량 조회됩니다."
                    if ctx.is_root
                    else f"\n\n참고: 다른 워크스페이스({names}) 자료는 그쪽에서 "
                    "`share_with` 로 넘긴 문서만 조회됩니다."
                )
            return Answer(
                f"최근 {days}일 원문이 없습니다. 아카이브된 문서: "
                + ", ".join(titles[:20])
                + hint,
                [], None, 0.0, 0, "no_hits",
            )

        # 원본 바이트는 외부 LLM에 보내지 않는다. 수집 단계에서 자동 변환하고 PII
        # 검사를 통과한 텍스트만 근거가 된다.
        withheld = _withheld_attachments(summary_hits)
        citations.extend(_attachment_source_links(summary_hits))

        # **요약도 전문가에게 먼저 묻는다.** Hermes 의 본업이 회의록·업무 진행 요약인데
        # 이 경로에만 훅이 없어서, 등록해도 전문가가 요약 질문을 받지 못했다
        # (2026-09-07 실제 발생). 근거는 아래 blocks 뿐이고 출처는 우리가 붙인다.
        if self._specialist is not None:
            from .specialist_adapters import MAX_EVIDENCE_CHARS

            selected_blocks: list[str] = []
            selected_citations: list[str] = []
            selected_hits: list[SearchHit] = []
            selected_lines = 0
            used_chars = 0
            # 한도를 넘어 못 보낸 것을 **센다.** 조용히 끊으면 일부만 본 종합이
            # 전부를 본 종합처럼 나간다(설계 §12).
            omitted: list[str] = []
            for block, block_citations, block_hits in specialist_parts:
                separator = 2 if selected_blocks else 0
                if used_chars + separator + len(block) > MAX_EVIDENCE_CHARS:
                    omitted.append(_block_title(block))
                    continue
                selected_blocks.append(block)
                selected_citations.extend(block_citations)
                selected_hits.extend(block_hits)
                selected_lines += len(block_hits)
                used_chars += separator + len(block)
            selected_citations.extend(_attachment_source_links(selected_hits))
            specialist_coverage = _with_omitted(coverage, omitted)

            # The adapter's final size guard must not silently cut a channel in half.
            # A tools specialist can retrieve further evidence within its budget.
            specialist_evidence = "\n\n".join(selected_blocks)
            outcome = self._ask_specialist(
                task,
                question or f"최근 {days}일 진행 상황을 정리해 주세요.",
                ctx,
                specialist_evidence,
            )
            special = outcome.answer if outcome is not None and outcome.ok else None
            if special is not None and not _specialist_documents_ok(
                special, ctx, self._store
            ):
                # 계약 위반이다. **마스터가 대신 쓰지 않는다** — 답은 남고 근거만
                # 빠지면 답변과 출처가 어긋난 채로 나간다(설계 §12).
                outcome = _Outcome("unavailable", error_code="acl-source-violation")
                special = None
            if special is not None:
                logger.info(
                    "summary ok(전문가) ws=%s specialist=%s model=%s docs=%d",
                    ctx.workspace,
                    special.specialist,
                    special.model,
                    len(blocks),
                )
                return Answer(
                    _with_coverage(special.text, specialist_coverage),
                    selected_citations,
                    special.model,
                    special.cost_usd,
                    selected_lines,
                    "answered",
                    withheld=withheld,
                    specialist=special.specialist,
                    format_retry_count=int(getattr(special, "format_retry_count", 0) or 0),
                    guardrail_result=_guardrail_result(self._store.root, selected_hits),
                    required_capability=str(getattr(task, "required_capability", "") or ""),
                    attempted_specialists=list(outcome.attempted),
                    evidence_refs=_specialist_evidence_refs(
                        special, selected_hits, self._store.root
                    ),
                    attachment_refs=_attachment_refs(self._store.root, selected_hits),
                    subject_terms=list(terms or []),
                    context_resolution="transmitted_evidence",
                )
            # 전문 봇 계층이 있는데 못 답했다. 여기서 끝난다.
            return self._unavailable(outcome, hits=total, terms=terms, task=task)

        if not self._allow_master_business_answers:
            return self._unavailable(
                _Outcome("unavailable", error_code="specialist-layer-missing"),
                hits=total,
                terms=terms,
                task=task,
            )

        # 전문 봇 계층이 **아예 없는** 설치의 길이다(단위 테스트·전문 봇 미도입).
        # 운영 봇은 `slack/pilot.specialist_hook()` 을 항상 끼우므로 여기로 오지
        # 않는다 — `tests/test_answer_invariants.py` 가 그것을 지킨다.
        summary_prompt = (
            "<원문>\n"
            + "\n\n".join(blocks)
            + f"\n</원문>\n\n질문: {question or f'최근 {days}일 진행 상황을 정리해 주세요.'}"
        )
        messages = [
            Message("system", SUMMARY_PROMPT),
            Message("user", summary_prompt),
        ]
        try:
            resp = self._router.complete(
                messages, model=model, sensitivity=self._sensitivity, max_tokens=2048
            )
        except (UnknownModel, ModelNotAllowed) as e:
            return Answer(f"모델 선택 오류: {e}", [], model, 0.0, total, "error")
        except CostLimitExceeded as e:
            return Answer(f"오늘 LLM 사용 한도에 도달했습니다. ({e})", [], model, 0.0, total, "error")

        logger.info(
            "summary ws=%s days=%d channels=%d lines=%d model=%s cost=$%.4f",
            ctx.workspace, days, len(blocks), total, resp.model, resp.cost_usd,
        )
        return Answer(
            _with_coverage(resp.text.strip(), coverage),
            citations, resp.model, resp.cost_usd, total,
            "answered", withheld=withheld,
            evidence_refs=refs_from_hits(summary_hits, self._store.root),
            attachment_refs=_attachment_refs(self._store.root, summary_hits),
            subject_terms=list(terms or []),
            context_resolution="transmitted_evidence",
        )

    def advise(
        self,
        question: str,
        ctx: RequestContext,
        *,
        terms: list[str] | None = None,
        task=None,
    ) -> Answer:
        """판단·권고 요청 — 사내 사실은 원문만, 일반 판단은 LLM 지식 허용(라벨 부착).

        "출처 없으면 답하지 않는다"는 **사실 조회**의 규칙이다. 판단 요청에 그 규칙을 적용하면
        답을 못 하고, 반대로 라벨 없이 답하면 판단이 사내 사실로 오독된다. 그래서 둘을 분리한다.

        **판단도 업무 답변이다.** 예전에는 이 경로만 전문 봇을 아예 부르지 않고
        마스터가 처음부터 답했다(2026-09-13 검증). 사내 사실이 섞이는 답을
        마스터가 쓰면, 다른 경로에서 막아 둔 것이 여기로 전부 샌다.
        """
        model, q = parse_model_flag(question)
        query = " ".join(terms) if terms else q
        hits = self._store.search(query, ctx, limit=self._max_hits) if query else []

        if self._specialist is not None:
            outcome = self._ask_specialist(task, q, ctx, _evidence_block(hits))
            special = outcome.answer if outcome is not None and outcome.ok else None
            if special is not None and not _specialist_documents_ok(
                special, ctx, self._store
            ):
                outcome = _Outcome("unavailable", error_code="acl-source-violation")
                special = None
            if special is not None:
                citations = _specialist_citations(special, hits, ctx)
                citations += _attachment_source_links(hits)
                text = special.text
                model_name = special.model
                cost_usd = special.cost_usd
                master_interim = False
                # Hermes는 사내 근거를 읽는 전문 봇이다. 별도 판단 전문 봇이
                # 등록되기 전까지만, 마스터가 **같은 권한 필터 원문**으로 조언을
                # 덧붙인다. 외부 사실이나 Hermes 답변을 근거로 삼지 않는다.
                if special.specialist == "hermes":
                    evidence = (
                        f"<원문>\n{_evidence_block(hits)}\n</원문>\n\n"
                        if hits else "<원문>\n(관련 원문 없음)\n</원문>\n\n"
                    )
                    try:
                        comment = self._router.complete(
                            [
                                Message("system", INTERIM_ADVICE_PROMPT),
                                Message("user", f"{evidence}질문: {q}"),
                            ],
                            model=model,
                            sensitivity=self._sensitivity,
                            max_tokens=1000,
                        )
                    except Exception as exc:  # noqa: BLE001 - Hermes 요약은 보존한다
                        logger.warning("마스터 임시 판단 생성 실패: %s", exc)
                    else:
                        body = (comment.text or "").strip()
                        if body:
                            label = (
                                "아래 코멘트는 판단 전문봇 도입 전 TYBot 마스터가 "
                                "작성한 임시 판단입니다."
                            )
                            text = (
                                f"{special.text.rstrip()}\n\n"
                                f"*TYBot 임시 판단·조언*\n_{label}_\n\n{body}"
                            )
                            model_name = comment.model
                            cost_usd += comment.cost_usd
                            master_interim = True
                logger.info(
                    "advice ok(전문가%s) ws=%s specialist=%s hits=%d",
                    "+master-interim" if master_interim else "",
                    ctx.workspace, special.specialist, len(hits),
                )
                return Answer(
                    text,
                    citations,
                    model_name,
                    cost_usd,
                    len(hits),
                    "advice",
                    terms=list(terms or []),
                    specialist=special.specialist,
                    format_retry_count=int(getattr(special, "format_retry_count", 0) or 0),
                    guardrail_result=_guardrail_result(self._store.root, hits),
                    required_capability=str(getattr(task, "required_capability", "") or ""),
                    attempted_specialists=list(outcome.attempted),
                    evidence_refs=_specialist_evidence_refs(
                        special, hits, self._store.root
                    ),
                    subject_terms=list(terms or []),
                    context_resolution="transmitted_evidence",
                    master_interim=master_interim,
                )
            return self._unavailable(outcome, hits=len(hits), terms=terms, task=task)

        if not self._allow_master_business_answers:
            return self._unavailable(
                _Outcome("unavailable", error_code="specialist-layer-missing"),
                hits=len(hits),
                terms=terms,
                task=task,
            )

        # 전문 봇 계층이 없는 설치의 길이다.
        evidence = (
            f"<원문>\n{_evidence_block(hits)}\n</원문>\n\n"
            if hits
            else "<원문>\n(관련 원문 없음)\n</원문>\n\n"
        )
        messages = [
            Message("system", ADVICE_PROMPT),
            Message("user", f"{evidence}질문: {q}"),
        ]
        try:
            resp = self._router.complete(
                messages, model=model, sensitivity=self._sensitivity, max_tokens=1500
            )
        except (UnknownModel, ModelNotAllowed) as e:
            return Answer(f"모델 선택 오류: {e}", [], model, 0.0, len(hits), "error")
        except CostLimitExceeded as e:
            return Answer(f"오늘 LLM 사용 한도에 도달했습니다. ({e})", [], model, 0.0, 0, "error")

        # 라벨은 코드가 붙인다 — LLM 이 빼먹을 수 있는 것을 원칙에 맡기지 않는다.
        if hits:
            head = f"💬 판단 요청으로 답합니다. 아카이브 원문 {len(hits)}건을 근거로 참고했습니다.\n\n"
            citations = [
                h.citation(with_workspace=h.doc.workspace != ctx.workspace) for h in hits[:5]
            ]
            citations += _attachment_source_links(hits)
        else:
            head = "💬 판단 요청으로 답합니다. *아카이브에 관련 원문이 없어 일반적인 판단입니다* — 사내 사실 확인이 필요하면 원문을 따로 확인하세요.\n\n"
            citations = []

        logger.info(
            "advice ws=%s hits=%d model=%s cost=$%.4f q=%r",
            ctx.workspace, len(hits), resp.model, resp.cost_usd, q,
        )
        return Answer(
            head + resp.text.strip(), citations, resp.model, resp.cost_usd, len(hits),
            "advice", terms=list(terms or []),
        )

    def classify(self, question: str) -> Intent:
        """의도 분류(LLM, 실패 시 규칙). 라우팅만 하고 답은 만들지 않는다."""
        _, q = parse_model_flag(question)
        return classify(q, self._router)

    def plan(
        self,
        question: str,
        *,
        conversation_context: str = "",
        thread_has_refs: bool = False,
        specialists=None,
    ) -> list[Intent]:
        """복합 질문을 하위질문 목록으로 분해한다(1차 LLM, 실패 시 규칙).

        라벨 하나만 돌려주던 `classify` 를 대체한다 - 사람은 한 번에 여러 가지를 묻고,
        예전 구조에서는 그중 하나만 처리 경로에 도달했다.
        """
        _, q = parse_model_flag(question)
        return plan(
            q,
            self._router,
            conversation_context=conversation_context,
            thread_has_refs=thread_has_refs,
            specialists=specialists,
        )

    @property
    def router(self):
        """문장 생성(compose)용. 답변 엔진 밖에서도 같은 비용 상한을 쓰게 한다."""
        return self._router

    def respond(
        self,
        question: str,
        ctx: RequestContext,
        intent: Intent | None = None,
        *,
        followup=None,
        task=None,
        seed_hits=(),
    ) -> Answer:
        """아카이브로 답할 수 있는 의도를 처리한다.

        status/help 는 봇 런타임 정보라 Slack 계층이 처리한다 — 여기로 오면 안내만 한다.

        `followup` 은 같은 스레드의 이전 결과를 현재 권한으로 되살린 범위다
        (`thread_followup.ThreadFollowupResolver`). 주어지면 **그 범위를 벗어나지
        않는다** — 복원에 실패해도 채널 전체 검색으로 넓히지 않는다.
        """
        model, q = parse_model_flag(question)
        if not q:
            return Answer("질문 내용이 없습니다.", [], None, 0.0, 0, "error")
        intent = intent or classify(q, self._router)

        if followup is not None and getattr(followup, "applied", False):
            return self._respond_scoped(q, ctx, intent, followup, model=model, task=task)

        # 사람이 **이번 질문에 붙여 올린 원문**(B-57). 「찾아야 하는 자료」가 아니라
        # 「이 질문의 입력」이라, 검색이 낱말을 못 맞혀도 빠지면 안 된다.
        seed = list(seed_hits or ())

        if intent.kind == "summary":
            if seed:
                # "이 파일 요약해줘" — 기간 요약으로 넓히면 방금 올린 파일이
                # 채널 최근 줄에 묻힌다. 올린 것을 요약하라는 뜻으로 읽는다.
                return self.summarize(
                    ctx,
                    days=intent.days or DEFAULT_DAYS,
                    model=model,
                    question=q,
                    terms=list(intent.terms) if intent.document_query else None,
                    evidence_hits=seed,
                    task=task,
                )
            return self.summarize(
                ctx,
                days=intent.days or DEFAULT_DAYS,
                model=model,
                workspace_filter=_mentioned_workspaces(q) or None,
                question=q,
                # 문서 종류가 있으면 문서 집합 요약이다 — 채널당 최근 줄이 아니라
                # 보고서마다 몫을 나눈다(설계 §11).
                document_query=list(intent.document_query),
                all_time=intent.wants_all_time,
                # 주제어는 **문서 집합일 때만** 넘긴다. 일반 기간 요약에서 `terms` 는
                # 하드 필터라, 「이번주 진행 상황」 같은 질문의 낱말로 거르면 원문이
                # 통째로 탈락해 `no_hits` 가 된다. 문서 집합에서는 거르는 게 아니라
                # 문서별로 **고르는** 데 쓰인다.
                terms=list(intent.terms) if intent.document_query else None,
                task=task,
            )
        if intent.kind == "advice":
            return self.advise(question, ctx, terms=intent.terms, task=task)
        if intent.kind == "smalltalk":
            return Answer(
                "네, 대기 중입니다. 아카이브에 쌓인 원문으로 답할 수 있는 걸 물어보세요. "
                "`도움말` 로 사용법을 볼 수 있습니다.",
                [], None, 0.0, 0, "smalltalk",
            )
        if intent.kind == "out_of_scope":
            # 분류기가 "다른 워크스페이스 내용 알려줘" 같은 범위 질문을 외부 정보로 오인하는 일이
            # 있었다. 아카이브 관련 표현이 있으면 거절하지 않고 기간 요약으로 되돌린다.
            if ARCHIVE_SCOPE_RE.search(q):
                logger.info("out_of_scope 재분류 -> summary q=%r", q)
                return self.summarize(
                    ctx,
                    days=parse_period(q),
                    model=model,
                    workspace_filter=_mentioned_workspaces(q) or None,
                    question=q,
                    document_query=list(intent.document_query),
                    all_time=intent.wants_all_time,
                )
            # **거절하기 전에 한 번 찾아본다.** 분류기가 「우리 범위 밖」 이라고
            # 본 질문이 실제로는 채널에 쌓여 있는 일인 경우가 있다 — 사내 도구·
            # 계약·일정 이야기가 그렇다(2026-09-18 문제 패킷 QA 570e9393).
            # 알면서 거절하는 것이 모르고 못 찾는 것보다 나쁘다.
            #
            # 근거가 하나도 없으면 그때 거절한다. 일반 지식 질문은 여기서 0건이
            # 나오므로 결과가 달라지지 않는다.
            probe = self._store.search(
                " ".join(intent.terms) if intent.terms else q,
                ctx,
                limit=self._max_hits,
            )
            if probe:
                logger.info("out_of_scope 재분류 -> 근거 %d줄로 답한다 q=%r", len(probe), q)
                return self.answer(
                    question, ctx, terms=intent.terms, task=task, extra_hits=probe
                )
            return Answer(
                "사내 아카이브에 쌓인 원문만 근거로 답하는 봇입니다. "
                "일반 지식이나 외부 정보는 다루지 않습니다.",
                [], None, 0.0, 0, "out_of_scope",
            )
        if intent.kind in ("status", "help"):
            return Answer(
                "봇 상태·사용법은 `상태` / `도움말` 로 확인하세요.", [], None, 0.0, 0, intent.kind
            )
        return self.answer(
            question, ctx, terms=intent.terms, task=task, extra_hits=seed
        )

    def _respond_scoped(
        self,
        q: str,
        ctx: RequestContext,
        intent: Intent,
        followup,
        *,
        model: str | None = None,
        task=None,
    ) -> Answer:
        """후속 질문 — 되살린 좌표 안에서만 답한다(설계 §9·§10·§13).

        여기서 넓히면 사용자는 좁게 물었는데 넓은 답을 받고, **그 사실을 알 수
        없다.** 근거를 못 찾았으면 못 찾았다고 답하는 것이 맞다.
        """
        if followup.needs_clarification:
            listed = "\n".join(f"• {name}" for name in followup.choices)
            return Answer(
                "어느 파일을 말씀하시는지 확정하지 못했습니다. 아래 중에서 알려주세요."
                f"\n\n{listed}",
                [], None, 0.0, 0, "clarify",
                context_parent_ids=list(followup.parent_record_ids),
                context_resolution=followup.resolution,
            )

        hits = list(followup.evidence_hits)
        if intent.format_only:
            from dataclasses import replace

            if not followup.editing_text or task is None:
                return Answer(
                    "이전 답변과 원문 접근 권한을 모두 확인하지 못해 형식을 변경할 수 없습니다.",
                    [], None, 0.0, 0, "no_hits",
                )
            task = replace(task, editing_text=followup.editing_text)
        asked_status = bool(intent.include_attachment_status)
        status = _attachment_status_block(followup.attachments, asked=asked_status)

        if not hits:
            if followup.attachments and asked_status:
                # 원문은 못 살렸지만 파일 상태는 확인됐다. 물은 것의 절반은 답이다.
                ans = Answer(status, [], None, 0.0, 0, "answered")
            elif (
                widen_terms := [
                    t for t in (list(intent.terms) or list(followup.topic_terms)) if t
                ]
            ) and not (set(followup.dropped_codes) & PERMISSION_MISS_CODES):
                # **되돌아 나오는 문**(2026-09-18 문제 패킷 QA ec366cd4·43142c76).
                #
                # 좁히기는 "방금 그 문서 다시" 를 위한 장치였는데, 사람이 대화로
                # 방향을 다시 줄 때도("~쪽으로 넓혀서 찾아줘", "최신 것으로")
                # 같은 문이 닫혀 「이전 근거를 확인하지 못했습니다」 로 끝났다.
                # 요청의 정반대다.
                #
                # 자기 주제어를 들고 온 질문은 좁힐 대상이 아니라 **새 질문**이다.
                # 그래서 현재 권한으로 다시 찾는다. 다만 **넓혔다고 말한다** —
                # 조용히 넓히면 좁게 물은 사람이 그 사실을 알 수 없다(설계 §10).
                logger.info(
                    "followup 범위 복원 실패 -> 현재 권한으로 재검색 terms=%r dropped=%s",
                    widen_terms, "|".join(followup.dropped_codes) or "-",
                )
                ans = self.answer(q, ctx, terms=widen_terms, task=task)
                ans.text = (
                    "직전 답변의 근거에서는 찾지 못해 **열람 권한 범위 전체에서 다시 찾았습니다.**"
                    f"\n\n{ans.text}"
                )
                ans.context_resolution = "widened_after_scope_miss"
                if status:
                    ans.text = f"{ans.text}\n\n{status}"
                ans.context_parent_ids = list(followup.parent_record_ids)
                logger.info(
                    "followup ws=%s ch=%s %s",
                    ctx.workspace, ctx.channel_id or "-", followup.log_line(),
                )
                return ans
            else:
                # 지칭만 있고 주제가 없는 질문("방금 그거 다시")은 넓힐 대상이
                # 없다. 넓히면 엉뚱한 답이 그 자리에 온다.
                ans = Answer(
                    "이전 답변이 근거로 쓴 원문을 현재 권한으로 다시 확인하지 못했습니다. "
                    "추측으로 답하지 않습니다."
                    + (f"\n\n{status}" if status else ""),
                    [], None, 0.0, 0, "no_hits",
                )
        elif intent.kind == "summary":
            ans = self.summarize(
                ctx,
                days=intent.days or DEFAULT_DAYS,
                model=model,
                question=q,
                terms=list(followup.topic_terms),
                evidence_hits=hits,
                task=task,
            )
        else:
            # advice 도 같은 길로 보낸다. 판단 요청이라고 범위를 넓히면 그 순간
            # 이 질문은 더 이상 후속 질문이 아니다.
            ans = self.answer(
                q, ctx, terms=list(intent.terms), evidence_hits=hits, task=task
            )

        if status and hits:
            ans.text = f"{ans.text}\n\n{status}"
        ans.context_parent_ids = list(followup.parent_record_ids)
        ans.context_resolution = followup.resolution
        if followup.topic_terms:
            ans.subject_terms = list(followup.topic_terms)
        if followup.attachments and not ans.attachment_refs:
            from .evidence_refs import AttachmentRef

            ans.attachment_refs = [
                AttachmentRef(
                    workspace=a.workspace, channel_id=a.channel_id, file_id=a.file_id
                )
                for a in followup.attachments
            ]
        logger.info(
            "followup ws=%s ch=%s %s",
            ctx.workspace,
            ctx.channel_id or "-",
            followup.log_line(),
        )
        return ans

    @staticmethod
    def _with_seed(seed, hits) -> list[SearchHit]:
        """이번 질문에 붙여 올린 원문을 근거 맨 앞에 둔다. 중복은 합친다."""
        rows = list(seed or ())
        if not rows:
            return list(hits)
        seen = {(str(h.doc.path), h.line.lineno) for h in rows}
        for hit in hits:
            key = (str(hit.doc.path), hit.line.lineno)
            if key not in seen:
                seen.add(key)
                rows.append(hit)
        return rows

    def _with_approved_guide(self, query: str, hits, ctx) -> list[SearchHit]:
        """승인 요약이 가리키는 원문 좌표를 **검색 후보에만** 더한다(B-56).

        사람이 쓰는 말과 문서에 적힌 말이 다르면 검색은 옆에 둔 문서를 못 찾는다.
        검토자가 승인한 요약은 그 둘을 이어 주는 유일한 사람 확인 자료다.

        그래도 **승인 문장 자체는 근거로 나가지 않는다.** 여기서 더해지는 것은 그
        문장이 가리킨 원문 줄이고, 그 줄은 요청자의 현재 권한으로 다시 연 것이다.
        검색 결과를 밀어내지 않도록 뒤에 붙인다 — 원문 검색이 먼저다.
        """
        try:
            extra = summary_guide.expand(self._store, ctx, query)
        except Exception:
            # 길잡이 실패가 답변을 막으면 안 된다. 없으면 예전과 같은 검색 결과다.
            logger.warning("승인 요약 길잡이를 쓰지 못했습니다", exc_info=True)
            return list(hits)
        if not extra:
            return list(hits)
        merged = list(hits)
        seen = {(str(h.doc.path), h.line.lineno) for h in merged}
        for hit in extra:
            key = (str(hit.doc.path), hit.line.lineno)
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
        if len(merged) > len(hits):
            logger.info(
                "승인 요약 길잡이로 원문 %d줄을 더했다 ws=%s",
                len(merged) - len(hits), ctx.workspace,
            )
        return merged[: self._max_hits + summary_guide.MAX_EXTRA_HITS]

    def answer(
        self,
        question: str,
        ctx: RequestContext,
        *,
        terms: list[str] | None = None,
        evidence_hits: list[SearchHit] | None = None,
        task=None,
        extra_hits=(),
    ) -> Answer:
        """구체 사실 질문 — 원문 검색 후 그 라인만 근거로 답한다.

        terms 는 분류기가 뽑은 핵심어. 요청 표현("알려줘")이 검색을 오염시키는 걸 막는다.

        `evidence_hits` 를 주면 **새로 검색하지 않는다.** 후속 질문이 가리키는
        범위 밖으로 나가지 않기 위한 것이다(설계 §10).
        """
        model, q = parse_model_flag(question)
        if not q:
            return Answer("질문 내용이 없습니다.", [], None, 0.0, 0, "error")

        scoped = evidence_hits is not None
        if scoped:
            hits = list(evidence_hits or ())
        else:
            # 2겹: 색인이 아니라 원문 라인을 연다.
            query = " ".join(terms) if terms else q
            hits = self._store.search(query, ctx, limit=self._max_hits)
            hits = self._with_approved_guide(query, hits, ctx)
            # 방금 올린 첨부는 **검색 앞에** 둔다. 검색어가 파일 내용과 어긋나도
            # 빠지면 안 되는 근거다 — 사람은 그 파일을 보고 물었다(B-57 §4).
            # 검색을 끄지는 않는다. 「이 목록이 슬랙 어디 있나」 같은 질문은
            # 첨부만으로 답할 수 없다.
            hits = self._with_seed(extra_hits, hits)

        if not hits and scoped:
            return Answer(
                "이전 답변이 근거로 쓴 원문을 현재 권한으로 다시 확인하지 못했습니다. "
                "추측으로 답하지 않습니다.",
                [], None, 0.0, 0, "no_hits",
                context_resolution="scoped_empty",
            )
        if not hits and self._specialist is not None:
            # **마스터 검색이 0건이어도 도구형 전문 봇은 스스로 찾을 수 있다.**
            #
            # 예전에는 여기서 곧바로 「찾지 못했습니다」 로 닫았다. 그래서 사람이
            # 쓴 말과 문서에 적힌 말이 다르기만 해도("미수금"↔"미회수") 읽을 수
            # 있는 문서를 옆에 두고 없다고 답했다. 검색 실패와 자료 부재를 같은
            # 것으로 취급한 셈이다(설계 §3-C).
            #
            # 근거는 **빈 채로** 넘긴다. 「검색 결과 없음」 같은 가짜 근거 한 줄을
            # 만들어 넣으면 근거 자리를 더 이상 믿을 수 없게 된다.
            outcome = self._ask_specialist(task, q, ctx, "")
            special = outcome.answer if outcome is not None and outcome.ok else None
            if special is not None and not _specialist_documents_ok(
                special, ctx, self._store
            ):
                outcome = _Outcome("unavailable", error_code="acl-source-violation")
                special = None
            if special is not None:
                citations = _specialist_citations(special, [], ctx)
                logger.info(
                    "answer ok(전문가·빈 seed) ws=%s specialist=%s srcs=%d",
                    ctx.workspace, special.specialist, len(citations),
                )
                return Answer(
                    special.text,
                    citations,
                    special.model,
                    special.cost_usd,
                    0,
                    "answered",
                    specialist=special.specialist,
                    format_retry_count=int(getattr(special, "format_retry_count", 0) or 0),
                    subject_terms=list(terms or []),
                    context_resolution="specialist_search",
                    required_capability=str(
                        getattr(task, "required_capability", "") or ""
                    ),
                    attempted_specialists=list(outcome.attempted),
                    evidence_refs=_specialist_evidence_refs(
                        special, [], self._store.root
                    ),
                )
            # 전문 봇도 못 찾았다. **「찾지 못했다」 와 「없다」 를 구분해서** 남긴다.
            logger.info(
                "answer no_hits(전문가도 못 찾음) q=%r ws=%s code=%s",
                q, ctx.workspace, outcome.error_code if outcome else "-",
            )
            if outcome is not None and outcome.status not in ("success",):
                return self._specialist_no_hits(outcome, q, ctx, terms=terms, task=task)

        if not hits:
            # 3겹: 근거가 없으면 **다른 질문에 답하지 않는다.** 예전엔 최근 원문 요약으로 폴백했는데,
            # 아카이브와 무관한 질문에도 그럴듯한 딴 얘기를 내놓아 더 나빴다.
            titles = self._store.titles(ctx)
            logger.info(
                "answer no_hits q=%r terms=%r ws=%s titles=%d", q, terms, ctx.workspace, len(titles)
            )
            if not titles:
                return Answer(
                    "열람 권한 범위에 아카이브된 문서가 없습니다. "
                    "채널에 봇을 초대(`/invite`)하고 대화가 쌓이길 기다려 주세요.",
                    [],
                    None,
                    0.0,
                    0,
                    "no_access",
                )
            listed = "\n".join(f"• {t}" for t in titles[:20])
            more = f"\n… 외 {len(titles) - 20}건" if len(titles) > 20 else ""
            return Answer(
                f"「{q}」에 해당하는 원문을 아카이브에서 찾지 못했습니다. 추측으로 답하지 않습니다.\n\n"
                f"열람 가능한 문서:\n{listed}{more}\n\n"
                "• 최근 대화 정리가 필요하면 `요약` 또는 `이번주 진행상황`\n"
                "• 판단·권고가 필요하면 그대로 물어보세요 (예: 「어느 방향이 나을까?」)\n"
                "• 봇 연결·수집 상태는 `상태`",
                [],
                None,
                0.0,
                0,
                "no_hits",
            )

        # 기본 근거는 변환된 텍스트다. 이미지 원본은 아래에서 같은 채널의 정확한 파일로
        # 식별되고 OCR·PII 검사를 통과한 경우에만 시각 입력으로 추가한다.
        withheld = _withheld_attachments(hits)
        partial = _partial_attachments(self._store.root, hits)
        visual = _visual_originals(self._store.root, hits)
        prompt = f"<원문>\n{_evidence_block(hits)}\n</원문>\n\n질문: {q}"
        # 전문가에게 먼저 묻는다. **근거는 이미 권한을 통과한 것뿐**이고(위 검색이
        # `visible_docs` 로 걸렀다), 출처는 아래에서 우리가 붙인다 — 전문가는
        # 문장만 돌려준다(원칙 2·3).
        #
        # 전문가가 없거나 못 답하면 `None` 이고, 그때 마스터가 그대로 답한다.
        if self._specialist is not None:
            # **시각 근거가 있어도 전문 봇이 답한다.** 예전에는 `not visual.any`
            # 일 때만 불렀다 — 이미지 원본이 하나라도 선택되면 마스터 LLM 이
            # 이미지를 직접 읽고 답했고, 이미지 PDF 가 많은 업무에서는 그 길이
            # 「업무 답변은 전문 봇만」 규칙의 가장 큰 구멍이었다(설계 §6.5).
            outcome = self._ask_specialist(
                task, q, ctx, _evidence_block(hits), visual=visual.blocks
            )
            special = outcome.answer if outcome is not None and outcome.ok else None
            if special is not None and not _specialist_documents_ok(
                special, ctx, self._store
            ):
                outcome = _Outcome("unavailable", error_code="acl-source-violation")
                special = None
            if special is not None:
                citations = _specialist_citations(special, hits, ctx)
                citations += _attachment_source_links(hits)
                logger.info(
                    "answer ok(전문가) ws=%s specialist=%s model=%s hits=%d srcs=%s",
                    ctx.workspace,
                    special.specialist,
                    special.model,
                    len(hits),
                    citations,
                )
                return Answer(
                    special.text,
                    citations,
                    special.model,
                    special.cost_usd,
                    len(hits),
                    "answered",
                    specialist=special.specialist,
                    format_retry_count=int(getattr(special, "format_retry_count", 0) or 0),
                    guardrail_result=_guardrail_result(self._store.root, hits),
                    withheld=withheld,
                    partial_attachments=partial,
                    required_capability=str(getattr(task, "required_capability", "") or ""),
                    attempted_specialists=list(outcome.attempted),
                    evidence_refs=_specialist_evidence_refs(
                        special, hits, self._store.root
                    ),
                    attachment_refs=_attachment_refs(self._store.root, hits),
                    subject_terms=list(terms or []),
                    context_resolution="transmitted_evidence",
                )
            return self._unavailable(outcome, hits=len(hits), terms=terms, task=task)

        if not self._allow_master_business_answers:
            return self._unavailable(
                _Outcome("unavailable", error_code="specialist-layer-missing"),
                hits=len(hits),
                terms=terms,
                task=task,
            )

        # 전문 봇 계층이 없는 설치의 길이다(위 `summarize()` 와 같다).
        user_content: str | list[dict] = prompt
        if visual.any:
            user_content = [{"type": "text", "text": prompt}, *visual.blocks]
        messages = [
            Message("system", SYSTEM_PROMPT),
            Message("user", user_content),
        ]
        try:
            resp = self._router.complete(
                messages, model=model, sensitivity=self._sensitivity, max_tokens=1024
            )
        except (UnknownModel, ModelNotAllowed) as e:
            return Answer(f"모델 선택 오류: {e}", [], model, 0.0, len(hits), "error")
        except CostLimitExceeded as e:
            return Answer(f"오늘 LLM 사용 한도에 도달했습니다. ({e})", [], model, 0.0, len(hits), "error")

        citations = [
            h.citation(with_workspace=h.doc.workspace != ctx.workspace) for h in hits[:5]
        ]
        citations += _attachment_source_links(hits)
        # 4겹: 질문·답변·근거를 전부 남긴다.
        logger.info(
            "answer ok ws=%s user=%s model=%s hits=%d cost=$%.4f q=%r srcs=%s",
            ctx.workspace,
            ctx.role,
            resp.model,
            len(hits),
            resp.cost_usd,
            q,
            citations,
        )
        # API 가 붙인 구조화된 인용(페이지 포함). 모델이 쓴 문장이 아니라서
        # 우리가 지어낸 출처가 아니라는 점이 중요하다.
        citations += documents.citation_lines(getattr(resp.raw, "content", None))
        return Answer(
            resp.text.strip(), citations, resp.model, resp.cost_usd, len(hits),
            "answered", terms=list(terms or []), withheld=withheld,
            partial_attachments=partial,
            evidence_refs=refs_from_hits(hits, self._store.root),
            attachment_refs=_attachment_refs(self._store.root, hits),
            subject_terms=list(terms or []),
            context_resolution="transmitted_evidence",
        )
