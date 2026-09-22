#!/usr/bin/env bash
# PostgreSQL 스키마를 **전부** 적용한다. 여러 번 실행해도 안전하다.
#
# ## 왜 스크립트인가
# 예전에는 배포 문서에 적용할 파일을 나열했다. 그 목록이 드리프트했다 — 스키마 파일은
# 18개인데 문서에는 7개만 있었고, 새로 만든 `console_schema.sql`·
# `specialist_runtime_schema.sql` 이 **한 번도 적용되지 않았다.**
#
# 그래서 콘솔 「봇 분류 상태」 가 `column "qa_record_id" does not exist` 로 죽었다
# (2026-09-14). 대조해 보니 빠진 것이 컬럼 9개와 표 4개였다 — 나머지는 그 화면을
# 아직 안 열어서 안 터졌을 뿐이었다.
#
# 사람이 목록을 옮겨 적는 단계는 한 번은 빠진다. 그래서 목록을 여기 둔다.
#
# ## 순서가 있다
# 뒤 파일이 앞 파일의 표를 참조한다(외래키). 알파벳 순으로 돌리면 깨진다.
#
# ## Oracle 쪽은 여기서 안 돌린다
# `oracle_*.sql`·`export_*.sql` 은 그룹웨어 DB 에서 DBA 가 실행한다. 섞으면 psql 이
# 문법 오류로 죽는다.
#
#   sudo /opt/tybot/deploy/apply-schema.sh
#   sudo /opt/tybot/deploy/apply-schema.sh --dry-run    # 무엇을 돌릴지만 본다
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL_DIR="$HERE/sql"
PORT="${TYBOT_DB_PORT:-55432}"
DB="${TYBOT_DB_NAME:-tyslackai}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# 구조 → 기능 → 데이터 보정 순서. 새 스키마 파일을 만들면 **여기에 추가한다.**
# 추가를 잊으면 `scripts/check_schema_drift.py` 가 다음 배포에서 잡는다.
FILES=(
  # 구조. 뒤 파일들이 이 표들을 참조한다.
  index_schema.sql            # org_unit · employee · user_identity · raw_line
  console_schema.sql          # console_user · workspace · usage_* · specialist_*
  # 기능별
  schedule_schema.sql
  schedule_dm_schema.sql
  reviewer_schema.sql
  review_digest_schema.sql
  summary_review_schema.sql
  summary_review_canvas_schema.sql   # B-50. summary_review_candidate 를 참조한다
  llm_secret_schema.sql
  specialist_routing_schema.sql
  specialist_rules_schema.sql
  specialist_runtime_schema.sql
  conversion_queue_schema.sql
  conversion_alert_schema.sql
  pf_console_schema.sql       # /pf/ 콘솔. console_user 를 참조한다
  # 데이터 보정(멱등). 구조가 다 선 뒤에 돌린다.
  schedule_folder_acl_default.sql
  schedule_dm_fixed_ten.sql
)

missing=()
for name in "${FILES[@]}"; do
  [[ -f "$SQL_DIR/$name" ]] || missing+=("$name")
done
if ((${#missing[@]})); then
  echo "스키마 파일이 없다: ${missing[*]}" >&2
  echo "코드를 먼저 배포했는지 확인한다(update.sh)." >&2
  exit 2
fi

# 목록에 없는 Postgres 스키마 파일을 알린다. 새로 만들고 목록에 안 넣은 경우다 —
# 그게 이번 사고의 원인이므로 조용히 넘기지 않는다.
for path in "$SQL_DIR"/*.sql; do
  name="$(basename "$path")"
  case "$name" in
    oracle_*|export_*) continue ;;   # 그룹웨어 Oracle 쪽. 여기서 안 돌린다
  esac
  listed=0
  for known in "${FILES[@]}"; do
    [[ "$known" == "$name" ]] && listed=1 && break
  done
  ((listed)) || echo "! 목록에 없는 스키마 파일: $name — apply-schema.sh 에 추가한다" >&2
done

echo "== PostgreSQL 스키마 적용 (port=$PORT db=$DB, ${#FILES[@]}개)"
if ((!DRY_RUN)); then
  sudo -u tybot env TYBOT_ENV_FILE="${TYBOT_ENV_FILE:-/etc/tybot/tybot.env}" \
    /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_schema_drift.py \
    --target-only --expected-db "$DB" --expected-port "$PORT"
fi
for name in "${FILES[@]}"; do
  if ((DRY_RUN)); then
    echo "   [dry-run] $name"
    continue
  fi
  echo "-- $name"
  # ON_ERROR_STOP: 한 파일이 실패하면 거기서 멈춘다. 계속 돌리면 뒤 파일이 앞 파일의
  # 표를 못 찾아 오류가 줄줄이 나고, 진짜 원인이 스크롤 위로 사라진다.
  sudo -u postgres psql -v ON_ERROR_STOP=1 -p "$PORT" -d "$DB" < "$SQL_DIR/$name"
done

if ((DRY_RUN)); then
  echo "dry-run 이었다. 실제 적용은 --dry-run 없이 다시 실행한다."
  exit 0
fi

echo
echo "== 대조: 선언과 실제가 같은가"
if [[ -x /opt/tybot/.venv/bin/python ]]; then
  sudo -u tybot env TYBOT_ENV_FILE="${TYBOT_ENV_FILE:-/etc/tybot/tybot.env}" \
    /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_schema_drift.py
else
  echo "(check_schema_drift.py 를 돌릴 파이썬을 찾지 못했다 — 직접 확인한다)"
  exit 2
fi
