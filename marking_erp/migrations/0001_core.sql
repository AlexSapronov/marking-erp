CREATE TABLE organizations (
    id uuid PRIMARY KEY,
    inn text NOT NULL UNIQUE CHECK (inn ~ '^([0-9]{10}|[0-9]{12})$'),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE groups (
    slug text PRIMARY KEY CHECK (slug IN ('bm','logistics','warehouse','tsd','specialist','administrator')),
    name text NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0)
);
CREATE TABLE permissions (
    id text PRIMARY KEY,
    administrative boolean NOT NULL,
    description text NOT NULL DEFAULT ''
);
CREATE TABLE group_grants (
    group_slug text NOT NULL REFERENCES groups(slug) ON DELETE RESTRICT,
    permission_id text NOT NULL REFERENCES permissions(id) ON DELETE RESTRICT,
    scope text NOT NULL CHECK (scope IN ('none','own','assigned','all')),
    PRIMARY KEY (group_slug, permission_id)
);
CREATE TABLE users (
    id uuid PRIMARY KEY,
    username text NOT NULL UNIQUE CHECK (username = lower(username) AND username ~ '^[a-z0-9][a-z0-9_.@-]{2,79}$'),
    display_name text NOT NULL CHECK (length(btrim(display_name)) > 0),
    password_hash text NOT NULL CHECK (length(password_hash) > 0),
    group_slug text NOT NULL REFERENCES groups(slug) ON DELETE RESTRICT,
    enabled boolean NOT NULL DEFAULT true,
    auth_version bigint NOT NULL DEFAULT 1 CHECK (auth_version > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE user_organizations (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    PRIMARY KEY (user_id, organization_id)
);
CREATE TABLE audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id uuid REFERENCES users(id) ON DELETE RESTRICT,
    action text NOT NULL,
    entity text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE seed_state (
    key text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION reject_audit_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit events are append-only';
END;
$$;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change();
CREATE FUNCTION validate_specialist_grant() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE is_admin boolean;
BEGIN
    IF NEW.group_slug = 'specialist' AND NEW.scope <> 'none' THEN
        SELECT administrative INTO is_admin FROM permissions WHERE id=NEW.permission_id FOR SHARE;
        IF is_admin THEN
            RAISE EXCEPTION 'specialist cannot hold administrative permissions';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER specialist_grants BEFORE INSERT OR UPDATE ON group_grants
    FOR EACH ROW EXECUTE FUNCTION validate_specialist_grant();
CREATE FUNCTION validate_permission_classification() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.administrative AND EXISTS (
        SELECT 1 FROM group_grants WHERE permission_id=NEW.id AND group_slug='specialist' AND scope<>'none'
    ) THEN
        RAISE EXCEPTION 'reclassification would grant administrative access to specialist';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER permission_classification BEFORE UPDATE OF administrative ON permissions
    FOR EACH ROW EXECUTE FUNCTION validate_permission_classification();

