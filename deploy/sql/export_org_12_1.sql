-- 조직도 스냅샷 → JSONL (Oracle 12.1 용)
--
-- 실행:
--   export NLS_LANG=KOREAN_KOREA.AL32UTF8
--   sqlplus -s TYSLACK_BOT/<비밀번호>@<호스트>:1523/BPROD @export_org_12_1.sql > org.jsonl
--
-- ## 왜 이 파일이 따로 있나
-- `export_org.sql` 은 `JSON_OBJECT` 를 쓰는데 그 함수는 **Oracle 12.2 에서 추가**됐다.
-- 12.1 에서는 없는 함수라 그대로 돌리면 ORA-00904 로 실패한다.
-- 그래서 문자열을 직접 이어 붙여 JSON 을 만든다.
--
-- ## 직접 이어 붙일 때 반드시 지킬 것 — 이스케이프
-- 조직명에 큰따옴표나 역슬래시가 하나만 들어가도 JSON 이 깨지고, 그 줄만 조용히 버려진다.
-- 그래서 아래 순서로 바꾼다(**순서를 바꾸면 안 된다**).
--   1) 역슬래시  \  →  \\      ← 반드시 먼저. 나중에 하면 2)가 넣은 역슬래시까지 또 바꾼다
--   2) 큰따옴표  "  →  \"
--   3) 개행·탭   → 공백        ← JSONL 은 한 줄에 하나이므로 줄바꿈이 섞이면 안 된다
--
-- NULL 은 빈 문자열이 아니라 JSON 의 null 로 내보낸다. 상위 조직이 없는 최상위 조직을
-- 빈 문자열로 만들면 트리가 끊긴 것인지 최상위인지 구분할 수 없다.

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

SELECT '{"org_code":'
       || CASE WHEN org_code IS NULL THEN 'null' ELSE
            '"' || REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
              org_code, '\', '\\'), '"', '\"'), CHR(13), ' '), CHR(10), ' '), CHR(9), ' ') || '"'
          END
       || ',"org_name":'
       || CASE WHEN org_name IS NULL THEN 'null' ELSE
            '"' || REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
              org_name, '\', '\\'), '"', '\"'), CHR(13), ' '), CHR(10), ' '), CHR(9), ' ') || '"'
          END
       || ',"parent_code":'
       || CASE WHEN parent_org_code IS NULL THEN 'null' ELSE
            '"' || REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
              parent_org_code, '\', '\\'), '"', '\"'), CHR(13), ' '), CHR(10), ' '), CHR(9), ' ') || '"'
          END
       || ',"kind":'
       || CASE WHEN org_kind IS NULL THEN 'null' ELSE
            '"' || REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
              org_kind, '\', '\\'), '"', '\"'), CHR(13), ' '), CHR(10), ' '), CHR(9), ' ') || '"'
          END
       || ',"company_code":'
       || CASE WHEN company_code IS NULL THEN 'null' ELSE
            '"' || REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
              company_code, '\', '\\'), '"', '\"'), CHR(13), ' '), CHR(10), ' '), CHR(9), ' ') || '"'
          END
       || ',"active":'
       || CASE WHEN use_yn = 'Y' THEN 'true' ELSE 'false' END
       || '}'
  FROM TYSLACK.V_TYSLACK_ORG
 ORDER BY org_code;

EXIT SUCCESS
