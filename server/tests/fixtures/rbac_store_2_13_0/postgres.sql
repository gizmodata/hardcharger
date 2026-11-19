CREATE SCHEMA hardcharger_rbac;
CREATE TABLE hardcharger_rbac.catalog_tenants (
    catalog_key character varying NOT NULL,
    catalog_name character varying NOT NULL,
    tenant_id character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.group_mappings (
    mapping_id character varying NOT NULL,
    issuer character varying NOT NULL,
    group_name character varying NOT NULL,
    role_id character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.mask_bindings (
    table_key character varying NOT NULL,
    column_key character varying NOT NULL,
    catalog_name character varying NOT NULL,
    schema_path character varying NOT NULL,
    object_name character varying NOT NULL,
    column_name character varying NOT NULL,
    policy_id character varying NOT NULL,
    exempt_role_ids character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.masking_policies (
    policy_id character varying NOT NULL,
    name character varying NOT NULL,
    name_key character varying NOT NULL,
    parameter character varying NOT NULL,
    parameter_type character varying NOT NULL,
    return_type character varying NOT NULL,
    body character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.memberships (
    member_kind character varying NOT NULL,
    member_id character varying NOT NULL,
    role_id character varying NOT NULL,
    admin_option boolean NOT NULL,
    granted_by character varying
);
CREATE TABLE hardcharger_rbac.meta (
    key character varying NOT NULL,
    value character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.object_grants (
    grant_id character varying NOT NULL,
    role_id character varying NOT NULL,
    scope character varying NOT NULL,
    catalog_name character varying NOT NULL,
    schema_path character varying NOT NULL,
    object_name character varying NOT NULL,
    privilege character varying NOT NULL,
    columns character varying NOT NULL,
    grant_option boolean NOT NULL,
    granted_by character varying
);
CREATE TABLE hardcharger_rbac.owners (
    object_key character varying NOT NULL,
    catalog_name character varying NOT NULL,
    schema_path character varying NOT NULL,
    object_name character varying NOT NULL,
    object_type character varying NOT NULL,
    owner_kind character varying NOT NULL,
    owner_id character varying NOT NULL,
    definition_hash character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.principal_tenants (
    principal_kind character varying NOT NULL,
    principal_id character varying NOT NULL,
    tenant_id character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.roles (
    role_id character varying NOT NULL,
    name character varying NOT NULL,
    name_key character varying NOT NULL,
    builtin boolean NOT NULL
);
CREATE TABLE hardcharger_rbac.row_policies (
    policy_id character varying NOT NULL,
    name character varying NOT NULL,
    table_key character varying NOT NULL,
    catalog_name character varying NOT NULL,
    schema_path character varying NOT NULL,
    object_name character varying NOT NULL,
    permissive boolean NOT NULL,
    role_ids character varying NOT NULL,
    predicate character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.scoped_tokens (
    token_id character varying NOT NULL,
    name character varying NOT NULL,
    name_key character varying NOT NULL,
    user_id character varying NOT NULL,
    parent_id character varying NOT NULL,
    privileges character varying NOT NULL,
    catalogs character varying NOT NULL,
    expires_at character varying NOT NULL,
    created_by character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.storage_privileges (
    role_id character varying NOT NULL,
    privilege character varying NOT NULL,
    location character varying NOT NULL,
    admin_option boolean NOT NULL,
    granted_by character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.system_privileges (
    role_id character varying NOT NULL,
    privilege character varying NOT NULL,
    admin_option boolean NOT NULL,
    granted_by character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.tenants (
    tenant_id character varying NOT NULL,
    name character varying NOT NULL,
    name_key character varying NOT NULL,
    default_catalog character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.user_attributes (
    user_id character varying NOT NULL,
    attribute character varying NOT NULL,
    value character varying NOT NULL
);
CREATE TABLE hardcharger_rbac.users (
    user_id character varying NOT NULL,
    name character varying NOT NULL,
    name_key character varying NOT NULL,
    password_hash character varying,
    enabled boolean NOT NULL
);
INSERT INTO hardcharger_rbac.group_mappings VALUES ('6245b920-e06c-41b9-acf8-1ef61ff1de0f', '', 'ops', 'aded4c29-e056-412d-ac59-d013cd7d2f03');
INSERT INTO hardcharger_rbac.memberships VALUES ('user', 'd98d3f50-efaf-470e-969f-d7811de3416a', 'e9404300-0889-44ef-996e-fc6d7ced3751', false, '');
INSERT INTO hardcharger_rbac.memberships VALUES ('user', 'bbdf1245-d293-4081-8068-2ce3e31fdce3', 'aded4c29-e056-412d-ac59-d013cd7d2f03', true, '');
INSERT INTO hardcharger_rbac.memberships VALUES ('user', '21e66a0b-5b91-4852-88d5-d49a670b78fd', '008bf9a9-e6aa-4856-b0a5-34dbbd924eb9', false, '');
INSERT INTO hardcharger_rbac.memberships VALUES ('user', '6f056549-ad85-4486-81c5-d4a3d83efe7c', 'aded4c29-e056-412d-ac59-d013cd7d2f03', false, 'bbdf1245-d293-4081-8068-2ce3e31fdce3');
INSERT INTO hardcharger_rbac.meta VALUES ('schema_version', '1');
INSERT INTO hardcharger_rbac.meta VALUES ('storage_privileges_split', '1');
INSERT INTO hardcharger_rbac.meta VALUES ('data_version', '17');
INSERT INTO hardcharger_rbac.object_grants VALUES ('c6173145-fc40-47ad-bbe7-f80f5fee3f2c', 'e9404300-0889-44ef-996e-fc6d7ced3751', 'schema', 'memory', '["main"]', '', 'SELECT', '[]', false, '');
INSERT INTO hardcharger_rbac.principal_tenants VALUES ('role', 'ca3cbb49-4a3d-4b58-9e99-20da3f06170c', '18a9b5f2-7ea0-42a0-9886-93c648cb4e57');
INSERT INTO hardcharger_rbac.principal_tenants VALUES ('role', '3c8e2e49-b1f4-4daf-83b7-28e75d47ddbb', '18a9b5f2-7ea0-42a0-9886-93c648cb4e57');
INSERT INTO hardcharger_rbac.principal_tenants VALUES ('user', 'bcc85091-089e-4845-96b2-6df8a05953ac', '18a9b5f2-7ea0-42a0-9886-93c648cb4e57');
INSERT INTO hardcharger_rbac.roles VALUES ('public', 'PUBLIC', 'public', true);
INSERT INTO hardcharger_rbac.roles VALUES ('e9404300-0889-44ef-996e-fc6d7ced3751', 'analysts', 'analysts', false);
INSERT INTO hardcharger_rbac.roles VALUES ('aded4c29-e056-412d-ac59-d013cd7d2f03', 'sysadmin', 'sysadmin', false);
INSERT INTO hardcharger_rbac.roles VALUES ('008bf9a9-e6aa-4856-b0a5-34dbbd924eb9', 'SYSTEM ADMIN', 'system admin', false);
INSERT INTO hardcharger_rbac.roles VALUES ('ca3cbb49-4a3d-4b58-9e99-20da3f06170c', 'acme/PUBLIC', 'acme/public', true);
INSERT INTO hardcharger_rbac.roles VALUES ('3c8e2e49-b1f4-4daf-83b7-28e75d47ddbb', 'acme/tenant_admin', 'acme/tenant_admin', true);
INSERT INTO hardcharger_rbac.system_privileges VALUES ('aded4c29-e056-412d-ac59-d013cd7d2f03', 'READ FILESYSTEM', false, '');
INSERT INTO hardcharger_rbac.tenants VALUES ('18a9b5f2-7ea0-42a0-9886-93c648cb4e57', 'acme', 'acme', '');
INSERT INTO hardcharger_rbac.users VALUES ('d98d3f50-efaf-470e-969f-d7811de3416a', 'ann', 'ann', 'pbkdf2-sha256$600000$aTE5I7LwgSMfmsr+JjjN4Q==$njoyKSEqJtnHsaAfsX9RebMQvBGW/HGv/IpmZfrdHE0=', true);
INSERT INTO hardcharger_rbac.users VALUES ('bbdf1245-d293-4081-8068-2ce3e31fdce3', 'bob', 'bob', NULL, true);
INSERT INTO hardcharger_rbac.users VALUES ('21e66a0b-5b91-4852-88d5-d49a670b78fd', 'cleo', 'cleo', NULL, true);
INSERT INTO hardcharger_rbac.users VALUES ('6f056549-ad85-4486-81c5-d4a3d83efe7c', 'dora', 'dora', NULL, true);
INSERT INTO hardcharger_rbac.users VALUES ('bcc85091-089e-4845-96b2-6df8a05953ac', 'acme/amy', 'acme/amy', NULL, true);
ALTER TABLE ONLY hardcharger_rbac.catalog_tenants
    ADD CONSTRAINT catalog_tenants_pkey PRIMARY KEY (catalog_key);
ALTER TABLE ONLY hardcharger_rbac.group_mappings
    ADD CONSTRAINT group_mappings_issuer_group_name_role_id_key UNIQUE (issuer, group_name, role_id);
ALTER TABLE ONLY hardcharger_rbac.group_mappings
    ADD CONSTRAINT group_mappings_pkey PRIMARY KEY (mapping_id);
ALTER TABLE ONLY hardcharger_rbac.mask_bindings
    ADD CONSTRAINT mask_bindings_pkey PRIMARY KEY (table_key, column_key);
ALTER TABLE ONLY hardcharger_rbac.masking_policies
    ADD CONSTRAINT masking_policies_name_key_key UNIQUE (name_key);
ALTER TABLE ONLY hardcharger_rbac.masking_policies
    ADD CONSTRAINT masking_policies_pkey PRIMARY KEY (policy_id);
ALTER TABLE ONLY hardcharger_rbac.memberships
    ADD CONSTRAINT memberships_pkey PRIMARY KEY (member_kind, member_id, role_id);
ALTER TABLE ONLY hardcharger_rbac.meta
    ADD CONSTRAINT meta_pkey PRIMARY KEY (key);
ALTER TABLE ONLY hardcharger_rbac.object_grants
    ADD CONSTRAINT object_grants_pkey PRIMARY KEY (grant_id);
ALTER TABLE ONLY hardcharger_rbac.owners
    ADD CONSTRAINT owners_pkey PRIMARY KEY (object_key);
ALTER TABLE ONLY hardcharger_rbac.principal_tenants
    ADD CONSTRAINT principal_tenants_pkey PRIMARY KEY (principal_kind, principal_id);
ALTER TABLE ONLY hardcharger_rbac.roles
    ADD CONSTRAINT roles_name_key_key UNIQUE (name_key);
ALTER TABLE ONLY hardcharger_rbac.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (role_id);
ALTER TABLE ONLY hardcharger_rbac.row_policies
    ADD CONSTRAINT row_policies_pkey PRIMARY KEY (policy_id);
ALTER TABLE ONLY hardcharger_rbac.scoped_tokens
    ADD CONSTRAINT scoped_tokens_pkey PRIMARY KEY (token_id);
ALTER TABLE ONLY hardcharger_rbac.scoped_tokens
    ADD CONSTRAINT scoped_tokens_user_id_name_key_key UNIQUE (user_id, name_key);
ALTER TABLE ONLY hardcharger_rbac.storage_privileges
    ADD CONSTRAINT storage_privileges_pkey PRIMARY KEY (role_id, privilege, location, granted_by);
ALTER TABLE ONLY hardcharger_rbac.system_privileges
    ADD CONSTRAINT system_privileges_pkey PRIMARY KEY (role_id, privilege, granted_by);
ALTER TABLE ONLY hardcharger_rbac.tenants
    ADD CONSTRAINT tenants_name_key_key UNIQUE (name_key);
ALTER TABLE ONLY hardcharger_rbac.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (tenant_id);
ALTER TABLE ONLY hardcharger_rbac.user_attributes
    ADD CONSTRAINT user_attributes_pkey PRIMARY KEY (user_id, attribute);
ALTER TABLE ONLY hardcharger_rbac.users
    ADD CONSTRAINT users_name_key_key UNIQUE (name_key);
ALTER TABLE ONLY hardcharger_rbac.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (user_id);
