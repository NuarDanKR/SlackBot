-- VIPBot runtime authorization window.
--
-- The dedicated runtime role must not read employee, identity, grant, or source
-- tables directly. This SECURITY DEFINER function returns only a fixed decision
-- code and, for an allowed caller, the explicitly managed evidence coordinates.

BEGIN;

DROP FUNCTION IF EXISTS public.vipbot_authorization(text, text);

CREATE FUNCTION public.vipbot_authorization(
    requested_workspace text,
    requested_slack_user text
)
RETURNS TABLE (
    code text,
    emp_no text,
    grant_id bigint,
    source_workspace text,
    channel_id text
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH identity_row AS (
    SELECT ui.emp_no AS mapped_emp_no,
           e.emp_no AS employee_emp_no,
           e.active
      FROM public.user_identity ui
      LEFT JOIN public.employee e ON e.emp_no = ui.emp_no
     WHERE ui.workspace = requested_workspace
       AND ui.slack_user = requested_slack_user
), grant_row AS (
    SELECT i.mapped_emp_no,
           i.employee_emp_no,
           i.active,
           a.id AS access_id,
           a.expires_at
      FROM identity_row i
      LEFT JOIN public.bot_access a
        ON a.emp_no = i.mapped_emp_no
       AND a.bot = 'vipbot'
       AND a.revoked_at IS NULL
), decision AS (
    SELECT CASE
             WHEN g.mapped_emp_no IS NULL THEN 'no_identity'
             WHEN g.employee_emp_no IS NULL THEN 'unknown_employee'
             WHEN g.active IS NOT TRUE THEN 'inactive_employee'
             WHEN g.access_id IS NULL THEN 'no_grant'
             WHEN g.expires_at IS NOT NULL
              AND g.expires_at <= statement_timestamp() THEN 'expired'
             WHEN NOT EXISTS (
                    SELECT 1
                      FROM public.bot_source_channel s
                     WHERE s.bot = 'vipbot' AND s.removed_at IS NULL
                  ) THEN 'no_sources'
             ELSE 'allowed'
           END AS code,
           g.mapped_emp_no AS emp_no,
           g.access_id AS grant_id
      FROM (SELECT 1) singleton
      LEFT JOIN grant_row g ON true
)
SELECT d.code,
       d.emp_no,
       d.grant_id,
       CASE WHEN d.code = 'allowed' THEN s.workspace END AS source_workspace,
       CASE WHEN d.code = 'allowed' THEN s.channel_id END AS channel_id
  FROM decision d
  LEFT JOIN public.bot_source_channel s
    ON d.code = 'allowed'
   AND s.bot = 'vipbot'
   AND s.removed_at IS NULL
 ORDER BY 4 NULLS LAST, 5 NULLS LAST
$function$;

REVOKE ALL ON FUNCTION public.vipbot_authorization(text, text) FROM PUBLIC;

DO $$
DECLARE
    grantee_name text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'tybot_vipbot_definer'
    ) THEN
        RAISE EXCEPTION 'tybot_vipbot_definer role is required';
    END IF;

    REVOKE ALL PRIVILEGES ON TABLE bot_connection FROM tybot_vipbot_definer;
    REVOKE ALL PRIVILEGES ON TABLE bot_connection_secret FROM tybot_vipbot_definer;
    REVOKE ALL PRIVILEGES ON TABLE employee FROM tybot_vipbot_definer;
    REVOKE ALL PRIVILEGES ON TABLE user_identity FROM tybot_vipbot_definer;
    REVOKE ALL PRIVILEGES ON TABLE bot_access FROM tybot_vipbot_definer;
    REVOKE ALL PRIVILEGES ON TABLE bot_source_channel FROM tybot_vipbot_definer;
    GRANT SELECT ON TABLE bot_connection, bot_connection_secret,
        employee, user_identity, bot_access, bot_source_channel
        TO tybot_vipbot_definer;

    ALTER FUNCTION public.vipbot_connection_config(text)
        OWNER TO tybot_vipbot_definer;
    ALTER FUNCTION public.vipbot_authorization(text, text)
        OWNER TO tybot_vipbot_definer;

    -- CREATE OR REPLACE preserves old ACLs. The authorization function is
    -- dropped above; the older connection function must be converged here.
    FOR grantee_name IN
        SELECT r.rolname
          FROM pg_proc p
          CROSS JOIN LATERAL aclexplode(
              coalesce(p.proacl, acldefault('f', p.proowner))
          ) acl
          JOIN pg_roles r ON r.oid = acl.grantee
         WHERE p.oid = 'public.vipbot_connection_config(text)'::regprocedure
           AND acl.privilege_type = 'EXECUTE'
           AND r.rolname NOT IN ('tybot_vipbot', 'tybot_vipbot_definer')
    LOOP
        EXECUTE format(
            'REVOKE EXECUTE ON FUNCTION public.vipbot_connection_config(text) FROM %I',
            grantee_name
        );
    END LOOP;
    REVOKE ALL ON FUNCTION public.vipbot_connection_config(text) FROM PUBLIC;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_vipbot') THEN
        REVOKE ALL PRIVILEGES ON TABLE employee FROM tybot_vipbot;
        REVOKE ALL PRIVILEGES ON TABLE user_identity FROM tybot_vipbot;
        REVOKE ALL PRIVILEGES ON TABLE bot_access FROM tybot_vipbot;
        REVOKE ALL PRIVILEGES ON TABLE bot_source_channel FROM tybot_vipbot;
        GRANT EXECUTE ON FUNCTION public.vipbot_authorization(text, text)
            TO tybot_vipbot;
        GRANT EXECUTE ON FUNCTION public.vipbot_connection_config(text)
            TO tybot_vipbot;
    END IF;
END
$$;

COMMIT;
