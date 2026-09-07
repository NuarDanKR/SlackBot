#!/usr/bin/env python3
"""TYBot PreToolUse 가드.

- 시크릿(슬랙/LLM 토큰, 개인키 등)이 커밋/파일에 들어가는 것을 차단
- PII/아카이브 제외 대상 키워드 차단
- 위험한 git 명령(force push) 차단
exit code 2 = 도구 호출 차단 + stderr 를 Claude 에게 피드백.
"""
import sys
import re
import json

SECRET_PATTERNS = [
    (r"xoxb-[A-Za-z0-9-]{10,}", "Slack bot token"),
    (r"xapp-[A-Za-z0-9-]{10,}", "Slack app token"),
    (r"xoxp-[A-Za-z0-9-]{10,}", "Slack user token"),
    (r"sk-ant-[A-Za-z0-9-]{20,}", "Anthropic API key"),
    (r"sk-[A-Za-z0-9]{32,}", "OpenAI API key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "Private key"),
    (r"ghp_[A-Za-z0-9]{30,}", "GitHub PAT"),
]

# 아카이브 금지 / 개인정보 키워드
PII_KEYWORDS = ["등기부등본", "계약자명단", "계약자 명단", "주민등록번호", "주민번호"]


def block(msg: str):
    sys.stderr.write(f"[tybot-guard] BLOCKED: {msg}\n")
    sys.exit(2)


def scan_secret(text: str, where: str):
    for pat, name in SECRET_PATTERNS:
        if re.search(pat, text):
            block(
                f"{name} 가 {where} 에서 감지됨. 시크릿은 저장소 금지 — "
                f"서버 설정/시크릿 매니저로만 관리하고 .env.example 에는 자리표시자만."
            )


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    if tool == "Bash":
        cmd = ti.get("command", "")
        scan_secret(cmd, "쉘 명령어")
        # **`git push` 가 있는 구간 안에서만** 강제 플래그를 찾는다.
        #
        # 예전에는 명령어 전체에서 `-f` 를 찾았다. 그래서 커밋 메시지 heredoc 안의
        # `psql ... -f -` 같은 예시 문구가 강제 푸시로 읽혀 **멀쩡한 커밋이 막혔다**
        # (2026-09-07). 오탐이 잦으면 사람이 가드를 끄게 되고, 그때 진짜가 지나간다.
        #
        # 구간은 `git push` 부터 다음 셸 구분자(`\n ; & |`)까지다.
        #
        # **`#` 에서는 자르지 않는다** — 인자 안의 `#`(`feat#3` 같은 브랜치명) 뒤에
        # 오는 진짜 플래그를 놓치기 때문이다. 그래서 같은 줄 주석에 적은 플래그도
        # 막힌다. 놓치는 쪽이 훨씬 나쁘다: 중앙 저장소 이력은 되돌릴 수 없다.
        #
        # 어느 쪽으로 틀리는지는 `tests/test_guard_hook.py` 가 고정한다.
        for segment in re.findall(r"git\s+push[^\n;&|]*", cmd):
            if re.search(r"(--force\b|-f\b|--force-with-lease)", segment):
                block(
                    "git force push 감지. 중앙 아카이브 저장소는 이력 손상 위험으로 강제 푸시 금지."
                )
        return

    if tool in ("Write", "Edit", "MultiEdit"):
        path = ti.get("file_path", "") or ""
        content = ti.get("content") or ti.get("new_string") or ""
        blob = f"{path}\n{content}"
        scan_secret(blob, f"파일 {path}")

        if path.endswith(".env") and not path.endswith(".env.example"):
            block(".env 작성 감지 — 시크릿은 버전관리 대상 아님. .env.example 만 커밋하세요.")

        for kw in PII_KEYWORDS:
            if kw in blob:
                block(
                    f"개인정보/아카이브 제외 키워드('{kw}') 감지 — "
                    f"아카이브 원칙상 이 자료는 아카이브 대상에서 제외입니다."
                )
        return


if __name__ == "__main__":
    main()
