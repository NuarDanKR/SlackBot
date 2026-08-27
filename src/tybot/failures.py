"""예외를 사용자에게 보여줄 한 줄로 바꾼다.

왜 필요한가 — 예전에는 요청 처리 중 예외가 나면 👀 반응만 붙고 답이 없었다.
사용자는 봇이 무시했다고 생각하고, 원인은 서버 로그를 보는 사람만 알 수 있었다.
'조용한 고장'을 사용자에게 드러내는 마지막 겹이다.

두 가지 규칙:
1. **시크릿 금지** — 키·토큰 값은 어떤 경로로도 문구에 넣지 않는다.
2. **스택트레이스 금지** — Slack 에 남는 내용은 채널 기록이 되고 검색 대상이 된다.
   상세는 journald 에만 남긴다.
"""
from __future__ import annotations

AUTH = (
    "⚠️ LLM 인증에 실패했습니다(401). 답변을 만들 수 없습니다.\n"
    "서버 관리자: `ANTHROPIC_API_KEY` 를 확인하세요 — 만료·오타이거나 값에 "
    "줄바꿈·공백이 섞였을 수 있습니다. 점검: `scripts/check_env.py`"
)
RATE = "⚠️ 호출 한도에 걸렸습니다. 잠시 뒤 다시 불러주세요."
SCOPE = (
    "⚠️ Slack 권한이 부족해 처리하지 못했습니다.\n"
    "서버 관리자: 앱 스코프(매니페스트)를 확인하세요."
)
WRITE = (
    "⚠️ 저장 경로에 쓸 수 없습니다. 원문이 저장되지 않습니다.\n"
    "서버 관리자: `ARCHIVE_DIR`/`QA_LOG_DIR` 설정과 디렉터리 권한을 확인하세요."
)


def failure_message(e: Exception) -> str:
    """예외 → 사용자용 문구. 조치 주체(사용자/관리자)를 문구에 밝힌다."""
    s = f"{e.__class__.__name__}: {e}"
    low = s.lower()

    # 비용 상한 등 우리가 직접 만든 한국어 메시지는 그대로 보여주는 게 가장 정확하다.
    if "한도" in s or "상한" in s:
        return f"⚠️ {e}"
    if "authentication" in low or "api key" in low or "401" in s:
        return AUTH
    if "rate" in low and "limit" in low:
        return RATE
    if "429" in s:
        return RATE
    if "missing_scope" in low or "not_allowed" in low or "not_authed" in low:
        return SCOPE
    if "read-only" in low or "readonly" in low or "permission denied" in low:
        return WRITE
    return (
        "⚠️ 요청을 처리하다 오류가 났습니다. 답변을 만들지 못했습니다.\n"
        f"관리자 확인용: `{s[:200]}`"
    )
