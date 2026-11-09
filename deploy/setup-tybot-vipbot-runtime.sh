#!/usr/bin/env bash
# B-83 VIPBot 전용 **접속 계정**을 준비한다. 여러 번 실행해도 안전하다.
#
#   /opt/tybot/deploy/setup-tybot-vipbot-runtime.sh                 # 무엇을 할지만 본다
#   sudo /opt/tybot/deploy/setup-tybot-vipbot-runtime.sh --apply    # 역할을 만든다
#   sudo /opt/tybot/deploy/setup-tybot-vipbot-runtime.sh --set-password
#   sudo /opt/tybot/deploy/setup-tybot-vipbot-runtime.sh --verify   # 확인만
#
# ## 왜 역할이 둘인가
#
# `tybot_vipbot` 은 **NOLOGIN 그룹**이다. 권한만 들고 있고 접속하지 않는다.
# 실제로 접속하는 주체는 `tybot_vipbot_runtime` 이고, 그룹의 멤버로서 권한을
# 상속받는다.
#
# 하나로 합치면 권한을 쥔 역할이 곧 접속 자격을 갖는다. 그러면 비밀번호를
# 바꾸거나 계정을 끊는 일이 **권한 구조를 건드리는 일**이 되고, 운영 중에
# 그것을 하기 어려워진다. 둘로 나누면 계정은 끊어도 권한 선언은 그대로다.
#
# ## 이 계정의 특권 창구는 전용 함수 두 개뿐이다
#
# 두 VIPBot SECURITY DEFINER 함수 외의 특권 함수와 기반 표는 열지 않는다.
# `bot_connection` 과 `bot_connection_secret` 을 직접 SELECT 할 수 없다.
#
# 그 두 표에는 **모든 봇의 토큰 암호문**이 들어 있다. VIPBot 런타임이 표를 읽을
# 수 있으면, VIPBot 한 줄의 버그나 한 번의 잘못된 질의가 Master·Archiver·전문봇의
# 토큰까지 꺼낸다. 함수는 `SECURITY DEFINER` 로 **그 한 줄만** 돌려주고, 조건
# (`bot_key='vipbot'` · `state='enabled'` · `identity_ok`)은 함수 안에 박혀 있어
# 호출자가 바꿀 수 없다.
#
# ## 하지 않는 것
#
# - **Master 의 `DATABASE_URL` 을 재사용하지 않는다.** 그 값으로 붙으면 화면은
#   정상으로 보이고, 권한 분리는 이미 사라진 뒤다
# - **비밀번호를 인자로 받지 않는다.** 인자로 받으면 `ps` 와 shell 이력에 남는다.
#   `--set-password` 는 psql 의 `\password` 를 띄울 뿐이고, 입력한 값은 psql 이
#   클라이언트에서 SCRAM 해시로 바꿔 보낸다
# - **남의 역할을 넘겨받지 않는다.** 다른 그룹 멤버십·ADMIN OPTION·과한 속성이
#   있으면 **거절하고 멈춘다.** 자동으로 떼거나 낮추지 않는다
# - **PUBLIC 에 아무것도 주지 않는다.** 이 스크립트에 `GRANT … TO PUBLIC` 이 한
#   줄도 없고, `--verify` 가 그것을 되검사한다
# - **서비스를 만들거나 켜지 않는다.** 「LOGIN 계정 준비」 와 「VIPBot 가동」 은
#   다른 완료다. `deploy/tybot-vipbot.service` 는 초안이고 `[Install]` 이 없다
# - **DB 를 만들지 않는다.** 대상이 예상과 다르면 멈춘다
#
# ## 2026-11-05 현재
#
# `--apply` 는 운영 DB 에서 **아직 실행하지 않았다.**
set -euo pipefail

ROLE_GROUP=tybot_vipbot
ROLE_RUNTIME=tybot_vipbot_runtime
ROLE_DEFINER=tybot_vipbot_definer
CREDENTIAL=tybot-vipbot-database-url
CONFIG_FN="public.vipbot_connection_config(text)"
ACCESS_FN="public.vipbot_authorization(text,text)"
PORT="${TYBOT_DB_PORT:-55432}"
DB="${TYBOT_DB_NAME:-tyslackai}"
HOST="${TYBOT_DB_HOST:-/run/postgresql}"

MODE=plan
case "${1:-}" in
  ""|--dry-run)    MODE=plan ;;
  --apply)         MODE=apply ;;
  --verify)        MODE=verify ;;
  --set-password)  MODE=password ;;
  *) echo "알 수 없는 인자: $1 (--dry-run | --apply | --verify | --set-password)" >&2; exit 2 ;;
esac

log()  { echo "[$(date '+%F %T')] $*"; }
fail() { echo "✗ $*" >&2; exit 1; }

# 결과만 돌려주는 한 줄 질의. `-A -t` 로 머리글·여백을 뺀다.
psql_q() { sudo -u postgres psql -v ON_ERROR_STOP=1 -h "$HOST" -p "$PORT" -d "$DB" -X -q -At -c "$1"; }
psql_run() { sudo -u postgres psql -v ON_ERROR_STOP=1 -h "$HOST" -p "$PORT" -d "$DB" -X -q -c "$1" >/dev/null; }

# 역할을 만들 때 쓰는 속성. 만들 때와 고칠 때가 **같은 목록**이어야 한다 —
# 손으로 만든 역할이 섞여 있어도 이 스크립트를 돌리면 같은 모양이 된다.
ATTRS="LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"

SQL_CREATE="DO \$runtime\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$ROLE_RUNTIME') THEN
    EXECUTE 'CREATE ROLE $ROLE_RUNTIME $ATTRS';
  END IF;
END \$runtime\$;"
SQL_ENFORCE="ALTER ROLE $ROLE_RUNTIME $ATTRS;"
# ADMIN OPTION 을 주지 않는다. 주면 접속 계정이 그룹을 **남에게 줄 수 있다.**
SQL_MEMBER="GRANT $ROLE_GROUP TO $ROLE_RUNTIME;"

# ---------------------------------------------------------------------------
# 계획만 — root 도 psql 도 필요 없다. 그래서 개발 PC 와 시험에서 돌릴 수 있다.
# ---------------------------------------------------------------------------
if [[ "$MODE" == plan ]]; then
  cat <<PLAN
== VIPBot 전용 LOGIN 계정 준비 (계획만 · DB 에 접속하지 않음)

대상        : host=$HOST db=$DB port=$PORT (다르면 --apply 가 중단한다)
그룹        : $ROLE_GROUP   — NOLOGIN 이어야 한다. 아니면 중단
접속 계정   : $ROLE_RUNTIME — $ATTRS
멤버십      : $ROLE_GROUP 하나뿐. ADMIN OPTION 없음
특권 창구    : $CONFIG_FN · $ACCESS_FN 실행.
              연결·권한·사용자·근거 기반 표 직접 SELECT 불가
사전 검토   : 역할이 이미 있으면 **바꾸기 전에** 멤버십·과한 속성을 본다.
              걸리면 거절한다 — 자동으로 떼거나 낮추지 않는다

적용 시 실행할 SQL:
  $SQL_CREATE
  $SQL_ENFORCE
  $SQL_MEMBER

권한 자체는 이 스크립트가 주지 않는다. deploy/sql/bot_connection_schema.sql 이
$ROLE_GROUP 에 주고, 이 계정은 그룹의 멤버로서 상속받는다. 먼저 돌려 두세요:
  sudo /opt/tybot/deploy/apply-schema.sh

비밀번호는 이 스크립트를 지나가지 않는다:
  sudo $0 --set-password

그 뒤 DSN 을 **파일로** 둔다(값은 사람이 적는다. 인자로 넘기지 않는다):
  sudo install -o tybot -g tybot -m 0600 /dev/null /etc/tybot/$CREDENTIAL
  sudo -u tybot vi /etc/tybot/$CREDENTIAL

DSN 은 **URI 형식**이고 접속 user 가 $ROLE_RUNTIME 이어야 한다. 런타임이 구조로
확인하고 다르면 거절한다 — 글자가 달라도 관리자 계정으로 붙으면 소용없다.
  postgresql://$ROLE_RUNTIME:<비밀번호>@/$DB?host=/run/postgresql&port=$PORT
  (host=… user=… 키워드 형식은 받지 않는다. ?user= 로 덮어쓴 것도 거절한다)

서비스에는 **자격증명으로** 넘긴다. 환경변수에 DSN 을 적지 않는다:
  LoadCredential=$CREDENTIAL:/etc/tybot/$CREDENTIAL
  (deploy/tybot-vipbot.service 초안에 이미 들어 있다)

확인:
  sudo $0 --verify

이것만으로 VIPBot 이 도는 것은 아니다. 서비스 유닛은 초안이고 꺼져 있다.
PLAN
  exit 0
fi

[[ $EUID -eq 0 ]] || fail "root 로 실행하세요 (sudo)"
command -v psql >/dev/null 2>&1 || fail "psql 을 찾지 못했습니다"

# ---------------------------------------------------------------------------
# 대상 확인 — 엉뚱한 DB 에 역할을 만들지 않는다
# ---------------------------------------------------------------------------
# `inet_server_port()` 를 쓰지 않는다. 유닉스 소켓 접속에서는 NULL 이라
# **대조가 늘 통과**한다. `current_setting('port')` 은 소켓에서도 값이 있다.
target="$(psql_q "SELECT current_database() || ':' || current_setting('port')")"
[[ "$target" == "$DB:$PORT" ]] || fail "대상이 다릅니다: 기대 $DB:$PORT · 실제 $target"
log "대상 확인: $target"

# ---------------------------------------------------------------------------
# 그룹은 NOLOGIN 이어야 한다
# ---------------------------------------------------------------------------
group_login="$(psql_q "SELECT coalesce(
    (SELECT CASE WHEN rolcanlogin THEN 't' ELSE 'f' END
       FROM pg_roles WHERE rolname='$ROLE_GROUP'), 'absent')")"
[[ "$group_login" != absent ]] || fail "$ROLE_GROUP 역할이 없습니다. deploy/apply-schema.sh 를 먼저 돌리세요"
[[ "$group_login" == f ]] || fail "$ROLE_GROUP 이 LOGIN 입니다. 그룹에는 접속 자격이 없어야 합니다"
log "$ROLE_GROUP: NOLOGIN 확인"

group_members="$(psql_q "SELECT coalesce(string_agg(m.rolname||':'||a.admin_option::text, ',' ORDER BY m.rolname),'')
                       FROM pg_auth_members a
                       JOIN pg_roles g ON g.oid=a.roleid
                       JOIN pg_roles m ON m.oid=a.member
                      WHERE g.rolname='$ROLE_GROUP'")"
if [[ -n "$group_members" && "$group_members" != "$ROLE_RUNTIME:false" ]]; then
  fail "$ROLE_GROUP 의 기존 멤버가 계약과 다릅니다: $group_members"
fi
log "$ROLE_GROUP: 멤버가 없거나 $ROLE_RUNTIME 하나뿐임을 확인"

# 그룹이 **다른 역할의 멤버**여도 안 된다. 그러면 이 계정은 그룹을 타고 그쪽
# 권한까지 상속받는다 — 함수 하나만 준 의미가 사라지고, 그 사실은 `\drg` 를
# 일부러 들여다보기 전에는 아무 데도 안 보인다.
group_parents="$(psql_q "SELECT coalesce(string_agg(g.rolname, ',' ORDER BY g.rolname),'')
                           FROM pg_auth_members a
                           JOIN pg_roles g ON g.oid=a.roleid
                           JOIN pg_roles m ON m.oid=a.member
                          WHERE m.rolname='$ROLE_GROUP'")"
[[ -z "$group_parents" ]] || fail "$ROLE_GROUP 이 다른 역할의 멤버입니다: $group_parents"
log "$ROLE_GROUP: 다른 역할을 상속하지 않음"

definer_state="$(psql_q "SELECT coalesce(
    (SELECT concat(
        CASE WHEN rolcanlogin THEN 't' ELSE 'f' END,
        CASE WHEN rolsuper THEN 't' ELSE 'f' END,
        CASE WHEN rolcreatedb THEN 't' ELSE 'f' END,
        CASE WHEN rolcreaterole THEN 't' ELSE 'f' END,
        CASE WHEN rolreplication THEN 't' ELSE 'f' END,
        CASE WHEN rolbypassrls THEN 't' ELSE 'f' END)
       FROM pg_roles WHERE rolname='$ROLE_DEFINER'), 'absent')")"
[[ "$definer_state" == ffffff ]] || fail "$ROLE_DEFINER 속성이 다릅니다(기대 ffffff · 실제 $definer_state)"
definer_memberships="$(psql_q "SELECT count(*)
  FROM pg_auth_members a
  JOIN pg_roles parent_role ON parent_role.oid=a.roleid
  JOIN pg_roles child_role ON child_role.oid=a.member
 WHERE child_role.rolname='$ROLE_DEFINER' OR parent_role.rolname='$ROLE_DEFINER'")"
[[ "$definer_memberships" == 0 ]] || fail "$ROLE_DEFINER 에 멤버 또는 상속 역할이 있습니다"
log "$ROLE_DEFINER: NOLOGIN · 과한 속성/멤버십 없음"

# ---------------------------------------------------------------------------
# 기존 역할 사전 검토 — **아무것도 바꾸기 전에** 본다
# ---------------------------------------------------------------------------
# 이미 있는 역할에는 이미 비밀번호가 있을 수 있다. 그 상태에서 `ALTER ROLE …
# LOGIN` 을 먼저 켜고 나중에 검증하면, 검증이 실패하는 동안에도 **과한 권한으로
# 로그인할 수 있는 창**이 열린다. 그래서 순서를 뒤집는다 — 안전하다고 확인되기
# 전에는 `CREATE`·`ALTER`·`GRANT` 가 한 줄도 돌지 않는다.
#
# 고치지도 않는다. 추가 멤버십을 자동으로 떼거나 속성을 자동으로 낮추면, 남이
# 다른 용도로 만든 역할을 **말없이 빼앗는** 일이 된다. 무엇이 걸렸는지 말하고
# 멈춘다. 사람이 확인한 뒤 직접 줄이거나 지운다.
runtime_exists="$(psql_q "SELECT count(*) FROM pg_roles WHERE rolname='$ROLE_RUNTIME'")"
if [[ "$runtime_exists" == 1 ]]; then
  # 멤버십이 없는 것은 괜찮다 — `--apply` 가 중간에 끊긴 뒤 다시 돌리는 경우다.
  # 괜찮지 않은 것은 **다른 그룹**과 **ADMIN OPTION** 이다.
  existing_members="$(psql_q "SELECT coalesce(string_agg(g.rolname||':'||a.admin_option::text, ',' ORDER BY g.rolname),'')
                     FROM pg_auth_members a
                     JOIN pg_roles g ON g.oid=a.roleid
                     JOIN pg_roles m ON m.oid=a.member
                    WHERE m.rolname='$ROLE_RUNTIME'")"
  if [[ -n "$existing_members" && "$existing_members" != "$ROLE_GROUP:false" ]]; then
    echo "✗ $ROLE_RUNTIME 의 기존 멤버십이 계약과 다릅니다: $existing_members" >&2
    echo "   기대: $ROLE_GROUP:false (그룹 하나 · ADMIN OPTION 없음)" >&2
    echo "   자동으로 떼지 않습니다. LOGIN 을 켜기 전에 사람이 확인하세요:" >&2
    echo "     sudo -u postgres psql -p $PORT -d $DB -c \"\\\\drg $ROLE_RUNTIME\"" >&2
    exit 1
  fi

  # 과한 속성도 여기서 멈춘다. `ALTER ROLE … LOGIN NOSUPERUSER …` 는 한 문장이라
  # 권한을 깎으면서 LOGIN 을 켜지만, 그 역할이 **다른 용도로 쓰이고 있었다면**
  # 깎는 것 자체가 사고다.
  existing_attrs="$(psql_q "SELECT concat_ws(' ',
        CASE WHEN rolsuper THEN 'SUPERUSER' END,
        CASE WHEN rolcreatedb THEN 'CREATEDB' END,
        CASE WHEN rolcreaterole THEN 'CREATEROLE' END,
        CASE WHEN rolreplication THEN 'REPLICATION' END,
        CASE WHEN rolbypassrls THEN 'BYPASSRLS' END)
      FROM pg_roles WHERE rolname='$ROLE_RUNTIME'")"
  if [[ -n "$existing_attrs" ]]; then
    echo "✗ $ROLE_RUNTIME 에 과한 속성이 있습니다: $existing_attrs" >&2
    echo "   이 스크립트는 남이 만든 역할을 넘겨받지 않습니다." >&2
    echo "   사람이 확인한 뒤 직접 줄이거나 지우세요:" >&2
    echo "     sudo -u postgres psql -p $PORT -d $DB -c \"\\\\du $ROLE_RUNTIME\"" >&2
    exit 1
  fi
  log "$ROLE_RUNTIME: 기존 역할 사전 검토 통과 (멤버십·속성)"
fi

# ---------------------------------------------------------------------------
# 만들기 / 속성 맞추기 / 멤버십
# ---------------------------------------------------------------------------
if [[ "$MODE" == apply ]]; then
  log "$ROLE_RUNTIME 역할을 만들거나 속성을 맞춥니다"
  psql_run "$SQL_CREATE"
  psql_run "$SQL_ENFORCE"
  psql_run "$SQL_MEMBER"
fi

if [[ "$MODE" == password ]]; then
  exists="$(psql_q "SELECT count(*) FROM pg_roles WHERE rolname='$ROLE_RUNTIME'")"
  [[ "$exists" == 1 ]] || fail "$ROLE_RUNTIME 역할이 없습니다. 먼저 --apply 를 돌리세요"
  echo "비밀번호를 입력하세요. 입력한 값은 psql 이 SCRAM 해시로 바꿔 보냅니다."
  echo "(인자·shell 이력·프로세스 목록에 남지 않습니다. 서버 로그에는 평문이 아닌 해시가 남습니다.)"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -h "$HOST" -p "$PORT" -d "$DB" -X -c "\\password $ROLE_RUNTIME"
  log "비밀번호를 설정했습니다. DSN 파일은 0600 으로 따로 둡니다."
  exit 0
fi

# ---------------------------------------------------------------------------
# 확인 — apply 뒤에도, --verify 로도 같은 검사를 돈다
# ---------------------------------------------------------------------------
attrs="$(psql_q "SELECT concat(
                    CASE WHEN rolcanlogin THEN 't' ELSE 'f' END,
                    CASE WHEN rolinherit THEN 't' ELSE 'f' END,
                    CASE WHEN rolsuper THEN 't' ELSE 'f' END,
                    CASE WHEN rolcreatedb THEN 't' ELSE 'f' END,
                    CASE WHEN rolcreaterole THEN 't' ELSE 'f' END,
                    CASE WHEN rolreplication THEN 't' ELSE 'f' END,
                    CASE WHEN rolbypassrls THEN 't' ELSE 'f' END)
                 FROM pg_roles WHERE rolname='$ROLE_RUNTIME'")"
if [[ -z "$attrs" ]]; then
  echo "준비 상태: $ROLE_RUNTIME 이 아직 없습니다. 구조만 선 상태입니다."
  echo "   만들려면: sudo $0 --apply"
  exit 0
fi
# 순서: login inherit super createdb createrole replication bypassrls
[[ "$attrs" == "ttfffff" ]] || fail "$ROLE_RUNTIME 속성이 다릅니다(기대 ttfffff · 실제 $attrs). 기대: $ATTRS"
log "$ROLE_RUNTIME: 속성 확인 ($ATTRS)"

members="$(psql_q "SELECT coalesce(string_agg(g.rolname||':'||a.admin_option::text, ',' ORDER BY g.rolname),'')
                     FROM pg_auth_members a
                     JOIN pg_roles g ON g.oid=a.roleid
                     JOIN pg_roles m ON m.oid=a.member
                    WHERE m.rolname='$ROLE_RUNTIME'")"
[[ "$members" == "$ROLE_GROUP:false" ]] || fail "멤버십이 다릅니다(기대 $ROLE_GROUP:false · 실제 '${members:-없음}')"
log "멤버십 확인: $ROLE_GROUP 하나, ADMIN OPTION 없음"

# 여기부터는 **실제로 쓸 수 있는 권한**을 본다. 역할과 멤버십이 맞아도 상속 경로가
# 끊겨 있으면 런타임은 기동에서 실패하고, 그 실패는 「권한 정상」 보고 뒤에 온다.
can_exec="$(psql_q "SELECT CASE
    WHEN has_function_privilege('$ROLE_RUNTIME','$CONFIG_FN','EXECUTE') THEN 't'
    ELSE 'f' END")"
[[ "$can_exec" == t ]] || fail "$ROLE_RUNTIME 이 $CONFIG_FN 을 실행할 수 없습니다(실제 $can_exec). apply-schema.sh 를 먼저 돌리세요"
log "실행 권한 확인: $CONFIG_FN"

can_authorize="$(psql_q "SELECT CASE
    WHEN has_function_privilege('$ROLE_RUNTIME','$ACCESS_FN','EXECUTE') THEN 't'
    ELSE 'f' END")"
[[ "$can_authorize" == t ]] || fail "$ROLE_RUNTIME 이 $ACCESS_FN 을 실행할 수 없습니다(실제 $can_authorize). apply-schema.sh 를 먼저 돌리세요"
log "실행 권한 확인: $ACCESS_FN"

for table in bot_connection bot_connection_secret employee user_identity bot_access bot_source_channel; do
  for privilege in SELECT INSERT UPDATE DELETE TRUNCATE REFERENCES TRIGGER; do
    got="$(psql_q "SELECT has_table_privilege('$ROLE_RUNTIME','public.$table','$privilege')")"
    [[ "$got" == f ]] || fail "$ROLE_RUNTIME 이 $table 에 $privilege 권한을 갖고 있습니다. 전용 함수만 허용해야 합니다"
  done
done
log "연결·권한·사용자·근거 표 직접 접근 없음"

# 위는 **쓸 수 있는가**(상속 포함), 아래는 **직접 준 적이 있는가**다. 둘 다 본다 —
# 직접 GRANT 가 남아 있으면 그룹에서 떼어 내도 권한은 그대로 남는다.
direct_grants="$(psql_q "SELECT coalesce(string_agg(grantee||':'||table_name||':'||privilege_type, ',' ORDER BY grantee),'')
                           FROM information_schema.role_table_grants
                          WHERE table_name IN ('bot_connection','bot_connection_secret','employee','user_identity','bot_access','bot_source_channel')
                            AND grantee IN ('$ROLE_RUNTIME','$ROLE_GROUP','PUBLIC')")"
[[ -z "$direct_grants" ]] || fail "VIPBot 기반 표에 직접 GRANT 가 남아 있습니다: $direct_grants"
log "VIPBot 기반 표 직접 GRANT 없음 (role_table_grants)"

function_owners="$(psql_q "SELECT string_agg(n.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||'):'||r.rolname, ',' ORDER BY p.proname)
  FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
  JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE p.oid IN ('$CONFIG_FN'::regprocedure, '$ACCESS_FN'::regprocedure)")"
expected_owners="public.vipbot_authorization(text, text):$ROLE_DEFINER,public.vipbot_connection_config(text):$ROLE_DEFINER"
[[ "$function_owners" == "$expected_owners" ]] || fail "VIPBot 함수 소유자가 다릅니다: $function_owners"
log "함수 소유자: $ROLE_DEFINER"

other_definers="$(psql_q "SELECT coalesce(string_agg(p.oid::regprocedure::text, ',' ORDER BY p.oid::regprocedure::text),'')
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.prosecdef
   AND p.oid NOT IN ('$CONFIG_FN'::regprocedure, '$ACCESS_FN'::regprocedure)
   AND has_function_privilege('$ROLE_RUNTIME', p.oid, 'EXECUTE')")"
[[ -z "$other_definers" ]] || fail "$ROLE_RUNTIME 이 다른 SECURITY DEFINER 함수를 실행할 수 있습니다: $other_definers"
log "다른 SECURITY DEFINER 함수 실행 권한 없음"

public_exec="$(psql_q "SELECT has_function_privilege('public','$CONFIG_FN','EXECUTE')")"
[[ "$public_exec" == f ]] || fail "PUBLIC 이 $CONFIG_FN 을 실행할 수 있습니다"
log "PUBLIC: $CONFIG_FN 실행 불가"

public_authorize="$(psql_q "SELECT has_function_privilege('public','$ACCESS_FN','EXECUTE')")"
[[ "$public_authorize" == f ]] || fail "PUBLIC 이 $ACCESS_FN 을 실행할 수 있습니다"
log "PUBLIC: $ACCESS_FN 실행 불가"

# 함수 ACL 을 직접 본다. PUBLIC 은 grantee oid **0** 이다.
#
# `proacl` 이 NULL 이면 「아무에게도 안 줬다」 가 아니라 **기본값**이고, 함수의
# 기본값은 `EXECUTE TO PUBLIC` 이다. 그래서 NULL 을 「PUBLIC 있음」 으로 센다 —
# 누군가 `CREATE OR REPLACE FUNCTION` 만 다시 돌리고 REVOKE 를 빠뜨리면 바로 그
# 상태가 되고, 줄을 세는 검사만 있으면 0 건으로 통과한다.
public_acl="$(psql_q "SELECT CASE WHEN p.proacl IS NULL THEN 1
                                  ELSE (SELECT count(*) FROM aclexplode(p.proacl) acl
                                         WHERE acl.grantee = 0)
                             END
                        FROM pg_proc p WHERE p.oid = '$CONFIG_FN'::regprocedure")"
[[ "$public_acl" == 0 ]] || fail "$CONFIG_FN ACL 이 PUBLIC 에 열려 있습니다(proacl NULL 이면 기본값=PUBLIC)"
log "함수 ACL: PUBLIC 줄 없음"

access_public_acl="$(psql_q "SELECT CASE WHEN p.proacl IS NULL THEN 1
                                         ELSE (SELECT count(*) FROM aclexplode(p.proacl) acl
                                                WHERE acl.grantee = 0)
                                    END
                               FROM pg_proc p WHERE p.oid = '$ACCESS_FN'::regprocedure")"
[[ "$access_public_acl" == 0 ]] || fail "$ACCESS_FN ACL 이 PUBLIC 에 열려 있습니다(proacl NULL 이면 기본값=PUBLIC)"
log "권한 함수 ACL: PUBLIC 줄 없음"

# 다른 봇의 창구도 열려 있으면 안 된다. VIPBot 런타임이 Archiver 의 연결을 읽을
# 이유가 없고, 읽을 수 있으면 그 자체가 분리가 깨진 상태다.
other_exec="$(psql_q "SELECT CASE
    WHEN has_function_privilege('$ROLE_RUNTIME','archiver_connection_config(text)','EXECUTE') THEN 't'
    ELSE 'f' END")"
[[ "$other_exec" == f ]] || fail "$ROLE_RUNTIME 이 archiver_connection_config(text) 를 실행할 수 있습니다"
log "다른 봇 창구: 실행 불가"

echo
echo "✅ $ROLE_RUNTIME 준비됨. 비밀번호와 DSN 파일은 따로 확인하세요."
echo "   DSN: /etc/tybot/$CREDENTIAL (0600, tybot 소유)"
echo "   서비스 유닛은 초안입니다(deploy/tybot-vipbot.service · [Install] 없음)."
