-- Direct-user calendar ACLs are for personal DM only, never department membership.
BEGIN;

ALTER TABLE schedule_folder ADD COLUMN IF NOT EXISTS dm_only boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS schedule_folder_person (
    source_folder_id bigint NOT NULL REFERENCES schedule_folder(source_folder_id),
    emp_no text NOT NULL REFERENCES employee(emp_no),
    read_allowed boolean NOT NULL,
    is_present boolean NOT NULL DEFAULT true,
    source_generated_at timestamptz NOT NULL,
    PRIMARY KEY (source_folder_id, emp_no)
);

CREATE TABLE IF NOT EXISTS schedule_personal_acl_state (
    source text PRIMARY KEY CHECK (source = 'oracle-ur-v1'),
    generated_at timestamptz NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        GRANT SELECT, INSERT, UPDATE ON TABLE schedule_folder_person, schedule_personal_acl_state TO tyslackai;
    END IF;
END $$;

COMMIT;
