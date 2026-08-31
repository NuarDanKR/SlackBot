-- 인사기본 스냅샷 → JSONL (Oracle 12.1 용)
--
-- 실행:
--   export NLS_LANG=KOREAN_KOREA.AL32UTF8
--   sqlplus -s TYSLACK_BOT/<비밀번호>@<호스트>:1523/BPROD @export_emp_12_1.sql > emp.jsonl
--
-- 12.1 에는 `JSON_OBJECT` 가 없어서 문자열을 직접 이어 붙인다.
-- 이스케이프 순서와 NULL 처리 이유는 `export_org_12_1.sql` 상단 설명 참조.
--
-- ## 뷰에 무엇이 들어 있는지 먼저 확인한다
-- `TYSLACK.V_TYSLACK_EMP` 에 주민번호·연락처·주소·급여·인사평가 컬럼이 있으면 안 된다.
-- 이 스크립트는 뷰가 이미 걸러져 있다고 전제한다 — 뷰를 만들 때 컬럼을 화이트리스트로 고른다.
--
-- 이메일은 소문자로 내보낸다. Slack 프로필 이메일과 맞출 때 대소문자가 어긋나면
-- 같은 사람이 다른 사람으로 잡힌다.

SET PAGESIZE 0
SET FEEDBACK OFF
SET HEADING OFF
SET ECHO OFF
SET VERIFY OFF
SET TERMOUT OFF
SET TRIMSPOOL ON
SET TRIMOUT ON
SET LINESIZE 32767
SET LONG 100000000
SET LONGCHUNKSIZE 100000
SET NEWPAGE NONE
SET SQLBLANKLINES ON
WHENEVER SQLERROR EXIT FAILURE
WHENEVER OSERROR EXIT FAILURE

SELECT '{"emp_no":'
       || CASE WHEN emp_no IS NULL THEN 'null' ELSE
            '"' || REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
              emp_no, '\', '\\'), '"', '\"'), CHR(13), ' '), CHR(10), ' '), CHR(9), ' ') || '"'
          END
       || ',"name":'
       || CASE WHEN emp_name IS NULL THEN 'null' ELSE
            '"' || REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
              emp_name, '\', '\\'), '"', '\"'), CHR(13), ' '), CHR(10), ' '), CHR(9), ' ') || '"'
          END
       || ',"email":'
       || CASE WHEN email IS NULL THEN 'null' ELSE
            '"' || LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
              email, '\', '\\'), '"', '\"'), CHR(13), ' '), CHR(10), ' '), CHR(9), ' ')) || '"'
          END
       || ',"org_code":'
       || CASE WHEN org_code IS NULL THEN 'null' ELSE
            '"' || REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
              org_code, '\', '\\'), '"', '\"'), CHR(13), ' '), CHR(10), ' '), CHR(9), ' ') || '"'
          END
       || ',"position":'
       || CASE WHEN position_name IS NULL THEN 'null' ELSE
            '"' || REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
              position_name, '\', '\\'), '"', '\"'), CHR(13), ' '), CHR(10), ' '), CHR(9), ' ') || '"'
          END
       || ',"active":'
       || CASE WHEN use_yn = 'Y' THEN 'true' ELSE 'false' END
       || '}'
  FROM TYSLACK.V_TYSLACK_EMP
 ORDER BY emp_no;

EXIT SUCCESS
