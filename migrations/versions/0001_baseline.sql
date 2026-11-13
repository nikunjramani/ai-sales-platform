-- Baseline schema (ADR 0026 step 4): every table, index, policy, trigger, function and
-- grant the former migrations 0001-0015 created, as of 0015_orders. Generated once from
-- pg_dump --schema-only; edit only through new migrations.
SET LOCAL check_function_bodies = false;

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE FUNCTION public.accept_invitation(p_token_sha256 text, p_user_id uuid) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
DECLARE
    v_subject text := NULLIF(current_setting('app.subject', true), '');
    v_previous text := current_setting('app.tenant_id', true);
    v_previous_token text := current_setting('app.account_token_sha256', true);
    v_tenant uuid;
    v_invitation invitations%ROWTYPE;
BEGIN
    PERFORM set_config('app.account_token_sha256', COALESCE(p_token_sha256, ''), true);
    SELECT tenant_id INTO v_tenant FROM invitations WHERE token_sha256 = p_token_sha256;
    PERFORM set_config('app.account_token_sha256', COALESCE(v_previous_token, ''), true);
    IF v_tenant IS NULL OR v_subject IS NULL THEN
        RAISE EXCEPTION 'invitation not usable' USING ERRCODE = 'QA001';
    END IF;
    PERFORM set_config('app.tenant_id', v_tenant::text, true);
    PERFORM 1 FROM tenants WHERE id = v_tenant AND status <> 'suspended' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation not usable' USING ERRCODE = 'QA001';
    END IF;
    SELECT * INTO v_invitation FROM invitations
    WHERE tenant_id = v_tenant AND token_sha256 = p_token_sha256 FOR UPDATE;
    IF v_invitation.accepted_at IS NOT NULL OR v_invitation.revoked_at IS NOT NULL
       OR v_invitation.expires_at IS NULL OR v_invitation.expires_at <= now()
       OR NOT EXISTS (
           SELECT 1 FROM users WHERE id = p_user_id AND subject = v_subject
           AND disabled_at IS NULL AND email_normalized = v_invitation.email_normalized
       ) THEN
        RAISE EXCEPTION 'invitation not usable' USING ERRCODE = 'QA001';
    END IF;
    -- The link in the email proves control of the address.
    UPDATE users SET email_verified_at = COALESCE(email_verified_at, now()), updated_at = now()
    WHERE id = p_user_id;
    INSERT INTO memberships (id, tenant_id, subject, role)
    SELECT gen_random_uuid(), v_tenant, v_subject, role FROM unnest(v_invitation.roles) AS role
    ON CONFLICT (tenant_id, subject, role) DO NOTHING;
    UPDATE invitations SET accepted_at = now(), accepted_by = v_subject
    WHERE tenant_id = v_tenant AND id = v_invitation.id;
    INSERT INTO admin_events (id, tenant_id, actor_subject, action, target, details)
    VALUES (gen_random_uuid(), v_tenant, v_subject, 'invitation_accepted', p_user_id::text,
            jsonb_build_object('invitation', v_invitation.id, 'roles', v_invitation.roles));
    PERFORM set_config('app.tenant_id', COALESCE(v_previous, ''), true);
    RETURN v_tenant;
END $$;

CREATE FUNCTION public.change_member_roles(p_tenant uuid, p_user_id uuid, p_roles text[]) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
DECLARE

    v_subject text := NULLIF(current_setting('app.subject', true), '');
    v_previous text := current_setting('app.tenant_id', true);
    v_target text;
    v_old text[];
BEGIN
    IF v_subject IS NULL THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('app.tenant_id', p_tenant::text, true);
    PERFORM 1 FROM tenants WHERE id = p_tenant AND status <> 'suspended' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;
    SELECT u.subject INTO v_target FROM users u WHERE u.id = p_user_id;
    SELECT array_agg(role ORDER BY role) INTO v_old FROM memberships
    WHERE tenant_id = p_tenant AND subject = v_target;
    IF v_target IS NULL OR v_old IS NULL THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM memberships WHERE tenant_id = p_tenant AND subject = v_subject AND role = 'administrator') THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;
    IF p_roles IS NULL OR cardinality(p_roles) < 1
       OR NOT (p_roles <@ ARRAY['administrator', 'sales_operator', 'approver', 'viewer']::text[]) THEN
        RAISE EXCEPTION 'invalid roles' USING ERRCODE = '22023';
    END IF;
    DELETE FROM memberships
    WHERE tenant_id = p_tenant AND subject = v_target AND NOT (role = ANY (p_roles));
    INSERT INTO memberships (id, tenant_id, subject, role)
    SELECT gen_random_uuid(), p_tenant, v_target, role FROM unnest(p_roles) AS role
    ON CONFLICT (tenant_id, subject, role) DO NOTHING;

    IF NOT EXISTS (
        SELECT 1 FROM memberships WHERE tenant_id = p_tenant AND role = 'administrator'
    ) THEN
        RAISE EXCEPTION 'last administrator' USING ERRCODE = 'QA002';
    END IF;

    INSERT INTO admin_events (id, tenant_id, actor_subject, action, target, details)
    VALUES (gen_random_uuid(), p_tenant, v_subject, 'member_roles_changed', p_user_id::text,
            jsonb_build_object('old', v_old,
                               'new', (SELECT array_agg(DISTINCT r ORDER BY r)
                                       FROM unnest(p_roles) AS r)));
    PERFORM set_config('app.tenant_id', COALESCE(v_previous, ''), true);
END $$;

CREATE FUNCTION public.create_business(p_user_id uuid, p_name text, p_country text, p_time_zone text, p_currency text, p_tax_rate numeric, p_quote_validity_days integer, p_catalogue_provider text DEFAULT NULL::text, p_catalogue_name text DEFAULT NULL::text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $_$
DECLARE
    v_subject text := NULLIF(current_setting('app.subject', true), '');
    v_previous text := current_setting('app.tenant_id', true);
    v_tenant uuid := gen_random_uuid();  -- never a parameter: cannot target an existing business
    v_name text := btrim(p_name);
    v_connection uuid := gen_random_uuid();
BEGIN
    IF v_subject IS NULL OR NOT EXISTS (
        SELECT 1 FROM users WHERE id = p_user_id AND subject = v_subject
        AND email_verified_at IS NOT NULL AND disabled_at IS NULL
    ) THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;
    IF v_name IS NULL OR length(v_name) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION 'invalid business name' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = p_time_zone) THEN
        RAISE EXCEPTION 'unknown time zone' USING ERRCODE = '22023';
    END IF;
    PERFORM set_config('app.tenant_id', v_tenant::text, true);
    INSERT INTO tenants (id, name, status, country, time_zone, created_by)
    VALUES (v_tenant, v_name, 'setting_up', p_country, p_time_zone, v_subject);
    INSERT INTO memberships (id, tenant_id, subject, role) VALUES
        (gen_random_uuid(), v_tenant, v_subject, 'administrator'),
        (gen_random_uuid(), v_tenant, v_subject, 'approver');
    INSERT INTO tenant_pricing_policies
        (tenant_id, version, currency, tax_rate, quote_validity_days, updated_by)
    VALUES (v_tenant, 1, p_currency, p_tax_rate, p_quote_validity_days, v_subject);
    INSERT INTO tenant_profiles (tenant_id) VALUES (v_tenant);
    INSERT INTO admin_events (id, tenant_id, actor_subject, action, details)
    VALUES (gen_random_uuid(), v_tenant, v_subject, 'business_created',
            jsonb_build_object('name', v_name));
    IF p_catalogue_provider IS NOT NULL THEN
        IF p_catalogue_provider !~ '^[a-z][a-z0-9_]{1,31}$' THEN
            RAISE EXCEPTION 'invalid provider key' USING ERRCODE = '22023';
        END IF;
        INSERT INTO connections
            (id, tenant_id, provider_key, display_name, purposes, status, created_by)
        VALUES (v_connection, v_tenant, p_catalogue_provider,
                left(COALESCE(NULLIF(btrim(p_catalogue_name), ''), p_catalogue_provider), 100),
                ARRAY['catalogue'], 'active', v_subject);
        INSERT INTO connection_events (id, tenant_id, connection_id, kind, actor, detail)
        VALUES (gen_random_uuid(), v_tenant, v_connection, 'created', v_subject,
                jsonb_build_object('reason', 'default product source'));
    END IF;
    PERFORM set_config('app.tenant_id', COALESCE(v_previous, ''), true);
    RETURN v_tenant;
END $_$;

CREATE FUNCTION public.guard_enquiry_reopen() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF current_user = 'sales_app' AND NEW.status IS DISTINCT FROM OLD.status
               AND NOT (OLD.status = 'needs_clarification' AND NEW.status = 'received') THEN
                RAISE EXCEPTION 'enquiry status change not allowed for this role'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;

CREATE FUNCTION public.guard_tenant_status() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN IF NEW.status IS DISTINCT FROM OLD.status AND NOT (OLD.status = 'setting_up' AND NEW.status = 'active') AND current_user <> 'sales_owner' THEN RAISE EXCEPTION 'This status change is not allowed' USING ERRCODE = '42501'; END IF; RETURN NEW; END $$;

CREATE FUNCTION public.reject_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME; END $$;

CREATE FUNCTION public.remove_member(p_tenant uuid, p_user_id uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
DECLARE

    v_subject text := NULLIF(current_setting('app.subject', true), '');
    v_previous text := current_setting('app.tenant_id', true);
    v_target text;
    v_old text[];
BEGIN
    IF v_subject IS NULL THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('app.tenant_id', p_tenant::text, true);
    PERFORM 1 FROM tenants WHERE id = p_tenant AND status <> 'suspended' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;
    SELECT u.subject INTO v_target FROM users u WHERE u.id = p_user_id;
    SELECT array_agg(role ORDER BY role) INTO v_old FROM memberships
    WHERE tenant_id = p_tenant AND subject = v_target;
    IF v_target IS NULL OR v_old IS NULL THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;

    -- Administrators may remove anyone; everyone may leave (Q22).
    IF v_target <> v_subject AND NOT EXISTS (SELECT 1 FROM memberships WHERE tenant_id = p_tenant AND subject = v_subject AND role = 'administrator') THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;
    DELETE FROM memberships WHERE tenant_id = p_tenant AND subject = v_target;

    IF NOT EXISTS (
        SELECT 1 FROM memberships WHERE tenant_id = p_tenant AND role = 'administrator'
    ) THEN
        RAISE EXCEPTION 'last administrator' USING ERRCODE = 'QA002';
    END IF;

    -- A removed person no longer receives approval requests on their linked phones.
    UPDATE channel_bindings SET revoked_at = now()
    WHERE tenant_id = p_tenant AND subject = v_target AND revoked_at IS NULL;
    INSERT INTO admin_events (id, tenant_id, actor_subject, action, target, details)
    VALUES (gen_random_uuid(), p_tenant, v_subject, 'member_removed', p_user_id::text,
            jsonb_build_object('old', v_old));
    PERFORM set_config('app.tenant_id', COALESCE(v_previous, ''), true);
END $$;

CREATE TABLE public.account_notices (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    email_normalized character varying(254) NOT NULL,
    purpose character varying(32) NOT NULL,
    email_status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_account_notices_email_status CHECK (((email_status)::text = ANY ((ARRAY['pending'::character varying, 'sending'::character varying, 'sent'::character varying, 'failed'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT ck_account_notices_purpose CHECK (((purpose)::text = ANY ((ARRAY['account_exists'::character varying, 'password_changed'::character varying])::text[])))
);

ALTER TABLE ONLY public.account_notices FORCE ROW LEVEL SECURITY;

CREATE TABLE public.admin_events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    actor_subject character varying(200) NOT NULL,
    action character varying(48) NOT NULL,
    target character varying(200),
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_admin_events_action CHECK (((action)::text = ANY ((ARRAY['business_created'::character varying, 'went_live'::character varying, 'status_changed'::character varying, 'details_changed'::character varying, 'profile_changed'::character varying, 'logo_changed'::character varying, 'pricing_changed'::character varying, 'member_invited'::character varying, 'invitation_resent'::character varying, 'invitation_revoked'::character varying, 'invitation_accepted'::character varying, 'member_roles_changed'::character varying, 'member_removed'::character varying, 'api_key_created'::character varying, 'api_key_revoked'::character varying, 'contacts_merged'::character varying, 'customer_messages_changed'::character varying, 'order_settings_changed'::character varying])::text[])))
);

ALTER TABLE ONLY public.admin_events FORCE ROW LEVEL SECURITY;

CREATE TABLE public.api_keys (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    role character varying(32) NOT NULL,
    secret_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    created_by character varying(200),
    revoked_by character varying(200),
    CONSTRAINT ck_api_keys_role CHECK (((role)::text = ANY ((ARRAY['administrator'::character varying, 'sales_operator'::character varying, 'approver'::character varying, 'viewer'::character varying, 'intake_service'::character varying])::text[])))
);

ALTER TABLE ONLY public.api_keys FORCE ROW LEVEL SECURITY;

CREATE TABLE public.approval_action_refs (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    task_id uuid NOT NULL,
    binding_id uuid NOT NULL,
    token_sha256 character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    sent_at timestamp with time zone,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.approval_action_refs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.approval_decisions (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    task_id uuid NOT NULL,
    command_id uuid NOT NULL,
    quotation_id uuid NOT NULL,
    version integer NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    action character varying(32) NOT NULL,
    actor_subject character varying(200) NOT NULL,
    reason character varying(1000),
    transport character varying(32) NOT NULL,
    evidence_ref character varying(300),
    received_at timestamp with time zone NOT NULL,
    decided_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_approval_decisions_action CHECK (((action)::text = ANY ((ARRAY['approve'::character varying, 'reject'::character varying, 'request_revision'::character varying])::text[])))
);

ALTER TABLE ONLY public.approval_decisions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.approval_reply_prompts (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    reference_id uuid NOT NULL,
    kind character varying(16) NOT NULL,
    action character varying(32) NOT NULL,
    reason text,
    token_sha256 character varying(64),
    outbound_message_id uuid,
    inbox_event_id uuid,
    expires_at timestamp with time zone NOT NULL,
    closed_at timestamp with time zone,
    outcome character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_approval_reply_prompts_action CHECK (((action)::text = ANY ((ARRAY['approve'::character varying, 'reject'::character varying, 'request_revision'::character varying])::text[]))),
    CONSTRAINT ck_approval_reply_prompts_closed CHECK (((closed_at IS NULL) = (outcome IS NULL))),
    CONSTRAINT ck_approval_reply_prompts_kind CHECK (((kind)::text = ANY ((ARRAY['reason'::character varying, 'confirm'::character varying])::text[]))),
    CONSTRAINT ck_approval_reply_prompts_reason CHECK (((reason IS NULL) OR (length(reason) <= 1000)))
);

ALTER TABLE ONLY public.approval_reply_prompts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.approval_tasks (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    quotation_id uuid NOT NULL,
    version_id uuid NOT NULL,
    version integer NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    status character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    CONSTRAINT ck_approval_tasks_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'rejected'::character varying, 'revision_requested'::character varying])::text[])))
);

ALTER TABLE ONLY public.approval_tasks FORCE ROW LEVEL SECURITY;

CREATE TABLE public.attachments (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    enquiry_id uuid NOT NULL,
    filename character varying(255) NOT NULL,
    declared_content_type character varying(100) NOT NULL,
    size_bytes integer NOT NULL,
    sha256 character varying(64) NOT NULL,
    status character varying(16) NOT NULL,
    detected_content_type character varying(100),
    rejection_reason character varying(50),
    storage_key character varying(300) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_attachments_status CHECK (((status)::text = ANY ((ARRAY['quarantined'::character varying, 'clean'::character varying, 'rejected'::character varying])::text[])))
);

ALTER TABLE ONLY public.attachments FORCE ROW LEVEL SECURITY;

CREATE TABLE public.catalogue_categories (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    archived_at timestamp with time zone,
    row_version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(200) NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(200) NOT NULL,
    CONSTRAINT ck_catalogue_categories_name CHECK ((btrim((name)::text) <> ''::text)),
    CONSTRAINT ck_catalogue_categories_row_version CHECK ((row_version >= 1))
);

ALTER TABLE ONLY public.catalogue_categories FORCE ROW LEVEL SECURITY;

CREATE TABLE public.catalogue_changes (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    entity character varying(16) NOT NULL,
    entity_id uuid NOT NULL,
    action character varying(16) NOT NULL,
    changes jsonb NOT NULL,
    actor character varying(200) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_catalogue_changes_action CHECK (((action)::text = ANY ((ARRAY['created'::character varying, 'updated'::character varying, 'archived'::character varying, 'restored'::character varying, 'deleted'::character varying])::text[]))),
    CONSTRAINT ck_catalogue_changes_entity CHECK (((entity)::text = ANY ((ARRAY['product'::character varying, 'category'::character varying])::text[])))
);

ALTER TABLE ONLY public.catalogue_changes FORCE ROW LEVEL SECURITY;

CREATE TABLE public.catalogue_prices (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    product_id uuid NOT NULL,
    price_list character varying(64) DEFAULT 'standard'::character varying NOT NULL,
    revision integer NOT NULL,
    currency character varying(3) NOT NULL,
    unit character varying(32) NOT NULL,
    unit_price numeric NOT NULL,
    effective_from timestamp with time zone NOT NULL,
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    note character varying(500),
    command_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(200) NOT NULL,
    CONSTRAINT ck_catalogue_prices_currency CHECK (((currency)::text ~ '^[A-Z]{3}$'::text)),
    CONSTRAINT ck_catalogue_prices_revision CHECK ((revision >= 1)),
    CONSTRAINT ck_catalogue_prices_source CHECK (((source)::text = 'manual'::text)),
    CONSTRAINT ck_catalogue_prices_unit_price CHECK (((unit_price > (0)::numeric) AND (unit_price < '1000000000000'::numeric) AND (scale(unit_price) <= 6)))
);

ALTER TABLE ONLY public.catalogue_prices FORCE ROW LEVEL SECURITY;

CREATE TABLE public.catalogue_products (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    sku character varying(64) NOT NULL,
    name character varying(200) NOT NULL,
    description character varying(2000),
    category_id uuid,
    unit character varying(32) NOT NULL,
    minimum_quantity numeric NOT NULL,
    quantity_step numeric NOT NULL,
    search_terms text[] NOT NULL,
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    archived_at timestamp with time zone,
    archived_by character varying(200),
    row_version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(200) NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(200) NOT NULL,
    CONSTRAINT ck_catalogue_products_minimum_quantity CHECK (((minimum_quantity > (0)::numeric) AND (minimum_quantity < '1000000000'::numeric) AND (scale(minimum_quantity) <= 3))),
    CONSTRAINT ck_catalogue_products_name CHECK ((btrim((name)::text) <> ''::text)),
    CONSTRAINT ck_catalogue_products_quantity_step CHECK (((quantity_step > (0)::numeric) AND (quantity_step < '1000000000'::numeric) AND (scale(quantity_step) <= 3))),
    CONSTRAINT ck_catalogue_products_row_version CHECK ((row_version >= 1)),
    CONSTRAINT ck_catalogue_products_sku CHECK (((sku)::text ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$'::text)),
    CONSTRAINT ck_catalogue_products_source CHECK (((source)::text = 'manual'::text)),
    CONSTRAINT ck_catalogue_products_unit CHECK ((btrim((unit)::text) <> ''::text))
);

ALTER TABLE ONLY public.catalogue_products FORCE ROW LEVEL SECURITY;

CREATE TABLE public.catalogue_stock (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    product_id uuid NOT NULL,
    location_code character varying(32) DEFAULT 'MAIN'::character varying NOT NULL,
    quantity numeric,
    unit character varying(32) NOT NULL,
    as_of timestamp with time zone NOT NULL,
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    note character varying(500),
    command_id uuid NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    recorded_by character varying(200) NOT NULL,
    CONSTRAINT ck_catalogue_stock_as_of CHECK ((as_of <= (recorded_at + '00:05:00'::interval))),
    CONSTRAINT ck_catalogue_stock_quantity CHECK (((quantity IS NULL) OR ((quantity >= (0)::numeric) AND (quantity < '1000000000000'::numeric) AND (scale(quantity) <= 3)))),
    CONSTRAINT ck_catalogue_stock_source CHECK (((source)::text = 'manual'::text))
);

ALTER TABLE ONLY public.catalogue_stock FORCE ROW LEVEL SECURITY;

CREATE TABLE public.channel_bindings (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    subject character varying(200) NOT NULL,
    channel character varying(32) NOT NULL,
    connection_id character varying(100) NOT NULL,
    external_user_id character varying(200) NOT NULL,
    enrollment_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    connection_ref uuid NOT NULL
);

ALTER TABLE ONLY public.channel_bindings FORCE ROW LEVEL SECURITY;

CREATE TABLE public.channel_enrollments (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    subject character varying(200) NOT NULL,
    channel character varying(32) NOT NULL,
    code_sha256 character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    connection_ref uuid
);

ALTER TABLE ONLY public.channel_enrollments FORCE ROW LEVEL SECURITY;

CREATE TABLE public.checkpoint_blobs (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    channel text NOT NULL,
    version text NOT NULL,
    type text NOT NULL,
    blob bytea
);

CREATE TABLE public.checkpoint_migrations (
    v integer NOT NULL
);

CREATE TABLE public.checkpoint_writes (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    type text,
    blob bytea NOT NULL,
    task_path text DEFAULT ''::text NOT NULL
);

CREATE TABLE public.checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    type text,
    checkpoint jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE public.connection_events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    kind character varying(32) NOT NULL,
    actor character varying(200) NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_connection_events_kind CHECK (((kind)::text = ANY ((ARRAY['created'::character varying, 'migrated'::character varying, 'status_changed'::character varying, 'tested'::character varying, 'provisioned'::character varying, 'deprovisioned'::character varying, 'settings_changed'::character varying, 'resumed'::character varying])::text[])))
);

ALTER TABLE ONLY public.connection_events FORCE ROW LEVEL SECURITY;

CREATE TABLE public.connection_routes (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    provider_key character varying(32) NOT NULL,
    routing_key character varying(200) NOT NULL,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.connection_routes FORCE ROW LEVEL SECURITY;

CREATE TABLE public.connections (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    provider_key character varying(32) NOT NULL,
    display_name character varying(100) NOT NULL,
    purposes text[] NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    secret_refs jsonb DEFAULT '{}'::jsonb NOT NULL,
    account_ref character varying(200),
    webhook_token_sha256 character varying(64),
    status character varying(16) NOT NULL,
    last_error character varying(300),
    created_by character varying(200) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    public_handle character varying(64),
    public_handle_changed_at timestamp with time zone,
    pending_action jsonb,
    CONSTRAINT ck_connections_provider_key CHECK (((provider_key)::text ~ '^[a-z][a-z0-9_]{1,31}$'::text)),
    CONSTRAINT ck_connections_public_handle CHECK (((public_handle IS NULL) OR ((public_handle)::text ~ '^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$'::text))),
    CONSTRAINT ck_connections_purposes CHECK (((cardinality(purposes) >= 1) AND (purposes <@ ARRAY['customer_messages'::text, 'manager_approvals'::text, 'catalogue'::text, 'payments'::text]))),
    CONSTRAINT ck_connections_status CHECK (((status)::text = ANY ((ARRAY['setting_up'::character varying, 'active'::character varying, 'error'::character varying, 'disabled'::character varying])::text[])))
);

ALTER TABLE ONLY public.connections FORCE ROW LEVEL SECURITY;

CREATE TABLE public.contact_identities (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    contact_id uuid NOT NULL,
    connection_ref uuid NOT NULL,
    external_id character varying(320) NOT NULL,
    display_hint character varying(200),
    verified boolean NOT NULL,
    consent_status character varying(16) DEFAULT 'implied'::character varying NOT NULL,
    consent_changed_at timestamp with time zone,
    consent_source character varying(100),
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_contact_identities_consent CHECK (((consent_status)::text = ANY ((ARRAY['implied'::character varying, 'opted_in'::character varying, 'opted_out'::character varying])::text[])))
);

ALTER TABLE ONLY public.contact_identities FORCE ROW LEVEL SECURITY;

CREATE TABLE public.contacts (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    display_name character varying(200),
    company character varying(200),
    email character varying(320),
    email_verified_at timestamp with time zone,
    phone character varying(32),
    phone_verified_at timestamp with time zone,
    merged_into_id uuid,
    merged_at timestamp with time zone,
    merged_by character varying(200),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_contacts_merged CHECK (((merged_into_id IS NULL) = (merged_at IS NULL))),
    CONSTRAINT ck_contacts_not_self CHECK (((merged_into_id IS NULL) OR (merged_into_id <> id)))
);

ALTER TABLE ONLY public.contacts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.conversation_messages (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    seq bigint NOT NULL,
    conversation_id uuid,
    enquiry_id uuid,
    direction character varying(8) NOT NULL,
    author character varying(16) NOT NULL,
    text text NOT NULL,
    template_key character varying(64),
    inbox_event_id uuid,
    outbound_message_id uuid,
    attachment_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_conversation_messages_author CHECK (((author)::text = ANY ((ARRAY['customer'::character varying, 'system'::character varying, 'staff'::character varying])::text[]))),
    CONSTRAINT ck_conversation_messages_direction CHECK (((direction)::text = ANY ((ARRAY['inbound'::character varying, 'outbound'::character varying])::text[]))),
    CONSTRAINT ck_conversation_messages_owner CHECK (((conversation_id IS NOT NULL) OR (enquiry_id IS NOT NULL))),
    CONSTRAINT ck_conversation_messages_source CHECK (((((direction)::text = 'inbound'::text) AND (inbox_event_id IS NOT NULL) AND (outbound_message_id IS NULL)) OR (((direction)::text = 'outbound'::text) AND (outbound_message_id IS NOT NULL) AND (inbox_event_id IS NULL)))),
    CONSTRAINT ck_conversation_messages_text CHECK ((length(text) <= 20000))
);

ALTER TABLE ONLY public.conversation_messages FORCE ROW LEVEL SECURITY;

ALTER TABLE public.conversation_messages ALTER COLUMN seq ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.conversation_messages_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.conversations (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    contact_id uuid NOT NULL,
    identity_id uuid NOT NULL,
    connection_ref uuid NOT NULL,
    last_message_at timestamp with time zone NOT NULL,
    last_inbound_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.conversations FORCE ROW LEVEL SECURITY;

CREATE TABLE public.customer_action_refs (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    quotation_id uuid NOT NULL,
    version integer NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    identity_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    outbound_message_id uuid,
    token_sha256 character varying(64),
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    response_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_customer_action_refs_consumed CHECK (((consumed_at IS NULL) = (response_id IS NULL)))
);

ALTER TABLE ONLY public.customer_action_refs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.customer_sessions (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    connection_ref uuid NOT NULL,
    identity_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    secret_sha256 character varying(64) NOT NULL,
    ip_hmac character varying(64) NOT NULL,
    origin character varying(200),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone
);

ALTER TABLE ONLY public.customer_sessions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.email_verifications (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    email_normalized character varying(254) NOT NULL,
    token_sha256 character varying(64),
    email_status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    expires_at timestamp with time zone,
    sent_at timestamp with time zone,
    consumed_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_email_verifications_email_status CHECK (((email_status)::text = ANY ((ARRAY['pending'::character varying, 'sending'::character varying, 'sent'::character varying, 'failed'::character varying, 'unknown'::character varying])::text[])))
);

ALTER TABLE ONLY public.email_verifications FORCE ROW LEVEL SECURITY;

CREATE TABLE public.enquiries (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    inbox_event_id uuid NOT NULL,
    channel character varying(32) NOT NULL,
    status character varying(32) NOT NULL,
    customer_reference character varying(200),
    text text NOT NULL,
    correlation_id uuid NOT NULL,
    received_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    conversation_id uuid,
    contact_id uuid,
    CONSTRAINT ck_enquiries_status CHECK (((status)::text = ANY ((ARRAY['received'::character varying, 'ready_for_extraction'::character varying, 'needs_review'::character varying, 'ready_for_quotation'::character varying, 'needs_clarification'::character varying])::text[])))
);

ALTER TABLE ONLY public.enquiries FORCE ROW LEVEL SECURITY;

CREATE TABLE public.inbox_events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    channel character varying(32) NOT NULL,
    connection_id character varying(100) NOT NULL,
    provider_event_id character varying(200) NOT NULL,
    payload_sha256 character varying(64) NOT NULL,
    envelope jsonb NOT NULL,
    received_at timestamp with time zone NOT NULL,
    correlation_id uuid NOT NULL,
    event_type character varying(32) DEFAULT 'customer_message'::character varying NOT NULL,
    processed_at timestamp with time zone,
    outcome character varying(300),
    connection_ref uuid NOT NULL,
    CONSTRAINT ck_inbox_events_event_type CHECK (((event_type)::text = ANY ((ARRAY['customer_message'::character varying, 'manager_action'::character varying, 'delivery_status'::character varying, 'consent_change'::character varying, 'manager_reply'::character varying, 'customer_action'::character varying, 'connection_update'::character varying, 'order_status_request'::character varying])::text[])))
);

ALTER TABLE ONLY public.inbox_events FORCE ROW LEVEL SECURITY;

CREATE TABLE public.invitations (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    email character varying(254) NOT NULL,
    email_normalized character varying(254) NOT NULL,
    roles text[] NOT NULL,
    invited_by character varying(200) NOT NULL,
    token_sha256 character varying(64),
    email_status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    expires_at timestamp with time zone,
    sent_at timestamp with time zone,
    accepted_at timestamp with time zone,
    accepted_by character varying(200),
    revoked_at timestamp with time zone,
    revoked_by character varying(200),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_invitations_email_status CHECK (((email_status)::text = ANY ((ARRAY['pending'::character varying, 'sending'::character varying, 'sent'::character varying, 'failed'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT ck_invitations_roles CHECK (((cardinality(roles) >= 1) AND (roles <@ ARRAY['administrator'::text, 'sales_operator'::text, 'approver'::text, 'viewer'::text])))
);

ALTER TABLE ONLY public.invitations FORCE ROW LEVEL SECURITY;

CREATE TABLE public.jobs (
    id uuid NOT NULL,
    tenant_id uuid,
    kind character varying(50) NOT NULL,
    payload jsonb NOT NULL,
    status character varying(16) NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer NOT NULL,
    run_at timestamp with time zone NOT NULL,
    lease_token uuid,
    lease_expires_at timestamp with time zone,
    last_error character varying(500),
    correlation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_jobs_attempts CHECK (((attempts >= 0) AND (max_attempts >= 1))),
    CONSTRAINT ck_jobs_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'succeeded'::character varying, 'dead'::character varying])::text[]))),
    CONSTRAINT ck_jobs_tenant CHECK (((tenant_id IS NOT NULL) OR ((kind)::text = 'send_account_email'::text)))
);

ALTER TABLE ONLY public.jobs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.manager_reply_classifications (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    inbox_event_id uuid NOT NULL,
    reference_id uuid,
    text text NOT NULL,
    text_sha256 character varying(64) NOT NULL,
    intent character varying(16) NOT NULL,
    model character varying(200) NOT NULL,
    guard character varying(64),
    outcome character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_manager_reply_classifications_intent CHECK (((intent)::text = ANY ((ARRAY['approve'::character varying, 'reject'::character varying, 'revise'::character varying, 'unclear'::character varying])::text[]))),
    CONSTRAINT ck_manager_reply_classifications_text CHECK ((length(text) <= 4000))
);

ALTER TABLE ONLY public.manager_reply_classifications FORCE ROW LEVEL SECURITY;

CREATE TABLE public.media_links (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    token_sha256 character varying(64) NOT NULL,
    document_id uuid NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    outbound_message_id uuid,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.media_links FORCE ROW LEVEL SECURITY;

CREATE TABLE public.memberships (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    subject character varying(200) NOT NULL,
    role character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_memberships_role CHECK (((role)::text = ANY ((ARRAY['administrator'::character varying, 'sales_operator'::character varying, 'approver'::character varying, 'viewer'::character varying])::text[])))
);

ALTER TABLE ONLY public.memberships FORCE ROW LEVEL SECURITY;

CREATE TABLE public.order_events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    order_id uuid NOT NULL,
    type character varying(32) NOT NULL,
    from_status character varying(16),
    to_status character varying(16),
    source character varying(16) NOT NULL,
    actor character varying(200) NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_order_events_source CHECK (((source)::text = ANY ((ARRAY['system'::character varying, 'staff'::character varying])::text[]))),
    CONSTRAINT ck_order_events_type CHECK (((type)::text = ANY ((ARRAY['created'::character varying, 'status_changed'::character varying, 'held'::character varying])::text[])))
);

ALTER TABLE ONLY public.order_events FORCE ROW LEVEL SECURITY;

CREATE TABLE public.order_lines (
    tenant_id uuid NOT NULL,
    order_id uuid NOT NULL,
    line_index integer NOT NULL,
    sku character varying(64) NOT NULL,
    description character varying(500) NOT NULL,
    quantity numeric NOT NULL,
    unit character varying(32) NOT NULL,
    unit_price numeric NOT NULL,
    line_total numeric NOT NULL,
    CONSTRAINT ck_order_lines_quantity CHECK ((quantity > (0)::numeric))
);

ALTER TABLE ONLY public.order_lines FORCE ROW LEVEL SECURITY;

CREATE TABLE public.orders (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    order_number character varying(16) NOT NULL,
    quotation_id uuid NOT NULL,
    version integer NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    response_id uuid,
    status character varying(16) NOT NULL,
    hold_reason character varying(500),
    currency character varying(3) NOT NULL,
    subtotal numeric NOT NULL,
    tax numeric NOT NULL,
    total numeric NOT NULL,
    created_by character varying(200) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_orders_currency CHECK (((currency)::text ~ '^[A-Z]{3}$'::text)),
    CONSTRAINT ck_orders_status CHECK (((status)::text = ANY ((ARRAY['placed'::character varying, 'confirmed'::character varying, 'in_progress'::character varying, 'shipped'::character varying, 'delivered'::character varying, 'cancelled'::character varying, 'on_hold'::character varying])::text[])))
);

ALTER TABLE ONLY public.orders FORCE ROW LEVEL SECURITY;

CREATE TABLE public.outbound_messages (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    connection_ref uuid NOT NULL,
    provider_key character varying(32) NOT NULL,
    purpose character varying(32) NOT NULL,
    template_key character varying(64) NOT NULL,
    params jsonb NOT NULL,
    body_text text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    recipient character varying(200) NOT NULL,
    idempotency_key character varying(200) NOT NULL,
    status character varying(16) NOT NULL,
    provider_message_id character varying(200),
    last_error character varying(300),
    document_id uuid,
    quotation_id uuid,
    version integer,
    approval_task_id uuid,
    binding_id uuid,
    enquiry_id uuid,
    response_id uuid,
    correlation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    delivered_at timestamp with time zone,
    action_ref_id uuid,
    resent_from_id uuid,
    CONSTRAINT ck_outbound_messages_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'sending'::character varying, 'available'::character varying, 'sent'::character varying, 'delivered'::character varying, 'failed'::character varying, 'unknown'::character varying])::text[])))
);

ALTER TABLE ONLY public.outbound_messages FORCE ROW LEVEL SECURITY;

CREATE TABLE public.password_resets (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    email_normalized character varying(254) NOT NULL,
    token_sha256 character varying(64),
    email_status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    expires_at timestamp with time zone,
    sent_at timestamp with time zone,
    consumed_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_password_resets_email_status CHECK (((email_status)::text = ANY ((ARRAY['pending'::character varying, 'sending'::character varying, 'sent'::character varying, 'failed'::character varying, 'unknown'::character varying])::text[])))
);

ALTER TABLE ONLY public.password_resets FORCE ROW LEVEL SECURITY;

CREATE TABLE public.quotation_deliveries (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    quotation_id uuid NOT NULL,
    version integer NOT NULL,
    channel character varying(32) NOT NULL,
    recipient character varying(200) NOT NULL,
    status character varying(16) NOT NULL,
    document_id uuid,
    provider_reference character varying(200),
    last_error character varying(300),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    delivered_at timestamp with time zone,
    first_downloaded_at timestamp with time zone,
    outbound_message_id uuid,
    CONSTRAINT ck_quotation_deliveries_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'sending'::character varying, 'available'::character varying, 'sent'::character varying, 'failed'::character varying, 'unknown'::character varying])::text[])))
);

ALTER TABLE ONLY public.quotation_deliveries FORCE ROW LEVEL SECURITY;

CREATE TABLE public.quotation_documents (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    quotation_id uuid NOT NULL,
    version integer NOT NULL,
    kind character varying(16) NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    storage_key character varying(200) NOT NULL,
    sha256 character varying(64) NOT NULL,
    size_bytes integer NOT NULL,
    renderer character varying(32) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    valid_until timestamp with time zone NOT NULL,
    CONSTRAINT ck_quotation_documents_kind CHECK (((kind)::text = 'approved'::text)),
    CONSTRAINT ck_quotation_documents_size CHECK ((size_bytes > 0))
);

ALTER TABLE ONLY public.quotation_documents FORCE ROW LEVEL SECURITY;

CREATE TABLE public.quotation_responses (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    quotation_id uuid NOT NULL,
    version integer NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    outcome character varying(16) NOT NULL,
    actor character varying(200) NOT NULL,
    reason character varying(1000),
    idempotency_key character varying(200) NOT NULL,
    fingerprint character varying(64) NOT NULL,
    differences jsonb NOT NULL,
    revised_version integer,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_quotation_responses_outcome CHECK (((outcome)::text = ANY ((ARRAY['accepted'::character varying, 'declined'::character varying, 'refused_changed'::character varying, 'withdrawn'::character varying, 'expired'::character varying])::text[])))
);

ALTER TABLE ONLY public.quotation_responses FORCE ROW LEVEL SECURITY;

CREATE TABLE public.quotation_versions (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    quotation_id uuid NOT NULL,
    version integer NOT NULL,
    content jsonb NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    currency character varying(3) NOT NULL,
    total numeric(18,4) NOT NULL,
    created_by character varying(200) NOT NULL,
    reason character varying(1000),
    command_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_quotation_versions_version CHECK ((version >= 1))
);

ALTER TABLE ONLY public.quotation_versions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.quotations (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    enquiry_id uuid NOT NULL,
    status character varying(32) NOT NULL,
    current_version integer,
    pricing_issues jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_quotations_status CHECK (((status)::text = ANY ((ARRAY['pricing_failed'::character varying, 'pending_approval'::character varying, 'revision_requested'::character varying, 'revision_submitted'::character varying, 'approved'::character varying, 'rejected'::character varying, 'accepted'::character varying, 'declined'::character varying, 'expired'::character varying, 'withdrawn'::character varying])::text[])))
);

ALTER TABLE ONLY public.quotations FORCE ROW LEVEL SECURITY;

CREATE TABLE public.rate_limit_counters (
    rule character varying(32) NOT NULL,
    key_sha256 character varying(64) NOT NULL,
    window_start timestamp with time zone NOT NULL,
    hits integer NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_rate_limit_counters_hits CHECK ((hits >= 1))
);

ALTER TABLE ONLY public.rate_limit_counters FORCE ROW LEVEL SECURITY;

CREATE TABLE public.tenant_pricing_policies (
    tenant_id uuid NOT NULL,
    version integer NOT NULL,
    currency character varying(3),
    minor_units smallint,
    rounding_mode character varying(16),
    tax_rate numeric(7,6),
    max_price_age_seconds integer,
    max_stock_age_seconds integer,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    quote_validity_days integer DEFAULT 7 NOT NULL,
    updated_by character varying(200),
    CONSTRAINT ck_tenant_pricing_policies_ages CHECK (((max_price_age_seconds > 0) AND (max_stock_age_seconds > 0))),
    CONSTRAINT ck_tenant_pricing_policies_currency CHECK (((currency)::text ~ '^[A-Z]{3}$'::text)),
    CONSTRAINT ck_tenant_pricing_policies_minor_units CHECK (((minor_units >= 0) AND (minor_units <= 4))),
    CONSTRAINT ck_tenant_pricing_policies_quote_validity CHECK ((quote_validity_days >= 1)),
    CONSTRAINT ck_tenant_pricing_policies_rounding_mode CHECK (((rounding_mode)::text = ANY ((ARRAY['half_up'::character varying, 'half_even'::character varying])::text[]))),
    CONSTRAINT ck_tenant_pricing_policies_tax_rate CHECK (((tax_rate >= (0)::numeric) AND (tax_rate < (1)::numeric))),
    CONSTRAINT ck_tenant_pricing_policies_version CHECK ((version >= 1))
);

ALTER TABLE ONLY public.tenant_pricing_policies FORCE ROW LEVEL SECURITY;

CREATE TABLE public.tenant_profiles (
    tenant_id uuid NOT NULL,
    legal_name character varying(200),
    address character varying(500),
    tax_id_label character varying(32),
    tax_id character varying(64),
    logo_storage_key character varying(300),
    logo_sha256 character varying(64),
    version integer DEFAULT 1 NOT NULL,
    updated_by character varying(200),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tenant_profiles_logo CHECK (((logo_storage_key IS NULL) = (logo_sha256 IS NULL))),
    CONSTRAINT ck_tenant_profiles_version CHECK ((version >= 1))
);

ALTER TABLE ONLY public.tenant_profiles FORCE ROW LEVEL SECURITY;

CREATE TABLE public.tenants (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    status character varying(16) DEFAULT 'setting_up'::character varying NOT NULL,
    country character varying(2),
    time_zone character varying(64),
    created_by character varying(200),
    went_live_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    acknowledge_enquiries boolean DEFAULT true NOT NULL,
    order_creation character varying(16) DEFAULT 'staff'::character varying NOT NULL,
    CONSTRAINT ck_tenants_country CHECK (((country)::text ~ '^[A-Z]{2}$'::text)),
    CONSTRAINT ck_tenants_order_creation CHECK (((order_creation)::text = ANY ((ARRAY['staff'::character varying, 'automatic'::character varying])::text[]))),
    CONSTRAINT ck_tenants_status CHECK (((status)::text = ANY ((ARRAY['setting_up'::character varying, 'active'::character varying, 'suspended'::character varying])::text[])))
);

ALTER TABLE ONLY public.tenants FORCE ROW LEVEL SECURITY;

CREATE TABLE public.user_credentials (
    user_id uuid NOT NULL,
    password_hash character varying(255) NOT NULL,
    password_changed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_user_credentials_argon2id CHECK (((password_hash)::text ~~ '$argon2id$%'::text))
);

ALTER TABLE ONLY public.user_credentials FORCE ROW LEVEL SECURITY;

CREATE TABLE public.user_sessions (
    id uuid NOT NULL,
    token_sha256 character varying(64) NOT NULL,
    subject character varying(200) NOT NULL,
    provider character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone
);

ALTER TABLE ONLY public.user_sessions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.users (
    id uuid NOT NULL,
    subject character varying(200) NOT NULL,
    email character varying(254),
    email_normalized character varying(254),
    display_name character varying(100),
    email_verified_at timestamp with time zone,
    disabled_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_users_email_normalized CHECK (((email_normalized)::text = lower((email_normalized)::text)))
);

ALTER TABLE ONLY public.users FORCE ROW LEVEL SECURITY;

CREATE TABLE public.workflow_runs (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    enquiry_id uuid NOT NULL,
    graph_version character varying(50) NOT NULL,
    status character varying(32) NOT NULL,
    outcome jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    round integer DEFAULT 1 NOT NULL,
    CONSTRAINT ck_workflow_runs_round CHECK ((round >= 1)),
    CONSTRAINT ck_workflow_runs_status CHECK (((status)::text = ANY ((ARRAY['running'::character varying, 'ready_for_quotation'::character varying, 'needs_clarification'::character varying, 'needs_review'::character varying, 'approved'::character varying, 'rejected'::character varying, 'accepted'::character varying, 'declined'::character varying, 'expired'::character varying, 'withdrawn'::character varying])::text[])))
);

ALTER TABLE ONLY public.workflow_runs FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.checkpoint_blobs
    ADD CONSTRAINT checkpoint_blobs_pkey PRIMARY KEY (thread_id, checkpoint_ns, channel, version);

ALTER TABLE ONLY public.checkpoint_migrations
    ADD CONSTRAINT checkpoint_migrations_pkey PRIMARY KEY (v);

ALTER TABLE ONLY public.checkpoint_writes
    ADD CONSTRAINT checkpoint_writes_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx);

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id);

ALTER TABLE ONLY public.account_notices
    ADD CONSTRAINT pk_account_notices PRIMARY KEY (id);

ALTER TABLE ONLY public.admin_events
    ADD CONSTRAINT pk_admin_events PRIMARY KEY (id);

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT pk_api_keys PRIMARY KEY (id);

ALTER TABLE ONLY public.approval_action_refs
    ADD CONSTRAINT pk_approval_action_refs PRIMARY KEY (id);

ALTER TABLE ONLY public.approval_decisions
    ADD CONSTRAINT pk_approval_decisions PRIMARY KEY (id);

ALTER TABLE ONLY public.approval_reply_prompts
    ADD CONSTRAINT pk_approval_reply_prompts PRIMARY KEY (id);

ALTER TABLE ONLY public.approval_tasks
    ADD CONSTRAINT pk_approval_tasks PRIMARY KEY (id);

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT pk_attachments PRIMARY KEY (id);

ALTER TABLE ONLY public.catalogue_categories
    ADD CONSTRAINT pk_catalogue_categories PRIMARY KEY (id);

ALTER TABLE ONLY public.catalogue_changes
    ADD CONSTRAINT pk_catalogue_changes PRIMARY KEY (id);

ALTER TABLE ONLY public.catalogue_prices
    ADD CONSTRAINT pk_catalogue_prices PRIMARY KEY (id);

ALTER TABLE ONLY public.catalogue_products
    ADD CONSTRAINT pk_catalogue_products PRIMARY KEY (id);

ALTER TABLE ONLY public.catalogue_stock
    ADD CONSTRAINT pk_catalogue_stock PRIMARY KEY (id);

ALTER TABLE ONLY public.channel_bindings
    ADD CONSTRAINT pk_channel_bindings PRIMARY KEY (id);

ALTER TABLE ONLY public.channel_enrollments
    ADD CONSTRAINT pk_channel_enrollments PRIMARY KEY (id);

ALTER TABLE ONLY public.connection_events
    ADD CONSTRAINT pk_connection_events PRIMARY KEY (id);

ALTER TABLE ONLY public.connection_routes
    ADD CONSTRAINT pk_connection_routes PRIMARY KEY (id);

ALTER TABLE ONLY public.connections
    ADD CONSTRAINT pk_connections PRIMARY KEY (id);

ALTER TABLE ONLY public.contact_identities
    ADD CONSTRAINT pk_contact_identities PRIMARY KEY (id);

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT pk_contacts PRIMARY KEY (id);

ALTER TABLE ONLY public.conversation_messages
    ADD CONSTRAINT pk_conversation_messages PRIMARY KEY (id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT pk_conversations PRIMARY KEY (id);

ALTER TABLE ONLY public.customer_action_refs
    ADD CONSTRAINT pk_customer_action_refs PRIMARY KEY (id);

ALTER TABLE ONLY public.customer_sessions
    ADD CONSTRAINT pk_customer_sessions PRIMARY KEY (id);

ALTER TABLE ONLY public.email_verifications
    ADD CONSTRAINT pk_email_verifications PRIMARY KEY (id);

ALTER TABLE ONLY public.enquiries
    ADD CONSTRAINT pk_enquiries PRIMARY KEY (id);

ALTER TABLE ONLY public.inbox_events
    ADD CONSTRAINT pk_inbox_events PRIMARY KEY (id);

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT pk_invitations PRIMARY KEY (id);

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT pk_jobs PRIMARY KEY (id);

ALTER TABLE ONLY public.manager_reply_classifications
    ADD CONSTRAINT pk_manager_reply_classifications PRIMARY KEY (id);

ALTER TABLE ONLY public.media_links
    ADD CONSTRAINT pk_media_links PRIMARY KEY (id);

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT pk_memberships PRIMARY KEY (id);

ALTER TABLE ONLY public.order_events
    ADD CONSTRAINT pk_order_events PRIMARY KEY (id);

ALTER TABLE ONLY public.order_lines
    ADD CONSTRAINT pk_order_lines PRIMARY KEY (tenant_id, order_id, line_index);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT pk_orders PRIMARY KEY (id);

ALTER TABLE ONLY public.outbound_messages
    ADD CONSTRAINT pk_outbound_messages PRIMARY KEY (id);

ALTER TABLE ONLY public.password_resets
    ADD CONSTRAINT pk_password_resets PRIMARY KEY (id);

ALTER TABLE ONLY public.quotation_deliveries
    ADD CONSTRAINT pk_quotation_deliveries PRIMARY KEY (id);

ALTER TABLE ONLY public.quotation_documents
    ADD CONSTRAINT pk_quotation_documents PRIMARY KEY (id);

ALTER TABLE ONLY public.quotation_responses
    ADD CONSTRAINT pk_quotation_responses PRIMARY KEY (id);

ALTER TABLE ONLY public.quotation_versions
    ADD CONSTRAINT pk_quotation_versions PRIMARY KEY (id);

ALTER TABLE ONLY public.quotations
    ADD CONSTRAINT pk_quotations PRIMARY KEY (id);

ALTER TABLE ONLY public.rate_limit_counters
    ADD CONSTRAINT pk_rate_limit_counters PRIMARY KEY (rule, key_sha256);

ALTER TABLE ONLY public.tenant_pricing_policies
    ADD CONSTRAINT pk_tenant_pricing_policies PRIMARY KEY (tenant_id);

ALTER TABLE ONLY public.tenant_profiles
    ADD CONSTRAINT pk_tenant_profiles PRIMARY KEY (tenant_id);

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT pk_tenants PRIMARY KEY (id);

ALTER TABLE ONLY public.user_credentials
    ADD CONSTRAINT pk_user_credentials PRIMARY KEY (user_id);

ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT pk_user_sessions PRIMARY KEY (id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);

ALTER TABLE ONLY public.workflow_runs
    ADD CONSTRAINT pk_workflow_runs PRIMARY KEY (id);

ALTER TABLE ONLY public.approval_action_refs
    ADD CONSTRAINT uq_approval_action_refs_task UNIQUE (tenant_id, task_id, binding_id);

ALTER TABLE ONLY public.approval_action_refs
    ADD CONSTRAINT uq_approval_action_refs_token UNIQUE (token_sha256);

ALTER TABLE ONLY public.approval_decisions
    ADD CONSTRAINT uq_approval_decisions_command UNIQUE (tenant_id, command_id);

ALTER TABLE ONLY public.approval_decisions
    ADD CONSTRAINT uq_approval_decisions_task UNIQUE (tenant_id, task_id);

ALTER TABLE ONLY public.approval_reply_prompts
    ADD CONSTRAINT uq_approval_reply_prompts_event UNIQUE (inbox_event_id);

ALTER TABLE ONLY public.approval_reply_prompts
    ADD CONSTRAINT uq_approval_reply_prompts_message UNIQUE (outbound_message_id);

ALTER TABLE ONLY public.approval_reply_prompts
    ADD CONSTRAINT uq_approval_reply_prompts_token UNIQUE (token_sha256);

ALTER TABLE ONLY public.approval_tasks
    ADD CONSTRAINT uq_approval_tasks_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.approval_tasks
    ADD CONSTRAINT uq_approval_tasks_version UNIQUE (tenant_id, version_id);

ALTER TABLE ONLY public.catalogue_categories
    ADD CONSTRAINT uq_catalogue_categories_tenant_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.catalogue_prices
    ADD CONSTRAINT uq_catalogue_prices_command UNIQUE (tenant_id, command_id);

ALTER TABLE ONLY public.catalogue_prices
    ADD CONSTRAINT uq_catalogue_prices_revision UNIQUE (tenant_id, product_id, price_list, currency, revision);

ALTER TABLE ONLY public.catalogue_products
    ADD CONSTRAINT uq_catalogue_products_tenant_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.catalogue_stock
    ADD CONSTRAINT uq_catalogue_stock_command UNIQUE (tenant_id, command_id);

ALTER TABLE ONLY public.channel_bindings
    ADD CONSTRAINT uq_channel_bindings_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.channel_enrollments
    ADD CONSTRAINT uq_channel_enrollments_code UNIQUE (tenant_id, code_sha256);

ALTER TABLE ONLY public.connection_routes
    ADD CONSTRAINT uq_connection_routes_key UNIQUE (provider_key, routing_key);

ALTER TABLE ONLY public.connections
    ADD CONSTRAINT uq_connections_account UNIQUE (tenant_id, provider_key, account_ref);

ALTER TABLE ONLY public.connections
    ADD CONSTRAINT uq_connections_public_handle UNIQUE (public_handle);

ALTER TABLE ONLY public.connections
    ADD CONSTRAINT uq_connections_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.contact_identities
    ADD CONSTRAINT uq_contact_identities_external UNIQUE (tenant_id, connection_ref, external_id);

ALTER TABLE ONLY public.contact_identities
    ADD CONSTRAINT uq_contact_identities_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT uq_contacts_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.conversation_messages
    ADD CONSTRAINT uq_conversation_messages_inbox UNIQUE (inbox_event_id);

ALTER TABLE ONLY public.conversation_messages
    ADD CONSTRAINT uq_conversation_messages_outbound UNIQUE (outbound_message_id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT uq_conversations_identity UNIQUE (tenant_id, identity_id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT uq_conversations_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.customer_action_refs
    ADD CONSTRAINT uq_customer_action_refs_offer UNIQUE (tenant_id, quotation_id, version, identity_id);

ALTER TABLE ONLY public.customer_action_refs
    ADD CONSTRAINT uq_customer_action_refs_token UNIQUE (token_sha256);

ALTER TABLE ONLY public.email_verifications
    ADD CONSTRAINT uq_email_verifications_token_sha256 UNIQUE (token_sha256);

ALTER TABLE ONLY public.enquiries
    ADD CONSTRAINT uq_enquiries_inbox_event_id UNIQUE (inbox_event_id);

ALTER TABLE ONLY public.enquiries
    ADD CONSTRAINT uq_enquiries_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.inbox_events
    ADD CONSTRAINT uq_inbox_events_provider_event UNIQUE (tenant_id, connection_ref, provider_event_id, event_type);

ALTER TABLE ONLY public.inbox_events
    ADD CONSTRAINT uq_inbox_events_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT uq_invitations_token_sha256 UNIQUE (token_sha256);

ALTER TABLE ONLY public.manager_reply_classifications
    ADD CONSTRAINT uq_manager_reply_classifications_event UNIQUE (inbox_event_id);

ALTER TABLE ONLY public.media_links
    ADD CONSTRAINT uq_media_links_token UNIQUE (token_sha256);

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT uq_memberships_tenant_subject_role UNIQUE (tenant_id, subject, role);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT uq_orders_number UNIQUE (tenant_id, order_number);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT uq_orders_quotation UNIQUE (tenant_id, quotation_id);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT uq_orders_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.outbound_messages
    ADD CONSTRAINT uq_outbound_messages_idempotency UNIQUE (tenant_id, idempotency_key);

ALTER TABLE ONLY public.outbound_messages
    ADD CONSTRAINT uq_outbound_messages_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.password_resets
    ADD CONSTRAINT uq_password_resets_token_sha256 UNIQUE (token_sha256);

ALTER TABLE ONLY public.quotation_deliveries
    ADD CONSTRAINT uq_quotation_deliveries_message UNIQUE (outbound_message_id);

ALTER TABLE ONLY public.quotation_deliveries
    ADD CONSTRAINT uq_quotation_deliveries_target UNIQUE (tenant_id, quotation_id, version, channel, recipient);

ALTER TABLE ONLY public.quotation_documents
    ADD CONSTRAINT uq_quotation_documents_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.quotation_documents
    ADD CONSTRAINT uq_quotation_documents_version UNIQUE (tenant_id, quotation_id, version, kind);

ALTER TABLE ONLY public.quotation_responses
    ADD CONSTRAINT uq_quotation_responses_key UNIQUE (tenant_id, idempotency_key);

ALTER TABLE ONLY public.quotation_versions
    ADD CONSTRAINT uq_quotation_versions_command UNIQUE (tenant_id, command_id);

ALTER TABLE ONLY public.quotation_versions
    ADD CONSTRAINT uq_quotation_versions_number UNIQUE (tenant_id, quotation_id, version);

ALTER TABLE ONLY public.quotation_versions
    ADD CONSTRAINT uq_quotation_versions_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.quotations
    ADD CONSTRAINT uq_quotations_tenant_enquiry UNIQUE (tenant_id, enquiry_id);

ALTER TABLE ONLY public.quotations
    ADD CONSTRAINT uq_quotations_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT uq_user_sessions_token_sha256 UNIQUE (token_sha256);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_subject UNIQUE (subject);

ALTER TABLE ONLY public.workflow_runs
    ADD CONSTRAINT uq_workflow_runs_enquiry_round UNIQUE (tenant_id, enquiry_id, graph_version, round);

CREATE INDEX checkpoint_blobs_thread_id_idx ON public.checkpoint_blobs USING btree (thread_id);

CREATE INDEX checkpoint_writes_thread_id_idx ON public.checkpoint_writes USING btree (thread_id);

CREATE INDEX checkpoints_thread_id_idx ON public.checkpoints USING btree (thread_id);

CREATE INDEX ix_admin_events_tenant_created ON public.admin_events USING btree (tenant_id, created_at);

CREATE INDEX ix_attachments_tenant_enquiry ON public.attachments USING btree (tenant_id, enquiry_id);

CREATE INDEX ix_catalogue_changes_entity ON public.catalogue_changes USING btree (tenant_id, entity_id, created_at);

CREATE INDEX ix_catalogue_prices_lookup ON public.catalogue_prices USING btree (tenant_id, product_id, price_list, currency, effective_from DESC, revision DESC);

CREATE INDEX ix_catalogue_products_category ON public.catalogue_products USING btree (tenant_id, category_id);

CREATE INDEX ix_catalogue_products_name_trgm ON public.catalogue_products USING gin (lower((name)::text) public.gin_trgm_ops);

CREATE INDEX ix_catalogue_products_sku_trgm ON public.catalogue_products USING gin (lower((sku)::text) public.gin_trgm_ops);

CREATE INDEX ix_catalogue_products_terms ON public.catalogue_products USING gin (search_terms);

CREATE INDEX ix_catalogue_stock_current ON public.catalogue_stock USING btree (tenant_id, product_id, location_code, as_of DESC, recorded_at DESC);

CREATE INDEX ix_connection_events_connection ON public.connection_events USING btree (tenant_id, connection_id, created_at);

CREATE INDEX ix_connection_routes_connection ON public.connection_routes USING btree (tenant_id, connection_id);

CREATE INDEX ix_connections_tenant_status ON public.connections USING btree (tenant_id, status);

CREATE INDEX ix_contact_identities_contact ON public.contact_identities USING btree (tenant_id, contact_id);

CREATE INDEX ix_contacts_tenant_email ON public.contacts USING btree (tenant_id, lower((email)::text));

CREATE INDEX ix_contacts_tenant_phone ON public.contacts USING btree (tenant_id, phone);

CREATE INDEX ix_conversation_messages_conversation ON public.conversation_messages USING btree (tenant_id, conversation_id, seq);

CREATE INDEX ix_conversation_messages_enquiry ON public.conversation_messages USING btree (tenant_id, enquiry_id, seq);

CREATE INDEX ix_conversations_contact ON public.conversations USING btree (tenant_id, contact_id);

CREATE INDEX ix_conversations_inbox ON public.conversations USING btree (tenant_id, last_message_at DESC);

CREATE INDEX ix_customer_sessions_connection ON public.customer_sessions USING btree (tenant_id, connection_ref);

CREATE INDEX ix_enquiries_conversation_status ON public.enquiries USING btree (tenant_id, conversation_id, status);

CREATE INDEX ix_jobs_claim ON public.jobs USING btree (status, run_at);

CREATE INDEX ix_order_events_order ON public.order_events USING btree (tenant_id, order_id, occurred_at);

CREATE INDEX ix_orders_status ON public.orders USING btree (tenant_id, status, created_at);

CREATE INDEX ix_outbound_messages_action_ref ON public.outbound_messages USING btree (tenant_id, action_ref_id);

CREATE INDEX ix_outbound_messages_quotation ON public.outbound_messages USING btree (tenant_id, quotation_id);

CREATE INDEX ix_outbound_messages_status ON public.outbound_messages USING btree (tenant_id, status);

CREATE INDEX ix_outbound_messages_task ON public.outbound_messages USING btree (tenant_id, approval_task_id);

CREATE INDEX ix_rate_limit_counters_expires ON public.rate_limit_counters USING btree (expires_at);

CREATE UNIQUE INDEX uq_approval_reply_prompts_open ON public.approval_reply_prompts USING btree (tenant_id, reference_id) WHERE (closed_at IS NULL);

CREATE UNIQUE INDEX uq_catalogue_categories_name ON public.catalogue_categories USING btree (tenant_id, lower((name)::text));

CREATE UNIQUE INDEX uq_catalogue_products_sku ON public.catalogue_products USING btree (tenant_id, lower((sku)::text));

CREATE UNIQUE INDEX uq_channel_bindings_active_account ON public.channel_bindings USING btree (tenant_id, connection_ref, external_user_id) WHERE (revoked_at IS NULL);

CREATE UNIQUE INDEX uq_connections_one_catalogue ON public.connections USING btree (tenant_id) WHERE (((status)::text = 'active'::text) AND (purposes @> ARRAY['catalogue'::text]));

CREATE UNIQUE INDEX uq_email_verifications_one_open ON public.email_verifications USING btree (user_id) WHERE ((consumed_at IS NULL) AND (revoked_at IS NULL));

CREATE UNIQUE INDEX uq_invitations_one_open ON public.invitations USING btree (tenant_id, email_normalized) WHERE ((accepted_at IS NULL) AND (revoked_at IS NULL));

CREATE UNIQUE INDEX uq_outbound_messages_provider_message ON public.outbound_messages USING btree (tenant_id, connection_ref, provider_message_id) WHERE (provider_message_id IS NOT NULL);

CREATE UNIQUE INDEX uq_password_resets_one_open ON public.password_resets USING btree (user_id) WHERE ((consumed_at IS NULL) AND (revoked_at IS NULL));

CREATE UNIQUE INDEX uq_quotation_responses_closing ON public.quotation_responses USING btree (tenant_id, quotation_id) WHERE ((outcome)::text <> 'refused_changed'::text);

CREATE UNIQUE INDEX uq_users_email_normalized ON public.users USING btree (email_normalized) WHERE (email_normalized IS NOT NULL);

CREATE TRIGGER admin_events_immutable BEFORE DELETE OR UPDATE ON public.admin_events FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER approval_decisions_immutable BEFORE DELETE OR UPDATE ON public.approval_decisions FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER catalogue_changes_immutable BEFORE DELETE OR UPDATE ON public.catalogue_changes FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER catalogue_prices_immutable BEFORE DELETE OR UPDATE ON public.catalogue_prices FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER catalogue_stock_immutable BEFORE DELETE OR UPDATE ON public.catalogue_stock FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER connection_events_immutable BEFORE DELETE OR UPDATE ON public.connection_events FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER conversation_messages_immutable BEFORE DELETE OR UPDATE ON public.conversation_messages FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER enquiries_reopen_guard BEFORE UPDATE OF status ON public.enquiries FOR EACH ROW EXECUTE FUNCTION public.guard_enquiry_reopen();

CREATE TRIGGER manager_reply_classifications_immutable BEFORE DELETE OR UPDATE ON public.manager_reply_classifications FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER order_events_immutable BEFORE DELETE OR UPDATE ON public.order_events FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER order_lines_immutable BEFORE DELETE OR UPDATE ON public.order_lines FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER quotation_documents_immutable BEFORE DELETE OR UPDATE ON public.quotation_documents FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER quotation_responses_immutable BEFORE DELETE OR UPDATE ON public.quotation_responses FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER quotation_versions_immutable BEFORE DELETE OR UPDATE ON public.quotation_versions FOR EACH ROW EXECUTE FUNCTION public.reject_change();

CREATE TRIGGER tenants_status_guard BEFORE UPDATE ON public.tenants FOR EACH ROW EXECUTE FUNCTION public.guard_tenant_status();

ALTER TABLE ONLY public.account_notices
    ADD CONSTRAINT fk_account_notices_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.admin_events
    ADD CONSTRAINT fk_admin_events_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT fk_api_keys_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.approval_action_refs
    ADD CONSTRAINT fk_approval_action_refs_binding FOREIGN KEY (tenant_id, binding_id) REFERENCES public.channel_bindings(tenant_id, id);

ALTER TABLE ONLY public.approval_action_refs
    ADD CONSTRAINT fk_approval_action_refs_task FOREIGN KEY (tenant_id, task_id) REFERENCES public.approval_tasks(tenant_id, id);

ALTER TABLE ONLY public.approval_action_refs
    ADD CONSTRAINT fk_approval_action_refs_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.approval_decisions
    ADD CONSTRAINT fk_approval_decisions_task FOREIGN KEY (tenant_id, task_id) REFERENCES public.approval_tasks(tenant_id, id);

ALTER TABLE ONLY public.approval_decisions
    ADD CONSTRAINT fk_approval_decisions_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.approval_reply_prompts
    ADD CONSTRAINT fk_approval_reply_prompts_event FOREIGN KEY (tenant_id, inbox_event_id) REFERENCES public.inbox_events(tenant_id, id);

ALTER TABLE ONLY public.approval_reply_prompts
    ADD CONSTRAINT fk_approval_reply_prompts_message FOREIGN KEY (tenant_id, outbound_message_id) REFERENCES public.outbound_messages(tenant_id, id);

ALTER TABLE ONLY public.approval_reply_prompts
    ADD CONSTRAINT fk_approval_reply_prompts_ref FOREIGN KEY (reference_id) REFERENCES public.approval_action_refs(id);

ALTER TABLE ONLY public.approval_reply_prompts
    ADD CONSTRAINT fk_approval_reply_prompts_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.approval_tasks
    ADD CONSTRAINT fk_approval_tasks_quotation FOREIGN KEY (tenant_id, quotation_id) REFERENCES public.quotations(tenant_id, id);

ALTER TABLE ONLY public.approval_tasks
    ADD CONSTRAINT fk_approval_tasks_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.approval_tasks
    ADD CONSTRAINT fk_approval_tasks_version FOREIGN KEY (tenant_id, version_id) REFERENCES public.quotation_versions(tenant_id, id);

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT fk_attachments_enquiry FOREIGN KEY (tenant_id, enquiry_id) REFERENCES public.enquiries(tenant_id, id);

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT fk_attachments_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.catalogue_categories
    ADD CONSTRAINT fk_catalogue_categories_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.catalogue_changes
    ADD CONSTRAINT fk_catalogue_changes_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.catalogue_prices
    ADD CONSTRAINT fk_catalogue_prices_product FOREIGN KEY (tenant_id, product_id) REFERENCES public.catalogue_products(tenant_id, id);

ALTER TABLE ONLY public.catalogue_prices
    ADD CONSTRAINT fk_catalogue_prices_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.catalogue_products
    ADD CONSTRAINT fk_catalogue_products_category FOREIGN KEY (tenant_id, category_id) REFERENCES public.catalogue_categories(tenant_id, id);

ALTER TABLE ONLY public.catalogue_products
    ADD CONSTRAINT fk_catalogue_products_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.catalogue_stock
    ADD CONSTRAINT fk_catalogue_stock_product FOREIGN KEY (tenant_id, product_id) REFERENCES public.catalogue_products(tenant_id, id);

ALTER TABLE ONLY public.catalogue_stock
    ADD CONSTRAINT fk_catalogue_stock_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.channel_bindings
    ADD CONSTRAINT fk_channel_bindings_connection FOREIGN KEY (tenant_id, connection_ref) REFERENCES public.connections(tenant_id, id);

ALTER TABLE ONLY public.channel_bindings
    ADD CONSTRAINT fk_channel_bindings_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.channel_enrollments
    ADD CONSTRAINT fk_channel_enrollments_connection FOREIGN KEY (tenant_id, connection_ref) REFERENCES public.connections(tenant_id, id);

ALTER TABLE ONLY public.channel_enrollments
    ADD CONSTRAINT fk_channel_enrollments_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.connection_events
    ADD CONSTRAINT fk_connection_events_connection FOREIGN KEY (tenant_id, connection_id) REFERENCES public.connections(tenant_id, id);

ALTER TABLE ONLY public.connection_routes
    ADD CONSTRAINT fk_connection_routes_connection FOREIGN KEY (tenant_id, connection_id) REFERENCES public.connections(tenant_id, id);

ALTER TABLE ONLY public.connection_routes
    ADD CONSTRAINT fk_connection_routes_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.connections
    ADD CONSTRAINT fk_connections_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.contact_identities
    ADD CONSTRAINT fk_contact_identities_connection FOREIGN KEY (tenant_id, connection_ref) REFERENCES public.connections(tenant_id, id);

ALTER TABLE ONLY public.contact_identities
    ADD CONSTRAINT fk_contact_identities_contact FOREIGN KEY (tenant_id, contact_id) REFERENCES public.contacts(tenant_id, id);

ALTER TABLE ONLY public.contact_identities
    ADD CONSTRAINT fk_contact_identities_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT fk_contacts_merged FOREIGN KEY (tenant_id, merged_into_id) REFERENCES public.contacts(tenant_id, id);

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT fk_contacts_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.conversation_messages
    ADD CONSTRAINT fk_conversation_messages_conversation FOREIGN KEY (tenant_id, conversation_id) REFERENCES public.conversations(tenant_id, id);

ALTER TABLE ONLY public.conversation_messages
    ADD CONSTRAINT fk_conversation_messages_enquiry FOREIGN KEY (tenant_id, enquiry_id) REFERENCES public.enquiries(tenant_id, id);

ALTER TABLE ONLY public.conversation_messages
    ADD CONSTRAINT fk_conversation_messages_inbox_event FOREIGN KEY (tenant_id, inbox_event_id) REFERENCES public.inbox_events(tenant_id, id);

ALTER TABLE ONLY public.conversation_messages
    ADD CONSTRAINT fk_conversation_messages_outbound FOREIGN KEY (tenant_id, outbound_message_id) REFERENCES public.outbound_messages(tenant_id, id);

ALTER TABLE ONLY public.conversation_messages
    ADD CONSTRAINT fk_conversation_messages_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT fk_conversations_connection FOREIGN KEY (tenant_id, connection_ref) REFERENCES public.connections(tenant_id, id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT fk_conversations_contact FOREIGN KEY (tenant_id, contact_id) REFERENCES public.contacts(tenant_id, id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT fk_conversations_identity FOREIGN KEY (tenant_id, identity_id) REFERENCES public.contact_identities(tenant_id, id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT fk_conversations_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.customer_action_refs
    ADD CONSTRAINT fk_customer_action_refs_conversation FOREIGN KEY (tenant_id, conversation_id) REFERENCES public.conversations(tenant_id, id);

ALTER TABLE ONLY public.customer_action_refs
    ADD CONSTRAINT fk_customer_action_refs_identity FOREIGN KEY (tenant_id, identity_id) REFERENCES public.contact_identities(tenant_id, id);

ALTER TABLE ONLY public.customer_action_refs
    ADD CONSTRAINT fk_customer_action_refs_response FOREIGN KEY (response_id) REFERENCES public.quotation_responses(id);

ALTER TABLE ONLY public.customer_action_refs
    ADD CONSTRAINT fk_customer_action_refs_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.customer_action_refs
    ADD CONSTRAINT fk_customer_action_refs_version FOREIGN KEY (tenant_id, quotation_id, version) REFERENCES public.quotation_versions(tenant_id, quotation_id, version);

ALTER TABLE ONLY public.customer_sessions
    ADD CONSTRAINT fk_customer_sessions_connection FOREIGN KEY (tenant_id, connection_ref) REFERENCES public.connections(tenant_id, id);

ALTER TABLE ONLY public.customer_sessions
    ADD CONSTRAINT fk_customer_sessions_conversation FOREIGN KEY (tenant_id, conversation_id) REFERENCES public.conversations(tenant_id, id);

ALTER TABLE ONLY public.customer_sessions
    ADD CONSTRAINT fk_customer_sessions_identity FOREIGN KEY (tenant_id, identity_id) REFERENCES public.contact_identities(tenant_id, id);

ALTER TABLE ONLY public.customer_sessions
    ADD CONSTRAINT fk_customer_sessions_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.email_verifications
    ADD CONSTRAINT fk_email_verifications_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.enquiries
    ADD CONSTRAINT fk_enquiries_contact FOREIGN KEY (tenant_id, contact_id) REFERENCES public.contacts(tenant_id, id);

ALTER TABLE ONLY public.enquiries
    ADD CONSTRAINT fk_enquiries_conversation FOREIGN KEY (tenant_id, conversation_id) REFERENCES public.conversations(tenant_id, id);

ALTER TABLE ONLY public.enquiries
    ADD CONSTRAINT fk_enquiries_inbox_event FOREIGN KEY (tenant_id, inbox_event_id) REFERENCES public.inbox_events(tenant_id, id);

ALTER TABLE ONLY public.enquiries
    ADD CONSTRAINT fk_enquiries_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.inbox_events
    ADD CONSTRAINT fk_inbox_events_connection FOREIGN KEY (tenant_id, connection_ref) REFERENCES public.connections(tenant_id, id);

ALTER TABLE ONLY public.inbox_events
    ADD CONSTRAINT fk_inbox_events_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT fk_invitations_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT fk_jobs_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.manager_reply_classifications
    ADD CONSTRAINT fk_manager_reply_classifications_event FOREIGN KEY (tenant_id, inbox_event_id) REFERENCES public.inbox_events(tenant_id, id);

ALTER TABLE ONLY public.manager_reply_classifications
    ADD CONSTRAINT fk_manager_reply_classifications_ref FOREIGN KEY (reference_id) REFERENCES public.approval_action_refs(id);

ALTER TABLE ONLY public.manager_reply_classifications
    ADD CONSTRAINT fk_manager_reply_classifications_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.media_links
    ADD CONSTRAINT fk_media_links_document FOREIGN KEY (tenant_id, document_id) REFERENCES public.quotation_documents(tenant_id, id);

ALTER TABLE ONLY public.media_links
    ADD CONSTRAINT fk_media_links_message FOREIGN KEY (tenant_id, outbound_message_id) REFERENCES public.outbound_messages(tenant_id, id);

ALTER TABLE ONLY public.media_links
    ADD CONSTRAINT fk_media_links_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT fk_memberships_subject_users FOREIGN KEY (subject) REFERENCES public.users(subject);

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT fk_memberships_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.order_events
    ADD CONSTRAINT fk_order_events_order FOREIGN KEY (tenant_id, order_id) REFERENCES public.orders(tenant_id, id);

ALTER TABLE ONLY public.order_events
    ADD CONSTRAINT fk_order_events_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.order_lines
    ADD CONSTRAINT fk_order_lines_order FOREIGN KEY (tenant_id, order_id) REFERENCES public.orders(tenant_id, id);

ALTER TABLE ONLY public.order_lines
    ADD CONSTRAINT fk_order_lines_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT fk_orders_response FOREIGN KEY (response_id) REFERENCES public.quotation_responses(id);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT fk_orders_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT fk_orders_version FOREIGN KEY (tenant_id, quotation_id, version) REFERENCES public.quotation_versions(tenant_id, quotation_id, version);

ALTER TABLE ONLY public.outbound_messages
    ADD CONSTRAINT fk_outbound_messages_action_ref FOREIGN KEY (action_ref_id) REFERENCES public.approval_action_refs(id);

ALTER TABLE ONLY public.outbound_messages
    ADD CONSTRAINT fk_outbound_messages_connection FOREIGN KEY (tenant_id, connection_ref) REFERENCES public.connections(tenant_id, id);

ALTER TABLE ONLY public.outbound_messages
    ADD CONSTRAINT fk_outbound_messages_document FOREIGN KEY (tenant_id, document_id) REFERENCES public.quotation_documents(tenant_id, id);

ALTER TABLE ONLY public.outbound_messages
    ADD CONSTRAINT fk_outbound_messages_resent_from FOREIGN KEY (tenant_id, resent_from_id) REFERENCES public.outbound_messages(tenant_id, id);

ALTER TABLE ONLY public.outbound_messages
    ADD CONSTRAINT fk_outbound_messages_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.password_resets
    ADD CONSTRAINT fk_password_resets_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.quotation_deliveries
    ADD CONSTRAINT fk_quotation_deliveries_document FOREIGN KEY (tenant_id, document_id) REFERENCES public.quotation_documents(tenant_id, id);

ALTER TABLE ONLY public.quotation_deliveries
    ADD CONSTRAINT fk_quotation_deliveries_message FOREIGN KEY (tenant_id, outbound_message_id) REFERENCES public.outbound_messages(tenant_id, id);

ALTER TABLE ONLY public.quotation_deliveries
    ADD CONSTRAINT fk_quotation_deliveries_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.quotation_deliveries
    ADD CONSTRAINT fk_quotation_deliveries_version FOREIGN KEY (tenant_id, quotation_id, version) REFERENCES public.quotation_versions(tenant_id, quotation_id, version);

ALTER TABLE ONLY public.quotation_documents
    ADD CONSTRAINT fk_quotation_documents_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.quotation_documents
    ADD CONSTRAINT fk_quotation_documents_version FOREIGN KEY (tenant_id, quotation_id, version) REFERENCES public.quotation_versions(tenant_id, quotation_id, version);

ALTER TABLE ONLY public.quotation_responses
    ADD CONSTRAINT fk_quotation_responses_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.quotation_responses
    ADD CONSTRAINT fk_quotation_responses_version FOREIGN KEY (tenant_id, quotation_id, version) REFERENCES public.quotation_versions(tenant_id, quotation_id, version);

ALTER TABLE ONLY public.quotation_versions
    ADD CONSTRAINT fk_quotation_versions_quotation FOREIGN KEY (tenant_id, quotation_id) REFERENCES public.quotations(tenant_id, id);

ALTER TABLE ONLY public.quotation_versions
    ADD CONSTRAINT fk_quotation_versions_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.quotations
    ADD CONSTRAINT fk_quotations_enquiry FOREIGN KEY (tenant_id, enquiry_id) REFERENCES public.enquiries(tenant_id, id);

ALTER TABLE ONLY public.quotations
    ADD CONSTRAINT fk_quotations_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.tenant_pricing_policies
    ADD CONSTRAINT fk_tenant_pricing_policies_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.tenant_profiles
    ADD CONSTRAINT fk_tenant_profiles_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.user_credentials
    ADD CONSTRAINT fk_user_credentials_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.workflow_runs
    ADD CONSTRAINT fk_workflow_runs_enquiry FOREIGN KEY (tenant_id, enquiry_id) REFERENCES public.enquiries(tenant_id, id);

ALTER TABLE ONLY public.workflow_runs
    ADD CONSTRAINT fk_workflow_runs_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

CREATE POLICY account_email_jobs ON public.jobs FOR INSERT TO sales_app WITH CHECK (((tenant_id IS NULL) AND ((kind)::text = 'send_account_email'::text)));

ALTER TABLE public.account_notices ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.admin_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY api_key_lookup ON public.api_keys FOR SELECT TO sales_app USING ((id = (NULLIF(current_setting('app.api_key_id'::text, true), ''::text))::uuid));

ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.approval_action_refs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.approval_decisions ENABLE ROW LEVEL SECURITY;

CREATE POLICY approval_link_lookup ON public.approval_action_refs FOR SELECT TO sales_app USING (((token_sha256)::text = NULLIF(current_setting('app.approval_token_sha256'::text, true), ''::text)));

ALTER TABLE public.approval_reply_prompts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.approval_tasks ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.attachments ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.catalogue_categories ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.catalogue_changes ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.catalogue_prices ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.catalogue_products ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.catalogue_stock ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.channel_bindings ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.channel_enrollments ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.connection_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY connection_handle_lookup ON public.connections FOR SELECT TO sales_app USING (((public_handle)::text = NULLIF(current_setting('app.public_handle'::text, true), ''::text)));

CREATE POLICY connection_hook_lookup ON public.connections FOR SELECT TO sales_app USING ((id = (NULLIF(current_setting('app.connection_id'::text, true), ''::text))::uuid));

CREATE POLICY connection_route_lookup ON public.connection_routes FOR SELECT TO sales_app, sales_worker USING ((((provider_key)::text = NULLIF(current_setting('app.route_provider'::text, true), ''::text)) AND ((routing_key)::text = NULLIF(current_setting('app.route_key'::text, true), ''::text))));

ALTER TABLE public.connection_routes ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.connections ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.contact_identities ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.contacts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.conversation_messages ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.conversations ENABLE ROW LEVEL SECURITY;

CREATE POLICY credentials_by_user_insert ON public.user_credentials FOR INSERT TO sales_app WITH CHECK ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY credentials_by_user_select ON public.user_credentials FOR SELECT TO sales_app USING ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY credentials_by_user_update ON public.user_credentials FOR UPDATE TO sales_app USING ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)) WITH CHECK ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY credentials_owner_all ON public.user_credentials TO sales_owner USING (true) WITH CHECK (true);

CREATE POLICY customer_action_lookup ON public.customer_action_refs FOR SELECT TO sales_app USING (((token_sha256)::text = NULLIF(current_setting('app.action_token_sha256'::text, true), ''::text)));

ALTER TABLE public.customer_action_refs ENABLE ROW LEVEL SECURITY;

CREATE POLICY customer_session_lookup ON public.customer_sessions FOR SELECT TO sales_app USING ((id = (NULLIF(current_setting('app.customer_session_id'::text, true), ''::text))::uuid));

ALTER TABLE public.customer_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.email_verifications ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.enquiries ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.inbox_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY invitation_token_lookup_select ON public.invitations FOR SELECT TO sales_app USING (((token_sha256)::text = NULLIF(current_setting('app.account_token_sha256'::text, true), ''::text)));

CREATE POLICY invitation_token_owner_select ON public.invitations FOR SELECT TO sales_owner USING (((token_sha256)::text = NULLIF(current_setting('app.account_token_sha256'::text, true), ''::text)));

ALTER TABLE public.invitations ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.manager_reply_classifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY media_link_lookup ON public.media_links FOR SELECT TO sales_app USING (((token_sha256)::text = NULLIF(current_setting('app.media_token_sha256'::text, true), ''::text)));

ALTER TABLE public.media_links ENABLE ROW LEVEL SECURITY;

CREATE POLICY membership_self_lookup ON public.memberships FOR SELECT TO sales_app USING (((subject)::text = NULLIF(current_setting('app.subject'::text, true), ''::text)));

ALTER TABLE public.memberships ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.order_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.order_lines ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.outbound_messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY own_rows_insert ON public.account_notices FOR INSERT TO sales_app WITH CHECK ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY own_rows_insert ON public.email_verifications FOR INSERT TO sales_app WITH CHECK ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY own_rows_insert ON public.password_resets FOR INSERT TO sales_app WITH CHECK ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY own_rows_select ON public.email_verifications FOR SELECT TO sales_app USING ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY own_rows_select ON public.password_resets FOR SELECT TO sales_app USING ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY own_rows_update ON public.email_verifications FOR UPDATE TO sales_app USING ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)) WITH CHECK ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY own_rows_update ON public.password_resets FOR UPDATE TO sales_app USING ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)) WITH CHECK ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

ALTER TABLE public.password_resets ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.quotation_deliveries ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.quotation_documents ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.quotation_responses ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.quotation_versions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.quotations ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.rate_limit_counters ENABLE ROW LEVEL SECURITY;

CREATE POLICY rate_limits_app_all ON public.rate_limit_counters TO sales_app USING (true) WITH CHECK (true);

CREATE POLICY session_create ON public.user_sessions FOR INSERT TO sales_app WITH CHECK (true);

CREATE POLICY session_for_subject_select ON public.user_sessions FOR SELECT TO sales_app USING (((subject)::text = NULLIF(current_setting('app.subject'::text, true), ''::text)));

CREATE POLICY session_for_subject_update ON public.user_sessions FOR UPDATE TO sales_app USING (((subject)::text = NULLIF(current_setting('app.subject'::text, true), ''::text))) WITH CHECK (((subject)::text = NULLIF(current_setting('app.subject'::text, true), ''::text)));

CREATE POLICY session_lookup ON public.user_sessions FOR SELECT TO sales_app USING (((token_sha256)::text = NULLIF(current_setting('app.session_token_sha256'::text, true), ''::text)));

CREATE POLICY session_revoke ON public.user_sessions FOR UPDATE TO sales_app USING (((token_sha256)::text = NULLIF(current_setting('app.session_token_sha256'::text, true), ''::text))) WITH CHECK (((token_sha256)::text = NULLIF(current_setting('app.session_token_sha256'::text, true), ''::text)));

CREATE POLICY tenant_isolation ON public.admin_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.api_keys USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.approval_action_refs USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.approval_decisions USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.approval_reply_prompts USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.approval_tasks USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.attachments USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.catalogue_categories USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.catalogue_changes USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.catalogue_prices USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.catalogue_products USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.catalogue_stock USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.channel_bindings USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.channel_enrollments USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.connection_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.connection_routes USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.connections USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.contact_identities USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.contacts USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.conversation_messages USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.conversations USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.customer_action_refs USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.customer_sessions USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.enquiries USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.inbox_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.invitations USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.jobs USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.manager_reply_classifications USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.media_links USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.memberships USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.order_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.order_lines USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.orders USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.outbound_messages USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.quotation_deliveries USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.quotation_documents USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.quotation_responses USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.quotation_versions USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.quotations USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.tenant_pricing_policies USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.tenant_profiles USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.tenants USING ((id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.workflow_runs USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_member_lookup ON public.tenants FOR SELECT TO sales_app USING ((EXISTS ( SELECT 1
   FROM public.memberships m
  WHERE ((m.tenant_id = tenants.id) AND ((m.subject)::text = NULLIF(current_setting('app.subject'::text, true), ''::text))))));

ALTER TABLE public.tenant_pricing_policies ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.tenant_profiles ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;

CREATE POLICY token_lookup_select ON public.email_verifications FOR SELECT TO sales_app USING (((token_sha256)::text = NULLIF(current_setting('app.account_token_sha256'::text, true), ''::text)));

CREATE POLICY token_lookup_select ON public.password_resets FOR SELECT TO sales_app USING (((token_sha256)::text = NULLIF(current_setting('app.account_token_sha256'::text, true), ''::text)));

CREATE POLICY token_lookup_update ON public.email_verifications FOR UPDATE TO sales_app USING (((token_sha256)::text = NULLIF(current_setting('app.account_token_sha256'::text, true), ''::text))) WITH CHECK (((token_sha256)::text = NULLIF(current_setting('app.account_token_sha256'::text, true), ''::text)));

CREATE POLICY token_lookup_update ON public.password_resets FOR UPDATE TO sales_app USING (((token_sha256)::text = NULLIF(current_setting('app.account_token_sha256'::text, true), ''::text))) WITH CHECK (((token_sha256)::text = NULLIF(current_setting('app.account_token_sha256'::text, true), ''::text)));

ALTER TABLE public.user_credentials ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.user_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;

CREATE POLICY users_by_id_select ON public.users FOR SELECT TO sales_app USING ((id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY users_by_id_update ON public.users FOR UPDATE TO sales_app USING ((id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)) WITH CHECK ((id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid));

CREATE POLICY users_email_lookup_select ON public.users FOR SELECT TO sales_app USING (((email_normalized)::text = NULLIF(current_setting('app.user_email'::text, true), ''::text)));

CREATE POLICY users_owner_all ON public.users TO sales_owner USING (true) WITH CHECK (true);

CREATE POLICY users_self_select ON public.users FOR SELECT TO sales_app USING (((subject)::text = NULLIF(current_setting('app.subject'::text, true), ''::text)));

CREATE POLICY users_self_update ON public.users FOR UPDATE TO sales_app USING (((subject)::text = NULLIF(current_setting('app.subject'::text, true), ''::text))) WITH CHECK (((subject)::text = NULLIF(current_setting('app.subject'::text, true), ''::text)));

CREATE POLICY users_signup_insert ON public.users FOR INSERT TO sales_app WITH CHECK ((((email_normalized)::text = NULLIF(current_setting('app.user_email'::text, true), ''::text)) AND ((subject)::text = ('user:'::text || (id)::text))));

CREATE POLICY users_tenant_directory_select ON public.users FOR SELECT TO sales_app USING ((EXISTS ( SELECT 1
   FROM public.memberships m
  WHERE (((m.subject)::text = (users.subject)::text) AND (m.tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)))));

CREATE POLICY worker_queue ON public.jobs TO sales_worker USING (true) WITH CHECK (true);

CREATE POLICY worker_send_select ON public.account_notices FOR SELECT TO sales_worker USING ((id = (NULLIF(current_setting('app.account_ref_id'::text, true), ''::text))::uuid));

CREATE POLICY worker_send_select ON public.email_verifications FOR SELECT TO sales_worker USING ((id = (NULLIF(current_setting('app.account_ref_id'::text, true), ''::text))::uuid));

CREATE POLICY worker_send_select ON public.password_resets FOR SELECT TO sales_worker USING ((id = (NULLIF(current_setting('app.account_ref_id'::text, true), ''::text))::uuid));

CREATE POLICY worker_send_update ON public.account_notices FOR UPDATE TO sales_worker USING ((id = (NULLIF(current_setting('app.account_ref_id'::text, true), ''::text))::uuid)) WITH CHECK ((id = (NULLIF(current_setting('app.account_ref_id'::text, true), ''::text))::uuid));

CREATE POLICY worker_send_update ON public.email_verifications FOR UPDATE TO sales_worker USING ((id = (NULLIF(current_setting('app.account_ref_id'::text, true), ''::text))::uuid)) WITH CHECK ((id = (NULLIF(current_setting('app.account_ref_id'::text, true), ''::text))::uuid));

CREATE POLICY worker_send_update ON public.password_resets FOR UPDATE TO sales_worker USING ((id = (NULLIF(current_setting('app.account_ref_id'::text, true), ''::text))::uuid)) WITH CHECK ((id = (NULLIF(current_setting('app.account_ref_id'::text, true), ''::text))::uuid));

ALTER TABLE public.workflow_runs ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON FUNCTION public.accept_invitation(p_token_sha256 text, p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.accept_invitation(p_token_sha256 text, p_user_id uuid) TO sales_app;

REVOKE ALL ON FUNCTION public.change_member_roles(p_tenant uuid, p_user_id uuid, p_roles text[]) FROM PUBLIC;
GRANT ALL ON FUNCTION public.change_member_roles(p_tenant uuid, p_user_id uuid, p_roles text[]) TO sales_app;

REVOKE ALL ON FUNCTION public.create_business(p_user_id uuid, p_name text, p_country text, p_time_zone text, p_currency text, p_tax_rate numeric, p_quote_validity_days integer, p_catalogue_provider text, p_catalogue_name text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.create_business(p_user_id uuid, p_name text, p_country text, p_time_zone text, p_currency text, p_tax_rate numeric, p_quote_validity_days integer, p_catalogue_provider text, p_catalogue_name text) TO sales_app;

REVOKE ALL ON FUNCTION public.remove_member(p_tenant uuid, p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.remove_member(p_tenant uuid, p_user_id uuid) TO sales_app;

GRANT SELECT ON TABLE public.account_notices TO sales_worker;

GRANT INSERT(id) ON TABLE public.account_notices TO sales_app;

GRANT INSERT(user_id) ON TABLE public.account_notices TO sales_app;

GRANT INSERT(email_normalized) ON TABLE public.account_notices TO sales_app;

GRANT INSERT(purpose) ON TABLE public.account_notices TO sales_app;

GRANT UPDATE(email_status) ON TABLE public.account_notices TO sales_worker;

GRANT UPDATE(sent_at) ON TABLE public.account_notices TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.admin_events TO sales_app;

GRANT SELECT ON TABLE public.alembic_version TO sales_app;

GRANT SELECT ON TABLE public.api_keys TO sales_app;

GRANT INSERT(id) ON TABLE public.api_keys TO sales_app;

GRANT INSERT(tenant_id) ON TABLE public.api_keys TO sales_app;

GRANT INSERT(name) ON TABLE public.api_keys TO sales_app;

GRANT INSERT(role) ON TABLE public.api_keys TO sales_app;

GRANT INSERT(secret_sha256) ON TABLE public.api_keys TO sales_app;

GRANT UPDATE(revoked_at) ON TABLE public.api_keys TO sales_app;

GRANT INSERT(created_by) ON TABLE public.api_keys TO sales_app;

GRANT UPDATE(revoked_by) ON TABLE public.api_keys TO sales_app;

GRANT SELECT ON TABLE public.approval_action_refs TO sales_app;
GRANT SELECT,INSERT ON TABLE public.approval_action_refs TO sales_worker;

GRANT UPDATE(token_sha256) ON TABLE public.approval_action_refs TO sales_worker;

GRANT UPDATE(expires_at) ON TABLE public.approval_action_refs TO sales_worker;

GRANT UPDATE(sent_at) ON TABLE public.approval_action_refs TO sales_worker;

GRANT UPDATE(consumed_at) ON TABLE public.approval_action_refs TO sales_app;
GRANT UPDATE(consumed_at) ON TABLE public.approval_action_refs TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.approval_decisions TO sales_app;
GRANT SELECT,INSERT ON TABLE public.approval_decisions TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.approval_reply_prompts TO sales_app;
GRANT SELECT,INSERT ON TABLE public.approval_reply_prompts TO sales_worker;

GRANT UPDATE(token_sha256) ON TABLE public.approval_reply_prompts TO sales_app;
GRANT UPDATE(token_sha256) ON TABLE public.approval_reply_prompts TO sales_worker;

GRANT UPDATE(outbound_message_id) ON TABLE public.approval_reply_prompts TO sales_app;
GRANT UPDATE(outbound_message_id) ON TABLE public.approval_reply_prompts TO sales_worker;

GRANT UPDATE(closed_at) ON TABLE public.approval_reply_prompts TO sales_app;
GRANT UPDATE(closed_at) ON TABLE public.approval_reply_prompts TO sales_worker;

GRANT UPDATE(outcome) ON TABLE public.approval_reply_prompts TO sales_app;
GRANT UPDATE(outcome) ON TABLE public.approval_reply_prompts TO sales_worker;

GRANT SELECT ON TABLE public.approval_tasks TO sales_app;
GRANT SELECT,INSERT ON TABLE public.approval_tasks TO sales_worker;

GRANT UPDATE(status) ON TABLE public.approval_tasks TO sales_app;
GRANT UPDATE(status) ON TABLE public.approval_tasks TO sales_worker;

GRANT UPDATE(decided_at) ON TABLE public.approval_tasks TO sales_app;
GRANT UPDATE(decided_at) ON TABLE public.approval_tasks TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.attachments TO sales_app;
GRANT SELECT,UPDATE ON TABLE public.attachments TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.catalogue_categories TO sales_app;
GRANT SELECT ON TABLE public.catalogue_categories TO sales_worker;

GRANT UPDATE(name) ON TABLE public.catalogue_categories TO sales_app;

GRANT UPDATE(archived_at) ON TABLE public.catalogue_categories TO sales_app;

GRANT UPDATE(row_version) ON TABLE public.catalogue_categories TO sales_app;

GRANT UPDATE(updated_at) ON TABLE public.catalogue_categories TO sales_app;

GRANT UPDATE(updated_by) ON TABLE public.catalogue_categories TO sales_app;

GRANT SELECT,INSERT ON TABLE public.catalogue_changes TO sales_app;

GRANT SELECT,INSERT ON TABLE public.catalogue_prices TO sales_app;
GRANT SELECT ON TABLE public.catalogue_prices TO sales_worker;

GRANT SELECT,INSERT,DELETE ON TABLE public.catalogue_products TO sales_app;
GRANT SELECT ON TABLE public.catalogue_products TO sales_worker;

GRANT UPDATE(name) ON TABLE public.catalogue_products TO sales_app;

GRANT UPDATE(description) ON TABLE public.catalogue_products TO sales_app;

GRANT UPDATE(category_id) ON TABLE public.catalogue_products TO sales_app;

GRANT UPDATE(unit) ON TABLE public.catalogue_products TO sales_app;

GRANT UPDATE(minimum_quantity) ON TABLE public.catalogue_products TO sales_app;

GRANT UPDATE(quantity_step) ON TABLE public.catalogue_products TO sales_app;

GRANT UPDATE(search_terms) ON TABLE public.catalogue_products TO sales_app;


GRANT UPDATE(archived_at) ON TABLE public.catalogue_products TO sales_app;

GRANT UPDATE(archived_by) ON TABLE public.catalogue_products TO sales_app;

GRANT UPDATE(row_version) ON TABLE public.catalogue_products TO sales_app;

GRANT UPDATE(updated_at) ON TABLE public.catalogue_products TO sales_app;

GRANT UPDATE(updated_by) ON TABLE public.catalogue_products TO sales_app;

GRANT SELECT,INSERT ON TABLE public.catalogue_stock TO sales_app;
GRANT SELECT ON TABLE public.catalogue_stock TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.channel_bindings TO sales_app;
GRANT SELECT,INSERT ON TABLE public.channel_bindings TO sales_worker;

GRANT UPDATE(revoked_at) ON TABLE public.channel_bindings TO sales_app;

GRANT SELECT,INSERT ON TABLE public.channel_enrollments TO sales_app;
GRANT SELECT ON TABLE public.channel_enrollments TO sales_worker;

GRANT UPDATE(consumed_at) ON TABLE public.channel_enrollments TO sales_app;
GRANT UPDATE(consumed_at) ON TABLE public.channel_enrollments TO sales_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.checkpoint_blobs TO sales_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.checkpoint_migrations TO sales_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.checkpoint_writes TO sales_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.checkpoints TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.connection_events TO sales_app;
GRANT SELECT,INSERT ON TABLE public.connection_events TO sales_worker;

GRANT SELECT,INSERT,DELETE ON TABLE public.connection_routes TO sales_app;
GRANT SELECT,INSERT,DELETE ON TABLE public.connection_routes TO sales_worker;

GRANT SELECT ON TABLE public.connections TO sales_app;
GRANT SELECT ON TABLE public.connections TO sales_worker;

GRANT INSERT(id) ON TABLE public.connections TO sales_app;

GRANT INSERT(tenant_id) ON TABLE public.connections TO sales_app;

GRANT INSERT(provider_key) ON TABLE public.connections TO sales_app;

GRANT INSERT(display_name) ON TABLE public.connections TO sales_app;

GRANT INSERT(purposes) ON TABLE public.connections TO sales_app;

GRANT INSERT(config),UPDATE(config) ON TABLE public.connections TO sales_app;
GRANT UPDATE(config) ON TABLE public.connections TO sales_worker;

GRANT INSERT(secret_refs),UPDATE(secret_refs) ON TABLE public.connections TO sales_app;
GRANT UPDATE(secret_refs) ON TABLE public.connections TO sales_worker;

GRANT INSERT(account_ref) ON TABLE public.connections TO sales_app;

GRANT INSERT(status),UPDATE(status) ON TABLE public.connections TO sales_app;
GRANT UPDATE(status) ON TABLE public.connections TO sales_worker;

GRANT UPDATE(last_error) ON TABLE public.connections TO sales_app;
GRANT UPDATE(last_error) ON TABLE public.connections TO sales_worker;

GRANT INSERT(created_by) ON TABLE public.connections TO sales_app;

GRANT UPDATE(updated_at) ON TABLE public.connections TO sales_app;
GRANT UPDATE(updated_at) ON TABLE public.connections TO sales_worker;

GRANT UPDATE(public_handle) ON TABLE public.connections TO sales_app;

GRANT UPDATE(public_handle_changed_at) ON TABLE public.connections TO sales_app;

GRANT INSERT(pending_action),UPDATE(pending_action) ON TABLE public.connections TO sales_app;
GRANT UPDATE(pending_action) ON TABLE public.connections TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.contact_identities TO sales_app;
GRANT SELECT,INSERT ON TABLE public.contact_identities TO sales_worker;

GRANT UPDATE(contact_id) ON TABLE public.contact_identities TO sales_app;
GRANT UPDATE(contact_id) ON TABLE public.contact_identities TO sales_worker;

GRANT UPDATE(consent_status) ON TABLE public.contact_identities TO sales_app;
GRANT UPDATE(consent_status) ON TABLE public.contact_identities TO sales_worker;

GRANT UPDATE(consent_changed_at) ON TABLE public.contact_identities TO sales_app;
GRANT UPDATE(consent_changed_at) ON TABLE public.contact_identities TO sales_worker;

GRANT UPDATE(consent_source) ON TABLE public.contact_identities TO sales_app;
GRANT UPDATE(consent_source) ON TABLE public.contact_identities TO sales_worker;

GRANT UPDATE(last_seen_at) ON TABLE public.contact_identities TO sales_app;
GRANT UPDATE(last_seen_at) ON TABLE public.contact_identities TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.contacts TO sales_app;
GRANT SELECT,INSERT ON TABLE public.contacts TO sales_worker;

GRANT UPDATE(merged_into_id) ON TABLE public.contacts TO sales_app;

GRANT UPDATE(merged_at) ON TABLE public.contacts TO sales_app;

GRANT UPDATE(merged_by) ON TABLE public.contacts TO sales_app;

GRANT UPDATE(updated_at) ON TABLE public.contacts TO sales_app;

GRANT SELECT,INSERT ON TABLE public.conversation_messages TO sales_app;
GRANT SELECT,INSERT ON TABLE public.conversation_messages TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.conversations TO sales_app;
GRANT SELECT,INSERT ON TABLE public.conversations TO sales_worker;

GRANT UPDATE(contact_id) ON TABLE public.conversations TO sales_app;
GRANT UPDATE(contact_id) ON TABLE public.conversations TO sales_worker;

GRANT UPDATE(last_message_at) ON TABLE public.conversations TO sales_app;
GRANT UPDATE(last_message_at) ON TABLE public.conversations TO sales_worker;

GRANT UPDATE(last_inbound_at) ON TABLE public.conversations TO sales_app;
GRANT UPDATE(last_inbound_at) ON TABLE public.conversations TO sales_worker;

GRANT SELECT ON TABLE public.customer_action_refs TO sales_app;
GRANT SELECT,INSERT ON TABLE public.customer_action_refs TO sales_worker;

GRANT UPDATE(outbound_message_id) ON TABLE public.customer_action_refs TO sales_worker;

GRANT UPDATE(token_sha256) ON TABLE public.customer_action_refs TO sales_worker;

GRANT UPDATE(consumed_at) ON TABLE public.customer_action_refs TO sales_app;
GRANT UPDATE(consumed_at) ON TABLE public.customer_action_refs TO sales_worker;

GRANT UPDATE(response_id) ON TABLE public.customer_action_refs TO sales_app;
GRANT UPDATE(response_id) ON TABLE public.customer_action_refs TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.customer_sessions TO sales_app;
GRANT SELECT ON TABLE public.customer_sessions TO sales_worker;

GRANT UPDATE(last_seen_at) ON TABLE public.customer_sessions TO sales_app;

GRANT UPDATE(expires_at) ON TABLE public.customer_sessions TO sales_app;

GRANT UPDATE(revoked_at) ON TABLE public.customer_sessions TO sales_app;

GRANT SELECT ON TABLE public.email_verifications TO sales_app;
GRANT SELECT ON TABLE public.email_verifications TO sales_worker;

GRANT INSERT(id) ON TABLE public.email_verifications TO sales_app;

GRANT INSERT(user_id) ON TABLE public.email_verifications TO sales_app;

GRANT INSERT(email_normalized) ON TABLE public.email_verifications TO sales_app;

GRANT UPDATE(token_sha256) ON TABLE public.email_verifications TO sales_worker;

GRANT UPDATE(email_status) ON TABLE public.email_verifications TO sales_worker;

GRANT UPDATE(expires_at) ON TABLE public.email_verifications TO sales_worker;

GRANT UPDATE(sent_at) ON TABLE public.email_verifications TO sales_worker;

GRANT UPDATE(consumed_at) ON TABLE public.email_verifications TO sales_app;

GRANT UPDATE(revoked_at) ON TABLE public.email_verifications TO sales_app;

GRANT SELECT,INSERT ON TABLE public.enquiries TO sales_app;
GRANT SELECT,INSERT,UPDATE ON TABLE public.enquiries TO sales_worker;

GRANT UPDATE(status) ON TABLE public.enquiries TO sales_app;

GRANT UPDATE(updated_at) ON TABLE public.enquiries TO sales_app;

GRANT UPDATE(contact_id) ON TABLE public.enquiries TO sales_app;

GRANT SELECT,INSERT ON TABLE public.inbox_events TO sales_app;
GRANT SELECT,INSERT ON TABLE public.inbox_events TO sales_worker;

GRANT UPDATE(processed_at) ON TABLE public.inbox_events TO sales_app;
GRANT UPDATE(processed_at) ON TABLE public.inbox_events TO sales_worker;

GRANT UPDATE(outcome) ON TABLE public.inbox_events TO sales_app;
GRANT UPDATE(outcome) ON TABLE public.inbox_events TO sales_worker;

GRANT SELECT ON TABLE public.invitations TO sales_app;
GRANT SELECT ON TABLE public.invitations TO sales_worker;

GRANT INSERT(id) ON TABLE public.invitations TO sales_app;

GRANT INSERT(tenant_id) ON TABLE public.invitations TO sales_app;

GRANT INSERT(email) ON TABLE public.invitations TO sales_app;

GRANT INSERT(email_normalized) ON TABLE public.invitations TO sales_app;

GRANT INSERT(roles) ON TABLE public.invitations TO sales_app;

GRANT INSERT(invited_by) ON TABLE public.invitations TO sales_app;

GRANT UPDATE(token_sha256) ON TABLE public.invitations TO sales_app;
GRANT UPDATE(token_sha256) ON TABLE public.invitations TO sales_worker;

GRANT UPDATE(email_status) ON TABLE public.invitations TO sales_app;
GRANT UPDATE(email_status) ON TABLE public.invitations TO sales_worker;

GRANT UPDATE(expires_at) ON TABLE public.invitations TO sales_app;
GRANT UPDATE(expires_at) ON TABLE public.invitations TO sales_worker;

GRANT UPDATE(sent_at) ON TABLE public.invitations TO sales_worker;

GRANT UPDATE(revoked_at) ON TABLE public.invitations TO sales_app;

GRANT UPDATE(revoked_by) ON TABLE public.invitations TO sales_app;

GRANT INSERT ON TABLE public.jobs TO sales_app;
GRANT SELECT,INSERT,UPDATE ON TABLE public.jobs TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.manager_reply_classifications TO sales_worker;
GRANT SELECT ON TABLE public.manager_reply_classifications TO sales_app;

GRANT SELECT,INSERT ON TABLE public.media_links TO sales_worker;
GRANT SELECT ON TABLE public.media_links TO sales_app;

GRANT SELECT ON TABLE public.memberships TO sales_app;
GRANT SELECT ON TABLE public.memberships TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.order_events TO sales_app;
GRANT SELECT,INSERT ON TABLE public.order_events TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.order_lines TO sales_app;
GRANT SELECT,INSERT ON TABLE public.order_lines TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.orders TO sales_app;
GRANT SELECT,INSERT ON TABLE public.orders TO sales_worker;

GRANT UPDATE(status) ON TABLE public.orders TO sales_app;
GRANT UPDATE(status) ON TABLE public.orders TO sales_worker;

GRANT UPDATE(hold_reason) ON TABLE public.orders TO sales_app;
GRANT UPDATE(hold_reason) ON TABLE public.orders TO sales_worker;

GRANT UPDATE(updated_at) ON TABLE public.orders TO sales_app;
GRANT UPDATE(updated_at) ON TABLE public.orders TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.outbound_messages TO sales_app;
GRANT SELECT,INSERT ON TABLE public.outbound_messages TO sales_worker;

GRANT UPDATE(status) ON TABLE public.outbound_messages TO sales_worker;

GRANT UPDATE(provider_message_id) ON TABLE public.outbound_messages TO sales_worker;

GRANT UPDATE(last_error) ON TABLE public.outbound_messages TO sales_worker;

GRANT UPDATE(updated_at) ON TABLE public.outbound_messages TO sales_worker;

GRANT UPDATE(sent_at) ON TABLE public.outbound_messages TO sales_worker;

GRANT UPDATE(delivered_at) ON TABLE public.outbound_messages TO sales_worker;

GRANT UPDATE(action_ref_id) ON TABLE public.outbound_messages TO sales_worker;

GRANT SELECT ON TABLE public.password_resets TO sales_app;
GRANT SELECT ON TABLE public.password_resets TO sales_worker;

GRANT INSERT(id) ON TABLE public.password_resets TO sales_app;

GRANT INSERT(user_id) ON TABLE public.password_resets TO sales_app;

GRANT INSERT(email_normalized) ON TABLE public.password_resets TO sales_app;

GRANT UPDATE(token_sha256) ON TABLE public.password_resets TO sales_worker;

GRANT UPDATE(email_status) ON TABLE public.password_resets TO sales_worker;

GRANT UPDATE(expires_at) ON TABLE public.password_resets TO sales_worker;

GRANT UPDATE(sent_at) ON TABLE public.password_resets TO sales_worker;

GRANT UPDATE(consumed_at) ON TABLE public.password_resets TO sales_app;

GRANT UPDATE(revoked_at) ON TABLE public.password_resets TO sales_app;

GRANT SELECT,INSERT ON TABLE public.quotation_deliveries TO sales_worker;
GRANT SELECT ON TABLE public.quotation_deliveries TO sales_app;

GRANT UPDATE(status) ON TABLE public.quotation_deliveries TO sales_worker;

GRANT UPDATE(document_id) ON TABLE public.quotation_deliveries TO sales_worker;

GRANT UPDATE(provider_reference) ON TABLE public.quotation_deliveries TO sales_worker;

GRANT UPDATE(last_error) ON TABLE public.quotation_deliveries TO sales_worker;

GRANT UPDATE(updated_at) ON TABLE public.quotation_deliveries TO sales_worker;

GRANT UPDATE(delivered_at) ON TABLE public.quotation_deliveries TO sales_worker;

GRANT UPDATE(first_downloaded_at) ON TABLE public.quotation_deliveries TO sales_app;

GRANT UPDATE(outbound_message_id) ON TABLE public.quotation_deliveries TO sales_worker;
GRANT UPDATE(outbound_message_id) ON TABLE public.quotation_deliveries TO sales_app;

GRANT SELECT,INSERT ON TABLE public.quotation_documents TO sales_worker;
GRANT SELECT ON TABLE public.quotation_documents TO sales_app;

GRANT SELECT,INSERT ON TABLE public.quotation_responses TO sales_app;
GRANT SELECT,INSERT ON TABLE public.quotation_responses TO sales_worker;

GRANT SELECT,INSERT ON TABLE public.quotation_versions TO sales_app;
GRANT SELECT,INSERT ON TABLE public.quotation_versions TO sales_worker;

GRANT SELECT ON TABLE public.quotations TO sales_app;
GRANT SELECT,INSERT ON TABLE public.quotations TO sales_worker;

GRANT UPDATE(status) ON TABLE public.quotations TO sales_app;
GRANT UPDATE(status) ON TABLE public.quotations TO sales_worker;

GRANT UPDATE(current_version) ON TABLE public.quotations TO sales_app;
GRANT UPDATE(current_version) ON TABLE public.quotations TO sales_worker;

GRANT UPDATE(pricing_issues) ON TABLE public.quotations TO sales_worker;
GRANT UPDATE(pricing_issues) ON TABLE public.quotations TO sales_app;

GRANT UPDATE(updated_at) ON TABLE public.quotations TO sales_app;
GRANT UPDATE(updated_at) ON TABLE public.quotations TO sales_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.rate_limit_counters TO sales_app;

GRANT SELECT ON TABLE public.tenant_pricing_policies TO sales_app;
GRANT SELECT ON TABLE public.tenant_pricing_policies TO sales_worker;

GRANT UPDATE(version) ON TABLE public.tenant_pricing_policies TO sales_app;

GRANT UPDATE(currency) ON TABLE public.tenant_pricing_policies TO sales_app;

GRANT UPDATE(minor_units) ON TABLE public.tenant_pricing_policies TO sales_app;

GRANT UPDATE(rounding_mode) ON TABLE public.tenant_pricing_policies TO sales_app;

GRANT UPDATE(tax_rate) ON TABLE public.tenant_pricing_policies TO sales_app;

GRANT UPDATE(max_price_age_seconds) ON TABLE public.tenant_pricing_policies TO sales_app;

GRANT UPDATE(max_stock_age_seconds) ON TABLE public.tenant_pricing_policies TO sales_app;

GRANT UPDATE(updated_at) ON TABLE public.tenant_pricing_policies TO sales_app;

GRANT UPDATE(quote_validity_days) ON TABLE public.tenant_pricing_policies TO sales_app;

GRANT UPDATE(updated_by) ON TABLE public.tenant_pricing_policies TO sales_app;

GRANT SELECT ON TABLE public.tenant_profiles TO sales_app;
GRANT SELECT ON TABLE public.tenant_profiles TO sales_worker;

GRANT UPDATE(legal_name) ON TABLE public.tenant_profiles TO sales_app;

GRANT UPDATE(address) ON TABLE public.tenant_profiles TO sales_app;

GRANT UPDATE(tax_id_label) ON TABLE public.tenant_profiles TO sales_app;

GRANT UPDATE(tax_id) ON TABLE public.tenant_profiles TO sales_app;

GRANT UPDATE(logo_storage_key) ON TABLE public.tenant_profiles TO sales_app;

GRANT UPDATE(logo_sha256) ON TABLE public.tenant_profiles TO sales_app;

GRANT UPDATE(version) ON TABLE public.tenant_profiles TO sales_app;

GRANT UPDATE(updated_by) ON TABLE public.tenant_profiles TO sales_app;

GRANT UPDATE(updated_at) ON TABLE public.tenant_profiles TO sales_app;

GRANT SELECT ON TABLE public.tenants TO sales_app;
GRANT SELECT ON TABLE public.tenants TO sales_worker;

GRANT UPDATE(name) ON TABLE public.tenants TO sales_app;

GRANT UPDATE(status) ON TABLE public.tenants TO sales_app;

GRANT UPDATE(country) ON TABLE public.tenants TO sales_app;

GRANT UPDATE(time_zone) ON TABLE public.tenants TO sales_app;

GRANT UPDATE(went_live_at) ON TABLE public.tenants TO sales_app;

GRANT UPDATE(updated_at) ON TABLE public.tenants TO sales_app;

GRANT UPDATE(acknowledge_enquiries) ON TABLE public.tenants TO sales_app;

GRANT UPDATE(order_creation) ON TABLE public.tenants TO sales_app;

GRANT SELECT,INSERT ON TABLE public.user_credentials TO sales_app;

GRANT UPDATE(password_hash) ON TABLE public.user_credentials TO sales_app;

GRANT UPDATE(password_changed_at) ON TABLE public.user_credentials TO sales_app;

GRANT SELECT,INSERT ON TABLE public.user_sessions TO sales_app;

GRANT UPDATE(revoked_at) ON TABLE public.user_sessions TO sales_app;

GRANT SELECT ON TABLE public.users TO sales_app;

GRANT INSERT(id) ON TABLE public.users TO sales_app;

GRANT INSERT(subject) ON TABLE public.users TO sales_app;

GRANT INSERT(email) ON TABLE public.users TO sales_app;

GRANT INSERT(email_normalized) ON TABLE public.users TO sales_app;

GRANT INSERT(display_name),UPDATE(display_name) ON TABLE public.users TO sales_app;

GRANT UPDATE(email_verified_at) ON TABLE public.users TO sales_app;

GRANT UPDATE(updated_at) ON TABLE public.users TO sales_app;

GRANT SELECT ON TABLE public.workflow_runs TO sales_app;
GRANT SELECT,INSERT,UPDATE ON TABLE public.workflow_runs TO sales_worker;

-- LangGraph's checkpoint schema versions (created by the former 0004).
INSERT INTO public.checkpoint_migrations VALUES (0);
INSERT INTO public.checkpoint_migrations VALUES (1);
INSERT INTO public.checkpoint_migrations VALUES (2);
INSERT INTO public.checkpoint_migrations VALUES (3);
INSERT INTO public.checkpoint_migrations VALUES (4);
INSERT INTO public.checkpoint_migrations VALUES (5);
INSERT INTO public.checkpoint_migrations VALUES (6);
INSERT INTO public.checkpoint_migrations VALUES (7);
INSERT INTO public.checkpoint_migrations VALUES (8);
INSERT INTO public.checkpoint_migrations VALUES (9);
