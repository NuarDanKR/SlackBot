"""질의응답 파이프라인 — 환각방지 4겹을 코드로 강제한다.

순서(바꾸지 말 것): 권한 필터 → 원문 검색 → (0건이면 목록만, LLM 호출 안 함) → LLM → 출처 부착 → 로깅.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import documents
from .access import RequestContext
from .archive.store import ArchiveStore, SearchHit
from .attachment_review import find_sendable, status_line
from .evidence_refs import refs_from_hits
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
        return f"_근거: {' · '.join(bits)}_" if bits else ""

    def to_slack(self) -> str:
        # Slack 에는 표 문법이 없다. 모델이 마크다운 표를 뱉으면 파이프가 그대로 보이고
        # 열이 어긋난다 - 프롬프트로 금지해도 새는 경우가 있어 여기서 다시 그린다.
        from .evidence_view import fix_markdown_tables

        parts = [fix_markdown_tables(self.text)]
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
            prefix = f"[{doc.workspace}] " if doc.workspace != ctx.workspace else ""
            out.append(f"{prefix}{doc.channel}, 📄{doc.path.name}")
    else:
        out = [
            h.citation(with_workspace=h.doc.workspace != ctx.workspace)
            for h in hits[:5]
        ]
    for link in (getattr(special, "live_links", ()) or ())[:3]:
        out.append(f"🔴실시간 <{link}|Slack 원문>")
    return out


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


MISSING_ATTACHMENT_STATUS = "관련 파일의 현재 변환 상태를 확인하지 못했습니다."


def _attachment_status_block(attachments, *, asked: bool = False) -> str:
    """관련 첨부의 **현재** 상태. 아카이브 문장이 아니라 메타데이터가 기준이다.

    설계 §11. 과거 대화에 「처리실패」라고 적혀 있어도 지금 변환됐으면 변환된
    것이다 — 옛 문장을 사실 근거로 쓰면 이미 고친 것을 계속 고장으로 답한다.

    묻지 않았으면 빈 문자열이다. 물었는데 알 수 없으면 **그 사실을 말한다** —
    아무 말도 안 하면 사용자는 문제가 없다고 읽는다.
    """
    items = list(attachments or ())
    if not items:
        return MISSING_ATTACHMENT_STATUS if asked else ""
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
            doc_citations.append(f"{ws_tag}{channel}, 📄{source.name}({date})")
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
    ) -> None:
        self._store = store
        self._router = router
        # 전문가 훅. `(question, ctx, evidence) -> SpecialistAnswer | None`.
        #
        # **엔진은 전문가를 모른다.** 라우팅·DB·계약은 호출부(`slack/pilot.py`)가
        # 넣어 준다. 그래야 엔진 테스트가 DB 없이 돌고, 전문가가 없는 설치에서도
        # 이 파일이 그대로 쓰인다.
        self._specialist = specialist
        self._sensitivity = sensitivity
        self._max_hits = max_hits
        self._max_lines_per_channel = max_lines_per_channel

    @classmethod
    def from_env(cls, archive_dir: str | Path, **kw) -> AnswerEngine:
        import os

        from .config import cost_state_path

        router = Router.from_default_registry(
            daily_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
            default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
            cost_state_path=cost_state_path(),
        )
        return cls(ArchiveStore(archive_dir), router, **kw)

    def model_info(self) -> str:
        return self._router.default_model

    def spent_today(self) -> float:
        return self._router.spent_today

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
    ) -> Answer:
        """기간 요약 — 권한 내 전 채널의 최근 원문을 채널별로 정리한다.

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
        cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
        for doc in visible_docs:
            recent = [ln for ln in doc.raw_lines if TS_RE.match(ln.ts) and ln.ts[:10] >= cutoff]
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
                doc_citations.append(f"{ws_tag}{doc.channel}, 📄{source.name}({date})")
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
            for block, block_citations, block_hits in specialist_parts:
                separator = 2 if selected_blocks else 0
                if used_chars + separator + len(block) > MAX_EVIDENCE_CHARS:
                    break
                selected_blocks.append(block)
                selected_citations.extend(block_citations)
                selected_hits.extend(block_hits)
                selected_lines += len(block_hits)
                used_chars += separator + len(block)
            selected_citations.extend(_attachment_source_links(selected_hits))

            # The adapter's final size guard must not silently cut a channel in half.
            # If no complete channel fits, the master handles the full evidence.
            specialist_evidence = "\n\n".join(selected_blocks)
            special = (
                self._specialist(
                    question or f"최근 {days}일 진행 상황을 정리해 주세요.",
                    ctx,
                    specialist_evidence,
                )
                if specialist_evidence
                else None
            )
            if (
                special is not None
                and special.text.strip()
                and _specialist_documents_ok(special, ctx, self._store)
            ):
                logger.info(
                    "summary ok(전문가) ws=%s specialist=%s model=%s docs=%d",
                    ctx.workspace,
                    special.specialist,
                    special.model,
                    len(blocks),
                )
                return Answer(
                    special.text,
                    selected_citations,
                    special.model,
                    special.cost_usd,
                    selected_lines,
                    "answered",
                    withheld=withheld,
                    specialist=special.specialist,
                    evidence_refs=refs_from_hits(selected_hits, self._store.root),
                    attachment_refs=_attachment_refs(self._store.root, selected_hits),
                    subject_terms=list(terms or []),
                    context_resolution="transmitted_evidence",
                )

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
            resp.text.strip(), citations, resp.model, resp.cost_usd, total,
            "answered", withheld=withheld,
            evidence_refs=refs_from_hits(summary_hits, self._store.root),
            attachment_refs=_attachment_refs(self._store.root, summary_hits),
            subject_terms=list(terms or []),
            context_resolution="transmitted_evidence",
        )

    def advise(
        self, question: str, ctx: RequestContext, *, terms: list[str] | None = None
    ) -> Answer:
        """판단·권고 요청 — 사내 사실은 원문만, 일반 판단은 LLM 지식 허용(라벨 부착).

        "출처 없으면 답하지 않는다"는 **사실 조회**의 규칙이다. 판단 요청에 그 규칙을 적용하면
        답을 못 하고, 반대로 라벨 없이 답하면 판단이 사내 사실로 오독된다. 그래서 둘을 분리한다.
        """
        model, q = parse_model_flag(question)
        query = " ".join(terms) if terms else q
        hits = self._store.search(query, ctx, limit=self._max_hits) if query else []

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
            return self._respond_scoped(q, ctx, intent, followup, model=model)

        if intent.kind == "summary":
            return self.summarize(
                ctx,
                days=intent.days or DEFAULT_DAYS,
                model=model,
                workspace_filter=_mentioned_workspaces(q) or None,
                question=q,
            )
        if intent.kind == "advice":
            return self.advise(question, ctx, terms=intent.terms)
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
        return self.answer(question, ctx, terms=intent.terms)

    def _respond_scoped(
        self,
        q: str,
        ctx: RequestContext,
        intent: Intent,
        followup,
        *,
        model: str | None = None,
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
        asked_status = bool(intent.include_attachment_status)
        status = _attachment_status_block(followup.attachments, asked=asked_status)

        if not hits:
            if followup.attachments and asked_status:
                # 원문은 못 살렸지만 파일 상태는 확인됐다. 물은 것의 절반은 답이다.
                ans = Answer(status, [], None, 0.0, 0, "answered")
            else:
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
            )
        else:
            # advice 도 같은 길로 보낸다. 판단 요청이라고 범위를 넓히면 그 순간
            # 이 질문은 더 이상 후속 질문이 아니다.
            ans = self.answer(q, ctx, terms=list(intent.terms), evidence_hits=hits)

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

    def answer(
        self,
        question: str,
        ctx: RequestContext,
        *,
        terms: list[str] | None = None,
        evidence_hits: list[SearchHit] | None = None,
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

        if not hits and scoped:
            return Answer(
                "이전 답변이 근거로 쓴 원문을 현재 권한으로 다시 확인하지 못했습니다. "
                "추측으로 답하지 않습니다.",
                [], None, 0.0, 0, "no_hits",
                context_resolution="scoped_empty",
            )
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
        visual = _visual_originals(self._store.root, hits)
        prompt = f"<원문>\n{_evidence_block(hits)}\n</원문>\n\n질문: {q}"
        # 전문가에게 먼저 묻는다. **근거는 이미 권한을 통과한 것뿐**이고(위 검색이
        # `visible_docs` 로 걸렀다), 출처는 아래에서 우리가 붙인다 — 전문가는
        # 문장만 돌려준다(원칙 2·3).
        #
        # 전문가가 없거나 못 답하면 `None` 이고, 그때 마스터가 그대로 답한다.
        if self._specialist is not None and not visual.any:
            special = self._specialist(q, ctx, _evidence_block(hits))
            if (
                special is not None
                and special.text.strip()
                and _specialist_documents_ok(special, ctx, self._store)
            ):
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
                    withheld=withheld,
                    evidence_refs=refs_from_hits(hits, self._store.root),
                    attachment_refs=_attachment_refs(self._store.root, hits),
                    subject_terms=list(terms or []),
                    context_resolution="transmitted_evidence",
                )

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
            evidence_refs=refs_from_hits(hits, self._store.root),
            attachment_refs=_attachment_refs(self._store.root, hits),
            subject_terms=list(terms or []),
            context_resolution="transmitted_evidence",
        )
