"""MD 아카이브 수집/검색 파이프라인 — 뼈대.

구현 지침: `.claude/agents/md-archiver.md`, `.claude/skills/md-archive-schema`.
- 원문만 저장(요약 재귀 금지). 경로:
  archive/workspaces/<workspace>/channels/<channel-id>__<name>/raw/YYYY-MM-DD.md
- 게시 전 형식 검사, 실패 시 그날 취합 롤백.
- 검색 0건이면 해당 사업장 문서 제목 목록을 함께 반환.
"""
