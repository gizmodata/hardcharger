"""What the fixture test_rbac_name_scope.py opens was made of, by a Hardcharger 2.13.0 under
access control (fixtures/make_name_scope_fixture.py runs these lists with that binary): tables,
views, triggers and policies whose expressions call macros by a bare name, with a schema, and as a
method (value.f()), and the macros and the type a user had made in schemas of his own by then,
named like the ones those expressions call. 2.13.0 accepted every one of them."""

LEGACY = "name_scope_213"  # the fixture database's file name, so its catalog's name

LEGACY_SCHEMAS = ["CREATE SCHEMA app", "CREATE SCHEMA ws", "CREATE SCHEMA rpt", "CREATE SCHEMA fin", "CREATE SCHEMA util"]
LEGACY_PRINCIPALS = [
    "CREATE ROLE writers", "CREATE ROLE makers", "CREATE USER wes", "CREATE USER mia",
    "GRANT writers TO wes", "GRANT makers TO mia",
    "GRANT CREATE MACRO, CREATE TYPE ON SCHEMA ws TO writers", "GRANT CREATE MACRO ON SCHEMA rpt TO writers",
    "GRANT CREATE TABLE, CREATE TRIGGER ON SCHEMA app TO makers",
]
LEGACY_OBJECTS = [
    # The administrator's macros and type.
    "CREATE MACRO main.is_valid(x) AS x > 0", "CREATE MACRO main.fmt(x) AS 'genuine ' || x",
    "CREATE MACRO main.has_text(x) AS length(x) > 0", "CREATE MACRO main.mask(x) AS '***' || right(x, 2)",
    "CREATE MACRO app.amask(x) AS '###' || right(x, 2)", "CREATE MACRO util.stamp() AS 'genuine stamp'",
    "CREATE MACRO main.mask_ssn(x) AS '***-**-' || right(x, 4)", "CREATE MACRO main.in_region(x) AS x = 'EMEA'",
    "CREATE MACRO main.in_region2(x) AS x <> 'LATAM'", "CREATE MACRO main.dflt() AS 'genuine default'",
    "CREATE MACRO main.inner_ok(x) AS x > 0", "CREATE MACRO main.outer_ok(x) AS inner_ok(x)",
    "CREATE MACRO main.pair_ok(a, b) AS a < b",
    "CREATE TYPE main.mood AS ENUM ('ok', 'sad')",
    "CREATE TABLE main.other_secret (note VARCHAR)", "INSERT INTO main.other_secret VALUES ('other-secret-A'), ('other-secret-B')",
    "CREATE TABLE main.audit_log (what VARCHAR, n BIGINT)",
    # CHECK constraints: a bare name, a method call, a name with its schema, a macro that calls a macro.
    "CREATE TABLE main.checked (x INT CHECK (is_valid(x)))", "CREATE TABLE main.mchecked (x INT CHECK (x.is_valid()))",
    "CREATE TABLE main.qchecked (x INT CHECK (main.is_valid(x)))", "CREATE TABLE main.nested (x INT CHECK (outer_ok(x)))",
    "CREATE TABLE main.pay (id INT)",
    # Rows an administrator's trigger copies into a table with a CHECK (the writer has DELETE, no SELECT).
    "CREATE TABLE fin.src_t (id INT, secret VARCHAR)",
    "INSERT INTO fin.src_t VALUES (1, 'src-secret-1'), (2, 'src-secret-2'), (3, 'src-secret-3')",
    "CREATE TABLE fin.arch (secret VARCHAR CHECK (main.has_text(secret)))",
    # A CHECK on a column the writer may not read, re-checked on the rows an UPDATE carries.
    "CREATE TABLE main.trg_t (id INT, balance INT, secret VARCHAR CHECK (has_text(secret)))",
    "INSERT INTO main.trg_t VALUES (1, 0, 'chk-secret-1'), (2, 0, 'chk-secret-2')",
    # Generated columns, read directly, through views (one in the user's reach), in a trigger's body.
    "CREATE TABLE main.people (name VARCHAR, ssn VARCHAR, shown VARCHAR AS (mask(ssn)))",
    "INSERT INTO main.people VALUES ('ann', '111-11-1111'), ('bob', '222-22-2222')",
    "USE app",
    "CREATE TABLE cards (holder VARCHAR, pan VARCHAR, shown VARCHAR AS (amask(pan)))",
    "INSERT INTO cards VALUES ('ann', '4111-0000-0000-1111')",
    "USE main",
    "CREATE TABLE main.req (id INT)", "CREATE TABLE main.outbox (line VARCHAR)",
    "CREATE TABLE main.moods (m VARCHAR, mm VARCHAR AS (CAST(CAST(m AS mood) AS VARCHAR)))",
    "INSERT INTO main.moods (m) VALUES ('ok')",
    "CREATE TABLE main.dflt_t (id INT, note VARCHAR DEFAULT dflt())",
    "CREATE TABLE main.dflt_pk (id INT PRIMARY KEY, note VARCHAR DEFAULT dflt())",
    # A CHECK over two columns: an UPDATE of one must carry the other for it.
    "CREATE TABLE main.pairs (i INT, j INT, CHECK (pair_ok(i, j)))", "INSERT INTO main.pairs VALUES (1, 5)",
    # A generated column DuckDB computes again when rows are appended (by a trigger, here).
    "CREATE TABLE main.gen_src (secret VARCHAR)", "CREATE TABLE main.gen_sink (secret VARCHAR, label VARCHAR AS (fmt(secret)))",
    # Policies' tables.
    "CREATE TABLE main.emp (name VARCHAR, region VARCHAR, ssn VARCHAR)",
    "INSERT INTO main.emp VALUES ('ann', 'EMEA', '111-11-1111'), ('bob', 'APAC', '222-22-2222'), ('cy', 'LATAM', '333-33-3333')",
    "CREATE TABLE main.emp2 AS SELECT * FROM main.emp", "CREATE TABLE main.emp3 AS SELECT * FROM main.emp",
]
# What wes had made by then, before anything a 2.13.0 name pin looked at named them: macros in
# his own schema (and in rpt, where he may create them) named like the administrator's, with a
# second form of two parameters (what `main.f(x)` becomes once `main` is no schema the session
# finds: f(main, x)).
LEGACY_WES = [
    "CREATE MACRO ws.is_valid(x) AS true, (a, b) AS true",
    "CREATE MACRO ws.fmt(x) AS error('LEAK: ' || x), (a, b) AS error('LEAK: ' || b)",
    "CREATE MACRO ws.has_text(x) AS error('LEAK: ' || x), (a, b) AS error('LEAK: ' || b)",
    "CREATE MACRO ws.mask(x) AS error('LEAK: ' || x)", "CREATE MACRO ws.amask(x) AS error('LEAK: ' || x)",
    f"CREATE MACRO rpt.mask(x) AS error('LEAK: ' || x || ' / ' || (SELECT string_agg(note, ',') FROM {LEGACY}.main.other_secret))",
    "CREATE MACRO ws.stamp() AS 'FORGED BY WES'", "CREATE MACRO ws.dflt() AS 'FORGED DEFAULT', (m) AS 'FORGED DEFAULT'",
    "CREATE MACRO ws.pair_ok(a, b) AS true, (m, a, b) AS true",
    "CREATE MACRO ws.mask_ssn(x) AS x, (a, b) AS b", "CREATE MACRO ws.in_region(x) AS true, (a, b) AS true",
    "CREATE MACRO ws.in_region2(x) AS true", "CREATE MACRO ws.inner_ok(x) AS true",
    "CREATE TYPE ws.mood AS ENUM ('ok', 'LEAK')",
]
# The administrator's triggers and views ...
LEGACY_TRIGGERS_AND_VIEWS = [
    "CREATE TRIGGER pay_chk AFTER INSERT ON main.pay REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
    "INSERT INTO main.checked SELECT id FROM n",
    "CREATE TRIGGER src_d AFTER DELETE ON fin.src_t REFERENCING OLD TABLE AS o FOR EACH STATEMENT "
    "INSERT INTO fin.arch SELECT secret FROM o",
    "CREATE TRIGGER trg_u AFTER UPDATE ON main.trg_t REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
    "INSERT INTO main.audit_log SELECT 'upd', count(*) FROM n",
    "CREATE VIEW rpt.people AS SELECT name, shown FROM main.people",
    "CREATE VIEW main.people_v AS SELECT name, shown FROM main.people",
    # (the table's macro is in app: 2.13.0 found it for the view through the creator's search path)
    "SET search_path = 'main,app'", "CREATE VIEW main.cards_v AS SELECT holder, shown FROM app.cards",
    "SET search_path = 'main'",
    "CREATE TRIGGER req_ins AFTER INSERT ON main.req FOR EACH STATEMENT "
    "INSERT INTO main.outbox SELECT name || ' ' || shown FROM rpt.people",
    "CREATE TRIGGER gen_copy AFTER INSERT ON main.gen_src REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
    "INSERT INTO main.gen_sink SELECT secret FROM n",
    # A view's own query: a macro of the view's schema, and one DuckDB found in main for a view elsewhere.
    "CREATE VIEW main.fmt_v AS SELECT fmt(note) AS f FROM main.other_secret",
    "CREATE VIEW rpt.fmt_v AS SELECT fmt(name) AS f FROM main.people",
]
# ... the policies (kept in the access-control store) ...
LEGACY_POLICIES = [
    "CREATE MASKING POLICY ssn_mask AS (val VARCHAR) RETURNS VARCHAR -> mask_ssn(val)",
    "ALTER TABLE main.emp MODIFY COLUMN ssn SET MASKING POLICY ssn_mask",
    "CREATE ROW POLICY emea_only ON main.emp TO writers USING (in_region(region))",
    "CREATE MASKING POLICY ssn_mask2 AS (val VARCHAR) RETURNS VARCHAR -> main.mask_ssn(val)",
    "ALTER TABLE main.emp2 MODIFY COLUMN ssn SET MASKING POLICY ssn_mask2",
    "CREATE ROW POLICY emea_only ON main.emp2 TO writers USING (main.in_region(region))",
]
LEGACY_METHOD_POLICY = ["CREATE ROW POLICY not_latam ON main.emp3 TO writers USING (region.in_region2())"]
# ... and who may do what.
LEGACY_ACCESS = [
    "GRANT INSERT ON main.checked TO writers", "GRANT INSERT ON main.mchecked TO writers",
    "GRANT INSERT ON main.qchecked TO writers", "GRANT INSERT ON main.nested TO writers",
    "GRANT INSERT ON main.pay TO writers", "GRANT DELETE ON fin.src_t TO writers", "GRANT INSERT ON fin.arch TO writers",
    "GRANT UPDATE ON main.trg_t TO writers", "GRANT INSERT ON main.audit_log TO writers",
    "GRANT SELECT ON rpt.people TO writers", "GRANT SELECT ON main.people_v TO writers",
    "GRANT SELECT ON main.cards_v TO writers", "GRANT INSERT ON main.req TO writers",
    "GRANT INSERT ON main.outbox TO writers", "GRANT SELECT ON main.moods TO writers",
    "GRANT INSERT ON main.dflt_t TO writers", "GRANT INSERT ON main.gen_src TO writers",
    "GRANT SELECT, INSERT ON main.dflt_pk TO writers", "GRANT SELECT, UPDATE ON main.pairs TO writers",
    "GRANT INSERT ON main.gen_sink TO writers", "GRANT SELECT ON main.fmt_v TO writers",
    "GRANT SELECT ON rpt.fmt_v TO writers", "GRANT SELECT ON main.emp TO writers",
    "GRANT SELECT ON main.emp2 TO writers", "GRANT SELECT ON main.emp3 TO writers",
]
# mia's own tables and triggers (hers: the triggers run with her rights). A generated column
# calling the administrator's macro bare (her session found it in main) and with its schema; a
# default whose macro her search path found in a third schema; an action with a method call.
LEGACY_MIA = [
    "CREATE TABLE app.mine (id INT)", "CREATE TABLE app.mvault (secret VARCHAR, label VARCHAR AS (fmt(secret)))",
    "INSERT INTO app.mvault VALUES ('vault-secret-1'), ('vault-secret-2')", "CREATE TABLE app.mlabels (label VARCHAR)",
    "CREATE TRIGGER mine_lab AFTER INSERT ON app.mine FOR EACH STATEMENT INSERT INTO app.mlabels SELECT label FROM app.mvault",
    "CREATE TABLE app.mine2 (id INT)", "CREATE TABLE app.mvault2 (secret VARCHAR, label VARCHAR AS (main.fmt(secret)))",
    "INSERT INTO app.mvault2 VALUES ('vault2-secret-1')", "CREATE TABLE app.mlabels2 (label VARCHAR)",
    "CREATE TRIGGER mine2_lab AFTER INSERT ON app.mine2 FOR EACH STATEMENT INSERT INTO app.mlabels2 SELECT label FROM app.mvault2",
    "SET search_path = 'main,util'",
    "CREATE TABLE app.msrc (id INT)", "CREATE TABLE app.mlog (id INT, approved_by VARCHAR DEFAULT stamp())",
    "CREATE TRIGGER msrc_ins AFTER INSERT ON app.msrc REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
    "INSERT INTO app.mlog (id) SELECT id FROM n",
    "SET search_path = 'main'",
    "CREATE TABLE app.mine3 (id INT)", "CREATE TABLE app.mvault3 (secret VARCHAR)",
    "INSERT INTO app.mvault3 VALUES ('vault3-secret-1')", "CREATE TABLE app.mnotes (note VARCHAR)",
    "CREATE TRIGGER mine3_ins AFTER INSERT ON app.mine3 FOR EACH STATEMENT INSERT INTO app.mnotes SELECT secret.fmt() FROM app.mvault3",
]
LEGACY_MIA_ACCESS = ["GRANT INSERT ON app.mine TO writers", "GRANT INSERT ON app.mine2 TO writers",
                     "GRANT INSERT ON app.msrc TO writers", "GRANT INSERT ON app.mine3 TO writers"]
