-- 팀 일정 전용 뷰 — COVI 그룹웨어(BPROD)
--
-- 실행: TYSLACK 계정. 사전에 DBA 가 A절 권한을 준다(아래 0절).
-- 2026-08-31 · 실제 데이터를 확인하고 만들었다.
--
-- 조직·인사 뷰는 `oracle_tyslack_setup.sql` 에 있다. 이 파일은 일정만 다룬다.
--
-- ## 두 개를 만든다
--   V_TYSLACK_SCHEDULE_FOLDER — 팀 일정 폴더 ↔ **그 폴더를 볼 수 있는 부서**
--   V_TYSLACK_SCHEDULE        — 그 폴더의 일정 발생(occurrence)
--
-- **폴더 ID 를 손으로 적지 않는다.** 그룹웨어의 폴더 권한(ACL)에서 끌어온다.
-- 그래서 팀이 늘거나 폴더가 바뀌어도 코드를 고칠 일이 없다. 자세한 근거는 1절.
--
-- 폴더 목록을 따로 내보내는 이유: 받는 쪽은 "조회 범위 안에 있었는데 이번에 안 온 행"을
-- 삭제로 판정한다. 그런데 **일정이 하나도 없는 폴더**는 데이터에서 사라지므로, 폴더 목록을
-- 데이터에서 유추하면 그 폴더의 묵은 일정이 영영 지워지지 않는다.

-- ===========================================================================
-- 0. DBA 가 먼저 줘야 하는 권한
-- ===========================================================================
-- GRANT SELECT ON COVI_SMART4J.EVENT               TO TYSLACK WITH GRANT OPTION;
-- GRANT SELECT ON COVI_SMART4J.EVENT_DATE          TO TYSLACK WITH GRANT OPTION;
-- GRANT SELECT ON COVI_SMART4J.SYS_OBJECT_FOLDER   TO TYSLACK WITH GRANT OPTION;
-- GRANT SELECT ON COVI_SMART4J.SYS_OBJECT_ACL      TO TYSLACK WITH GRANT OPTION;  -- ← 추가
--
-- `SYS_OBJECT_GROUP` 은 조직 뷰를 만들 때 이미 받았다(oracle_tyslack_setup.sql A-2).
--
-- `WITH GRANT OPTION` 이 없으면 아래 3절 GRANT 에서 ORA-01720 이 난다.
-- 참석자(EVENT_ATTENDANT)·공유대상(EVENT_SHARE)·알림(EVENT_NOTIFICATION) 테이블은
-- **권한을 주지 않는다.** 가져가지 않는 값이다.

-- ===========================================================================
-- 1. 승인된 팀 일정 폴더
-- ===========================================================================
-- **폴더 ID 를 여기 적지 않는다.** 그룹웨어의 폴더 권한(ACL)에서 끌어온다.
--
-- `SYS_OBJECT_ACL` 은 폴더마다 "누가 볼 수 있는지" 를 담는다.
--   OBJECTID    = 폴더 ID
--   OBJECTTYPE  = 'FD' (폴더)
--   SUBJECTTYPE = 'GR' 부서 · 'UR' 개인 · 'CM' 커뮤니티 · 'JobTitle' 등
--   SUBJECTCODE = 그 대상의 코드. 'GR' 이면 `SYS_OBJECT_GROUP.GROUPCODE` 다
--   READ='R' / VIEW_='V' 가 허용, `_` 는 미허용
--
-- **부서(`GR` + `Dept`) 권한이 있는 폴더만** 가져온다. 이것 하나로 두 가지가 풀린다.
--
-- 1) **개인 달력이 저절로 빠진다.** 타입이 `Schedule` 인 개인 달력이 실제로 있다
--    (예: 2052 `임태종 전무`). 그 폴더의 ACL 은 개인(`UR`)·커뮤니티(`CM`)뿐이라
--    부서 조건에서 걸러진다. 타입 조건만으로는 못 막던 것이다.
-- 2) **어느 팀에 알릴지가 데이터에서 나온다.** `org_code` 가 곧 그 팀이다.
--    봇은 이 코드로 워크스페이스·공지 채널을 찾는다. 폴더를 손으로 등록하지 않는다.
--
-- 마지막 승인은 PostgreSQL 쪽 채널 등록이 맡는다. 등록되지 않은 조직으로는
-- 아무것도 발송되지 않으므로, 이 뷰가 넓어도 새어 나가지 않는다.
--
-- 2026-08-31 실측: 부서 ACL 을 가진 `Schedule` 폴더 276개.
-- 그중 향후 30일에 일정이 있는 것은 5개·25건이다.

CREATE OR REPLACE VIEW V_TYSLACK_SCHEDULE_FOLDER AS
SELECT DISTINCT
       f.FOLDERID                       AS folder_id,
       -- DISPLAYNAME 은 그대로 한국어다. 화면 쿼리가 쓰는 `MULTIDISPLAYNAME` 은
       -- 다국어용이라 `이름;;;;;;;` 처럼 구분자가 붙어 있으니 쓰지 않는다.
       f.DISPLAYNAME                    AS folder_name,
       a.SUBJECTCODE                    AS org_code,
       g.DISPLAYNAME                    AS org_name
  FROM COVI_SMART4J.SYS_OBJECT_FOLDER f
  JOIN COVI_SMART4J.SYS_OBJECT_ACL a
    ON a.OBJECTID = f.FOLDERID
   AND a.OBJECTTYPE = 'FD'
   AND a.SUBJECTTYPE = 'GR'
   AND a.READ = 'R'
   AND a.DELETEDATE IS NULL
  JOIN COVI_SMART4J.SYS_OBJECT_GROUP g
    ON g.GROUPCODE = a.SUBJECTCODE
   AND g.GROUPTYPE = 'Dept'
   AND g.ISUSE = 'Y'
 WHERE f.FOLDERTYPE = 'Schedule'
   AND f.MENUID = 7                    -- 일정 메뉴
   AND f.ISUSE = 'Y'
   AND f.ISDISPLAY = 'Y'
   AND f.DELETEDATE IS NULL;

-- ===========================================================================
-- 2. 일정 발생
-- ===========================================================================
-- 반복 일정은 `EVENT_DATE` 에 이미 펼쳐져 있다(한 일정당 최대 999회, 2274년까지 확인).
-- 그래서 반복 규칙을 해석할 필요 없이 기간으로 자르면 된다.
--
-- 제외하는 것
--   FOLDERTYPE <> 'Schedule'  — 개인 일정(`Schedule.Person`), 회의실 예약(`Resource`)
--   ISPUBLIC = 'N'            — 비공개로 표시한 일정
--   ISDISPLAY = 'N'           — 달력에 감춘 일정
--   DELETEDATE IS NOT NULL    — 삭제된 일정
--
-- 내보내지 않는 컬럼
--   DESCRIPTION(설명 본문) · 참석자 · 공유 대상 · 첨부
--   제목과 장소는 Slack 알림에 필요해서 넣지만, MD 아카이브에는 들어가지 않는다.
--
-- 시각은 원본이 `'YYYY-MM-DD HH24:MI'` 문자열(16자)이다. 형식이 ISO 와 같은 순서라
-- 문자열 비교로 기간을 잘라도 정확하고, 인덱스 `STR_END(STARTDATETIME, ENDDATETIME)` 를 탄다.
-- **`TO_DATE` 로 바꿔서 비교하지 말 것** — 인덱스를 못 쓰고 전체 스캔이 된다.
--
-- PLACE 는 CLOB 이라 그대로 내보내면 클라이언트가 LOB 을 다뤄야 한다.
-- 실측 최대 31자라 넉넉히 잘라 VARCHAR2 로 만든다.

CREATE OR REPLACE VIEW V_TYSLACK_SCHEDULE AS
SELECT e.FOLDERID                            AS folder_id,
       e.EVENTID                             AS event_id,
       d.DATEID                              AS occurrence_id,
       e.SUBJECT                             AS title,
       CAST(SUBSTR(e.PLACE, 1, 200) AS VARCHAR2(200))  AS place,
       d.STARTDATETIME                       AS starts_at,
       d.ENDDATETIME                         AS ends_at,
       d.ISALLDAY                            AS all_day_yn,
       NVL(d.ISREPEAT, 'N')                  AS repeat_yn,
       -- 변경 판정용. 수정된 적이 없으면 등록 시각을 쓴다.
       TO_CHAR(NVL(e.MODIFYDATE, e.REGISTDATE), 'YYYY-MM-DD HH24:MI:SS') AS updated_at
  FROM COVI_SMART4J.EVENT e
  JOIN COVI_SMART4J.EVENT_DATE d
    ON d.EVENTID = e.EVENTID
 WHERE e.FOLDERTYPE = 'Schedule'
   AND e.DELETEDATE IS NULL
   AND e.ISPUBLIC = 'Y'
   AND e.ISDISPLAY = 'Y'
   -- 초대받아 생긴 복사본과 참석 응답 행을 뺀다. 그대로 두면 같은 회의가
   -- 사람 수만큼 중복돼 나온다. 화면 쿼리도 같은 것을 걸러낸다.
   -- (승인 폴더에서는 2026-08-31 기준 0건이지만, 폴더가 늘면 생길 수 있다)
   AND e.LINKEVENTID IS NULL
   AND NVL(e.EVENTTYPE, 'X') <> 'A'
   -- 폴더 목록은 ACL 에서 나온다. 한 폴더에 부서가 여럿이면 뷰에 여러 행이 되므로
   -- DISTINCT 로 좁힌다. 여기서 조인하면 일정이 부서 수만큼 중복된다.
   AND e.FOLDERID IN (SELECT DISTINCT folder_id FROM V_TYSLACK_SCHEDULE_FOLDER);

-- ===========================================================================
-- 3. 봇 계정에 열어 준다
-- ===========================================================================
GRANT SELECT ON V_TYSLACK_SCHEDULE_FOLDER TO TYSLACK_BOT;
GRANT SELECT ON V_TYSLACK_SCHEDULE        TO TYSLACK_BOT;

-- ===========================================================================
-- 4. 만든 뒤 확인
-- ===========================================================================
-- 폴더·부서 매핑이 나오는가:
--   SELECT count(DISTINCT folder_id), count(*) FROM V_TYSLACK_SCHEDULE_FOLDER;
--
-- 개인 달력이 안 들어왔는가 (**0 이어야 한다**):
--   SELECT count(*) FROM V_TYSLACK_SCHEDULE_FOLDER WHERE folder_id = 2052;
--
-- 향후 48시간 건수:
--   SELECT count(*) FROM V_TYSLACK_SCHEDULE
--    WHERE starts_at BETWEEN TO_CHAR(SYSDATE, 'YYYY-MM-DD HH24:MI')
--                        AND TO_CHAR(SYSDATE + 2, 'YYYY-MM-DD HH24:MI');
--
-- 개인 일정이 새어 들어오지 않았는가 (**0 이어야 한다**):
--   SELECT count(*) FROM V_TYSLACK_SCHEDULE s
--    WHERE EXISTS (SELECT 1 FROM COVI_SMART4J.EVENT e
--                   WHERE e.EVENTID = s.event_id
--                     AND e.FOLDERTYPE <> 'Schedule');
--
-- 봇 계정으로 접속해 원본이 막혀 있는가 (**ORA-00942 여야 정상**):
--   SELECT count(*) FROM COVI_SMART4J.EVENT;
--   SELECT count(*) FROM COVI_SMART4J.EVENT_ATTENDANT;
