-- Run as view owner TYSLACK after review. Never grant base tables to TYSLACK_BOT.
-- DBA prerequisite for the owner (the other base-table grants already exist):
-- GRANT SELECT ON COVI_SMART4J.SYS_OBJECT_GROUP_MEMBER TO TYSLACK WITH GRANT OPTION;
-- Non-readable rows in an applicable active group conservatively block a direct
-- grant; this is not an implementation of the vendor's full ACL inheritance engine.
-- Existing department-only views remain unchanged for public consumers.
CREATE OR REPLACE VIEW V_TYSLACK_SCHEDULE_USER_ACL AS
SELECT f.FOLDERID AS folder_id,
       f.DISPLAYNAME AS folder_name,
       u.USERCODE AS emp_no,
       CASE WHEN MIN(CASE WHEN TRIM(a.READ) = 'R' AND NOT EXISTS (
           SELECT 1 FROM COVI_SMART4J.SYS_OBJECT_ACL denied
           JOIN COVI_SMART4J.SYS_OBJECT_GROUP_MEMBER member_
             ON member_.GROUPCODE=denied.SUBJECTCODE AND member_.USERCODE=u.USERCODE
            AND member_.ISUSE='Y' AND member_.MEMBERSTATUS='A'
            AND (member_.JOINDATE IS NULL OR member_.JOINDATE<=SYSDATE)
            AND (member_.LEAVEDATE IS NULL OR member_.LEAVEDATE>SYSDATE)
            AND (member_.EXPIREDATE IS NULL OR member_.EXPIREDATE>SYSDATE)
           JOIN COVI_SMART4J.SYS_OBJECT_GROUP group_
             ON group_.GROUPCODE=member_.GROUPCODE AND group_.ISUSE='Y'
           WHERE denied.OBJECTID=f.FOLDERID AND denied.OBJECTTYPE='FD'
             AND denied.SUBJECTTYPE<>'UR' AND denied.DELETEDATE IS NULL
             AND NVL(TRIM(denied.READ),'_')<>'R'
       ) THEN 1 ELSE 0 END) = 1
            THEN 1 ELSE 0 END AS read_allowed
  FROM COVI_SMART4J.SYS_OBJECT_FOLDER f
  JOIN COVI_SMART4J.SYS_OBJECT_ACL a
    ON a.OBJECTID=f.FOLDERID AND a.OBJECTTYPE='FD'
   AND a.SUBJECTTYPE='UR' AND a.DELETEDATE IS NULL
  JOIN COVI_SMART4J.SYS_OBJECT_USER u
    ON u.USERCODE=a.SUBJECTCODE AND u.ISHR='Y' AND u.ISUSE='Y'
 WHERE f.FOLDERTYPE IN ('Schedule', 'Schedule.Person')
   AND f.MENUID=7 AND f.ISUSE='Y' AND f.ISDISPLAY='Y' AND f.DELETEDATE IS NULL
 GROUP BY f.FOLDERID,f.DISPLAYNAME,u.USERCODE;

-- Preserve the existing event-level restrictions. A folder grant does not override
-- a private event, hidden event, invitation copy, or attendee-response row.
CREATE OR REPLACE VIEW V_TYSLACK_SCHEDULE_DM AS
SELECT folder_id,event_id,occurrence_id,title,place,starts_at,ends_at,
       all_day_yn,repeat_yn,updated_at
  FROM V_TYSLACK_SCHEDULE
UNION
SELECT e.FOLDERID,e.EVENTID,d.DATEID,e.SUBJECT,
       CAST(SUBSTR(e.PLACE,1,200) AS VARCHAR2(200)),
       d.STARTDATETIME,d.ENDDATETIME,d.ISALLDAY,NVL(d.ISREPEAT,'N'),
       TO_CHAR(NVL(e.MODIFYDATE,e.REGISTDATE),'YYYY-MM-DD HH24:MI:SS')
  FROM COVI_SMART4J.EVENT e
  JOIN COVI_SMART4J.EVENT_DATE d ON d.EVENTID=e.EVENTID
 WHERE e.FOLDERTYPE IN ('Schedule', 'Schedule.Person')
   AND e.DELETEDATE IS NULL AND e.ISPUBLIC='Y' AND e.ISDISPLAY='Y'
   AND e.LINKEVENTID IS NULL AND NVL(e.EVENTTYPE,'X')<>'A'
   AND EXISTS (SELECT 1 FROM V_TYSLACK_SCHEDULE_USER_ACL a
               WHERE a.folder_id=e.FOLDERID AND a.read_allowed=1);

GRANT SELECT ON V_TYSLACK_SCHEDULE_USER_ACL TO TYSLACK_BOT;
GRANT SELECT ON V_TYSLACK_SCHEDULE_DM TO TYSLACK_BOT;
