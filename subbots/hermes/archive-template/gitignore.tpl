# macOS
.DS_Store

# Editor
.vscode/
.idea/
*.swp
*~

# Claude Code local settings (per-user)
.claude/settings.local.json

# Node / scripts
node_modules/
*.log

# Python
__pycache__/
*.pyc
.venv/
venv/
.python-version

# Secrets — 토큰/키는 저장소 안에 두지 않는다 (보관 위치: ~/.claude/.env.notion 등)
.env
.env.*
.env.local
# 경로가 어긋나 만들어진 사본까지 잡는다 (예: 20-operations21-hermes.env).
# `.env` 규칙은 정확히 그 이름만 막아서, 이런 파일은 그대로 커밋될 뻔했다.
*.env
*.key
*.pem
*.token
secrets/

# 단, 값이 비어 있는 예시 파일은 커밋한다 (.env.* 규칙의 예외)
!**/.env.example

# Hermes 실행 로그의 **원본 jsonl 과 자동 반영 임시 파일은 코드 저장소에 있다**
# (`logs/`·`.ingest-tmp/`). 여기서 막을 것이 없다 — 그쪽 .gitignore 가 막는다.
# 이 저장소의 `hermes-log/*.md` 는 사람이 읽는 렌더본이라 **커밋한다.**

# 원자적 쓰기(tmp → rename)의 중간 파일. 이것들은 **저장소 안**에 쓸 수밖에 없다 —
# 다른 드라이브로 옮기면 rename 이 원자적이지 않아진다. 그래서 대신 여기서 무시한다.
# 안 그러면 강제 종료로 조각 하나가 남는 순간 syncBeforeWork 가 작업 트리를 더럽다고 보고
# **모든** 자동 반영이 멈춘다. 사람이 SSH 로 들어가 지울 때까지 봇은 옛 자료로 답한다.
#
# 여기 적는 것은 **대화 쪽뿐**이다 — `ingest/util.js` 의 writeJson,
# `slack-sync/scripts/insert_messages.py` 의 write_lines, `apply_edits.py` 의 save_state.
# 문서 쪽(`doc-archive/scripts/insert_entry.py`)도 tmp 를 쓰지만 거기는 적을 필요가 없다.
# 아래 「문서 아카이브」 절이 화이트리스트라 `*.md` 만 되살리고 `*.md.tmp` 는 그대로 빠진다.
slack-export/*.tmp
slack-export/channels/*.tmp

# 07:00 할 일을 「이 PC 에서 사람이 보고 정했다」는 기록 (archive-inbox 의 decide_work.py).
# 저장소에 넣지 않는다 — VM·다른 PC 의 기록과 섞이면 「여기서 사람이 봤다」는 뜻이 사라진다
# (문서 쪽 `.review-stamp.json` 과 같은 이유). 「빼·나중에」는 판단이라 저장소에 남고
# (`.sync-state.json`), 이 파일에는 승인과 편집 뒤 해시만 있다.
slack-export/.decision-stamp.json

# 「지금 이 PC 에서 archive-run 한 바퀴가 돌고 있다」는 표시 (archive-run 의 board.py).
# 저장소에 넣지 않는다 — 잠금은 이 기계에서 동시에 두 세션이 도는 것을 막으려는 것이고,
# push 되면 다른 PC·VM 이 남의 pid 를 보고 「돌고 있다」고 읽는다. 반대로 여기서 푼 잠금이
# 저장소를 통해 되살아나기도 한다. `.decision-stamp.json` 과 같은 이유다.
slack-export/.archive-run-lock.json

# Temp / work
.playwright-mcp/

# Skill cache
.cache/


# gmail-triage 작업 산출물 (메일 메타데이터 포함 — 커밋 금지)
tmp-triage/
tmp-triage-*/
.claude/skills/*/scripts/__pycache__/

# 문서 아카이브 — md 와 상태 파일만 커밋한다.
# 화이트리스트로 짜는 이유: 확장자 블랙리스트(*.pdf, *.hwp …)는 새 포맷이 하나
# 생길 때마다 뚫리고, 한 번 들어간 바이너리는 히스토리에서 지우기 어렵다.
# 원본 실물은 애초에 워크스페이스 밖(~/.doc-cache/)에 둔다 — 이중 방어.
documents/**
!documents/**/
!documents/**/*.md
!documents/.doc-state.json

# 격리 작업공간 — 저장소에 담지 않는다.
# .claude/worktrees/ 는 하네스가 만드는 워크트리(같은 저장소의 다른 체크아웃)이고,
# .superpowers/ 는 작업 진행 원장·검토 꾸러미라 둘 다 산출물이 아니다.
.claude/worktrees/
.superpowers/

# 백필 진행 상황 — 이 기계의 상태이지 아카이브의 내용이 아니다
slack-export/.backfill-state.json
