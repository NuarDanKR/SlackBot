-- 조직도 스냅샷 → JSONL (한 줄에 JSON 하나)
-- 실행: sqlplus -s TYBOT_RO/pw@ORCL @export_org.sql > org.jsonl
-- 요건: Oracle 12.2+ (JSON_OBJECT). 12.1 이하는 export_org_11g.sql 참조.
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

SELECT JSON_OBJECT(
         KEY 'org_code'    VALUE org_code,
         KEY 'org_name'    VALUE org_name,
         KEY 'parent_code' VALUE parent_org_code,
         KEY 'kind'        VALUE org_kind,
         KEY 'active'      VALUE CASE WHEN use_yn = 'Y' THEN 'true' ELSE 'false' END FORMAT JSON
       )
  FROM V_TYBOT_ORG
 ORDER BY org_code;

EXIT SUCCESS
