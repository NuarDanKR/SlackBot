"""Slack 연동 (Socket Mode, 아웃바운드 전용) — 뼈대.

구현 지침: `.claude/agents/slack-integration.md`.
- 이벤트는 app_mention, message.im 만.
- 새 채널은 /invite @Hermes 가 첫 액션.
- 첨부 다운로드 권한 / 채널 자가참여 권한 필수.
"""
