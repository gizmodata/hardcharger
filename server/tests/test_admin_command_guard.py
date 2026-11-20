"""Port of GizmoSQL 1.x tests/integration/test_admin_command_guard.cpp (42 cases).

36 cases unit-test the classifier: they are C++ unit tests against a real DuckDB
connection (server/unit/guards_test.cc, the 1.x bodies verbatim), run here. 1.x
classified from DuckDB's parse tree; Hardcharger classifies from DuckDB's tokens and
statement types through the v2 C API, failing closed where a path is not a literal
remote URL. The 6 server cases run below, with non-admin principals from tokens in the
server's own format (1.x: role "analyst"). All 42 apply.
"""

import pytest

from conftest import run_unit_tests

UNIT_CASES = [
    "AdminCommandGuard.AttachIsGated", "AdminCommandGuard.DetachIsGated",
    "AdminCommandGuard.ExplainWrappedStatementsAreGated", "AdminCommandGuard.AttachDetachEvasionStillCaught",
    "AdminCommandGuard.ExplainParenthesizedQueriesKeepTheirWholeBody",
    "UnredactedSecretsSet.TrueIsMatched", "UnredactedSecretsSet.TrueEquivalentsAreMatched",
    "UnredactedSecretsSet.ExplainWrappedSetIsMatched", "UnredactedSecretsSet.FalseAndOtherSqlAreNotMatched",
    "AdminCommandGuard.SetGlobalIsGated", "AdminCommandGuard.BareSetOfDangerousGlobalSettingIsGated",
    "AdminCommandGuard.HarmlessSessionSetIsAllowed", "AdminCommandGuard.InstallLoadIsGated",
    "AdminCommandGuard.CheckpointIsGated", "AdminCommandGuard.HarmlessPragmaIsAllowed",
    "AdminCommandGuard.CopyLocalIsGated", "AdminCommandGuard.CopyRemoteIsAllowed",
    "AdminCommandGuard.CopyRemoteButNestedLocalReadIsGated", "AdminCommandGuard.ExportImportDatabaseAlwaysGated",
    "AdminCommandGuard.ReadFunctionsLocalAreGated", "AdminCommandGuard.ReadFunctionsRemoteAreAllowed",
    "AdminCommandGuard.ReadFunctionsNonLiteralPathIsGated", "AdminCommandGuard.ReadFunctionsNestedAreGated",
    "AdminCommandGuard.PrepareIndirectionIsGated", "AdminCommandGuard.CreateDropSecretIsGated",
    "AdminCommandGuard.DuckDBSecretsIsAlwaysGated", "AdminCommandGuard.LocalReplacementScanIsGated",
    "AdminCommandGuard.RemoteReplacementScanIsAllowed", "AdminCommandGuard.QuackFunctionsAreGated",
    "AdminCommandGuard.CopyInsideCteIsGated", "AdminCommandGuard.ConnectAndExternalResourceAreGated",
    "AdminCommandGuard.NewSingleFileReadersAreGated", "AdminCommandGuard.DmlSourcesAreWalked",
    "AdminCommandGuard.OrdinaryQueriesAreAllowed", "AdminCommandGuard.UnparseableSqlIsAllowedThroughToDuckDB",
    "AdminCommandGuard.CheckWrapperReturnsErrorForGated", "AdminCommandGuard.CheckWrapperOkForAllowed",
    # GizmoSQL 1.42.3 (6a458ca7): a macro's own definition is not a call of the gated function it is named like.
    "AdminCommandGuard.MacroDefinitionIsNotACall",
]


@pytest.fixture(scope="module")
def unit_results():
    return run_unit_tests(gtest_filter="AdminCommandGuard.*:UnredactedSecretsSet.*")


def test_every_unit_case_ran(unit_results):
    assert sorted(unit_results) == sorted(UNIT_CASES)


@pytest.mark.parametrize("case", UNIT_CASES)
def test_unit_case(unit_results, case):
    assert unit_results[case] is True


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(env={"HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS": "true"})


@pytest.fixture(scope="module")
def analyst(server):
    return server.as_role(role="analyst", subject="analyst_user")


@pytest.mark.parametrize("sql,category", [
    ("ATTACH 'side.db' AS side", "ATTACH"),
    ("DETACH some_catalog", "DETACH"),
    ("SET GLOBAL memory_limit = '512MB'", "SET GLOBAL memory_limit"),
    ("INSTALL httpfs", "INSTALL extension"),
    ("SELECT * FROM read_csv('/etc/passwd')", "read_csv() (local filesystem)"),
    ("SELECT * FROM duckdb_secrets()", "duckdb_secrets()"),
    ("CHECKPOINT", "CHECKPOINT"),
])
def test_non_admin_token_rejected_for_gated_commands(analyst, sql, category):
    problem = analyst.error(sql=sql)
    assert problem["code"] == "permission_denied" and problem["status"] == 403, problem
    assert "'admin' role" in problem["detail"] and category in problem["detail"], problem


def test_non_admin_token_allowed_for_ordinary_queries(analyst):
    assert analyst.scalar(sql="SELECT 1 AS x") == 1
    assert analyst.query(sql="SELECT * FROM range(10)").num_rows == 10
    analyst.execute(sql="SET search_path = 'main'")


def test_admin_basic_auth_unaffected(server):
    admin = server.admin()
    admin.execute(sql="SET GLOBAL memory_limit = '512MB'")
    admin.execute(sql="CHECKPOINT")


def test_admin_set_unredacted_secrets_true_is_rejected(server):
    admin = server.admin()
    problem = admin.error(sql="SET allow_unredacted_secrets = true")
    assert problem["code"] == "permission_denied" and "disabled on this server" in problem["detail"], problem
    admin.execute(sql="SET allow_unredacted_secrets = false")


def test_sql_with_nul_byte_is_rejected(server):
    problem = server.admin().error(sql="SELECT 1\x00; SET allow_unredacted_secrets = true")
    assert "NUL byte" in problem["detail"], problem


def test_set_true_reaches_duckdb_when_the_block_is_off(launcher):
    admin = launcher.start(env={"HARDCHARGER_BLOCK_UNREDACTED_SECRETS": "false"}).admin()
    problem = admin.error(sql="SET allow_unredacted_secrets = true")
    assert "disabled on this server" not in problem["detail"], problem
    assert "allow_unredacted_secrets" in problem["detail"], problem  # DuckDB's own refusal


# ---- GizmoSQL 1.42.3 (v1.42.0..b820719d): cross-catalog reach and indirect routes ----
#
# 1.42.3 added 3 server cases and 20 classifier cases, and rewrote 5 (network URLs gated, object
# storage left to the file system). Hardcharger runs them as statements against a server, for both
# kinds of non-admin token 1.x has: one without rules (1.x's MintToken("app_a", "user")) and one
# whose catalog_access rules give it its own catalog. Each 1.x "Gated" statement must be refused,
# saying why; each "not gated" one must run. Where Hardcharger is stricter by design, the case
# says so and asserts the refusal: a non-admin token holds no storage privilege (object storage
# is opened by GRANT READ/WRITE OBJECT STORAGE to store users, or for a catalog_access token by its
# DuckLake catalogs' data paths), query() / query_table() stay the administrator's, and only an
# administrator creates macros and types. 1.x's local files (/etc/passwd, /etc/hosts) are files
# of the test's own, so a refusal can't come from the sensitive-path guard instead.

REACH_SETUP = [
    "CREATE TABLE my_table (i INTEGER)", "CREATE TABLE t (x INTEGER, path VARCHAR, note VARCHAR)",
    "CREATE TABLE orders (id INTEGER, amount INTEGER, region VARCHAR, file VARCHAR)",
    "INSERT INTO orders VALUES (1, 10, 'east', 'a'), (2, 5, 'west', 'b')",
    "CREATE TABLE returns (id INTEGER)", "CREATE TABLE staged (x INTEGER, path VARCHAR, note VARCHAR)",
    "CREATE TABLE u (path VARCHAR)", "CREATE TABLE v (y INTEGER)",
    "CREATE TABLE audit_log (query VARCHAR, \"glob\" VARCHAR)", "CREATE TABLE route_t AS SELECT 'a' AS x",
    # An administrator's macros that hand their argument on (1.x's route_sql / route_file).
    "CREATE OR REPLACE MACRO route_sql(s) AS TABLE SELECT * FROM query(s)",
    "CREATE OR REPLACE MACRO route_file(p) AS TABLE SELECT * FROM read_text(p)",
    "CREATE OR REPLACE MACRO fine_t(n) AS TABLE SELECT * FROM range(n)",
]


@pytest.fixture(scope="module")
def reach(module_launcher, tmp_path_factory):
    files = tmp_path_factory.mktemp("reach_files")
    (files / "passwd.csv").write_text("a\nfile-secret-word\n")
    (files / "hosts").write_text("host-secret-word\n")
    server = module_launcher.start(args=["--database", "reach.db"])
    admin = server.admin()
    for sql in REACH_SETUP:
        admin.execute(sql=sql)
    return {"server": server, "F": str(files / "passwd.csv"), "H": str(files / "hosts"), "D": str(files)}


@pytest.fixture(params=["token without rules", "catalog_access token"])
def non_admin(request, reach):
    server = reach["server"]
    if request.param == "token without rules":
        return server.as_role(role="user", subject="app_a")
    return server.as_role(role="user", subject="app_a_scoped",
                          catalog_access=[{"catalog": "reach", "access": "write"}, {"catalog": "*", "access": "none"}])


def sql_of(reach, template: str) -> str:
    return template.format(F=reach["F"], H=reach["H"], D=reach["D"])


def refused(session, sql: str, why: str) -> None:
    problem = session.error(sql=sql)
    assert problem["code"] == "permission_denied" and problem["status"] == 403, (sql, problem)
    assert why in problem["detail"], f"{sql}: expected {why!r} in {problem['detail']}"


LOCAL = "(local filesystem)"
TF = "Hardcharger blocked the table function "
QUERY = "Hardcharger blocked query()"

# (1.x case, statement, why it is refused). {F} is a CSV file, {H} a text file, {D} their directory.
GATED = [
    # NonAdminTokenRejectedForCrossCatalogReach (server case)
    ("CrossCatalogReach", "SELECT * FROM postgres_query('__ducklake_metadata_b', 'select table_id, table_name, path from ducklake_table')", TF + "postgres_query"),
    ("CrossCatalogReach", "SELECT * FROM postgres_query('__ducklake_metadata_b', 'select datname from pg_database')", TF + "postgres_query"),
    ("CrossCatalogReach", "CALL postgres_execute('__ducklake_metadata_b', 'select 1')", TF + "postgres_execute"),
    ("CrossCatalogReach", "SELECT 1; CALL postgres_execute('__ducklake_metadata_b', 'select 1')", TF + "postgres_execute"),
    ("CrossCatalogReach", "CREATE VIEW v_pg AS SELECT * FROM postgres_query('b', 'select 1')", TF + "postgres_query"),
    ("CrossCatalogReach", "SELECT * FROM query('SELECT * FROM postgres_query(''b'', ''select 1'')')", QUERY),
    ("CrossCatalogReach", "SELECT * FROM query('SELECT * FROM read_csv(''{F}'')')", QUERY),
    ("CrossCatalogReach", "COPY (SELECT 1 AS i) TO 'https://example.invalid/x.parquet'", "WRITE HTTP"),
    ("CrossCatalogReach", "SELECT * FROM read_csv('https://example.invalid/')", "READ HTTP"),
    ("CrossCatalogReach", "SELECT * FROM 'https://example.invalid/x.csv'", "READ HTTP"),
    # (CALL ducklake_add_data_files(...): the DuckLake track's, test_catalog_access.py.)
    # NonAdminTokenRejectedForIndirectRoutes (server case; the macros are the administrator's)
    ("IndirectRoutes", "SELECT * FROM route_sql('SELECT count(*) FROM duckdb_secrets()')", "duckdb_secrets()"),
    ("IndirectRoutes", "SELECT * FROM route_sql('SELECT * FROM postgres_query(''b'', ''select 1'')')", TF + "postgres_query"),
    ("IndirectRoutes", "SELECT * FROM route_sql('SELECT * FROM read_text(''{H}'')')", "read_text()"),
    ("IndirectRoutes", "SELECT * FROM route_file('{H}')", "read_text()"),
    ("IndirectRoutes", "CREATE OR REPLACE TEMP MACRO mine(s) AS TABLE SELECT * FROM query(s)", QUERY),
    ("IndirectRoutes", "CREATE OR REPLACE TEMP MACRO mine(p) AS TABLE SELECT * FROM read_text(p)", "read_text() " + LOCAL),
    ("IndirectRoutes", "CREATE OR REPLACE TEMP MACRO mine() AS TABLE SELECT * FROM query('SELECT count(*) FROM duckdb_' || 'secrets()')", QUERY),
    ("IndirectRoutes", "CREATE OR REPLACE TEMP MACRO mine(s) AS (SELECT count(*) FROM query(s))", QUERY),
    ("IndirectRoutes", "CALL duckdb_secrets()", "duckdb_secrets()"),
    ("IndirectRoutes", "CALL read_text('{H}')", "read_text() " + LOCAL),
    ("IndirectRoutes", "CALL glob('{D}/*')", "glob() " + LOCAL),
    ("IndirectRoutes", "CALL query('SELECT count(*) FROM duckdb_secrets()')", QUERY),
    ("IndirectRoutes", "CALL postgres_query('b', 'select 1')", TF + "postgres_query"),
    ("IndirectRoutes", "SELECT * FROM query_table('{H}.csv')", "Hardcharger blocked query_table()"),
    ("IndirectRoutes", "SUMMARIZE SELECT * FROM read_text('{H}')", "read_text() " + LOCAL),
    ("IndirectRoutes", "DESCRIBE SELECT * FROM postgres_query('b', 'select 1')", TF + "postgres_query"),
    ("IndirectRoutes", "SET VARIABLE leaked = (SELECT content FROM read_text('{H}'))", "read_text() " + LOCAL),
    ("IndirectRoutes", "UPDATE route_t SET x = (SELECT content FROM read_text('{H}'))", "read_text() " + LOCAL),
    # CopyToObjectStorageIsLeftToTheFileSystem / CopyFromObjectStorageIsAllowedButNetworkUrlIsNot
    ("CopyToObjectStorage", "COPY my_table TO 'https://example.invalid/x'", "WRITE HTTP"),
    ("CopyToObjectStorage", "COPY my_table TO '{D}/x.csv'", "COPY TO " + LOCAL),
    ("CopyFromObjectStorage", "COPY my_table FROM 'https://example.invalid/data.csv'", "READ HTTP"),
    # NetworkUrlInAnyTableFunctionIsGated
    ("NetworkUrlInAnyTableFunction", "SELECT * FROM delta_scan('https://example.invalid/t')", TF + "delta_scan"),
    ("NetworkUrlInAnyTableFunction", "SELECT * FROM iceberg_scan('http://169.254.169.254/x')", TF + "iceberg_scan"),
    ("NetworkUrlInAnyTableFunction", "SELECT * FROM read_xlsx('hf://datasets/x/a.xlsx')", "READ HTTP"),
    ("NetworkUrlInAnyTableFunction", "SELECT * FROM read_parquet(['https://a.invalid/x.parquet', 's3://b/y'])", "read_parquet()"),
    ("NetworkUrlInAnyTableFunction", "SELECT * FROM read_parquet('https://a.invalid/x.parquet', hive_partitioning = true)", "READ HTTP"),
    ("NetworkUrlInAnyTableFunction", "SELECT * FROM (SELECT * FROM delta_scan('https://a.invalid/t')) s", TF + "delta_scan"),
    # RemoteReplacementScanIsGatedOnlyForNetworkUrls, CopyInsideCteIsGated (edited)
    ("RemoteReplacementScan", "SELECT * FROM 'https://example.invalid/data.csv'", "READ HTTP"),
    ("CopyInsideCte", "WITH c AS (COPY (SELECT 42) TO 'https://example.invalid/out.csv') SELECT 1", "WRITE HTTP"),
    # ExternalDatabaseFunctionsAreGated
    ("ExternalDatabaseFunctions", "SELECT * FROM postgres_scan('dbname=x', 'public', 't')", TF + "postgres_scan"),
    ("ExternalDatabaseFunctions", "CALL postgres_clear_cache()", TF + "postgres_clear_cache"),
    ("ExternalDatabaseFunctions", "SELECT * FROM mysql_query('db', 'select 1')", TF + "mysql_query"),
    ("ExternalDatabaseFunctions", "SELECT * FROM sqlite_scan('x.db', 't')", TF + "sqlite_scan"),
    ("ExternalDatabaseFunctions", "CALL sqlite_attach('x.db')", TF + "sqlite_attach"),
    ("ExternalDatabaseFunctions", "SELECT * FROM (SELECT * FROM POSTGRES_QUERY('b', 'select 1')) t", TF + "postgres_query"),
    ("ExternalDatabaseFunctions", "CREATE TABLE t_pg AS SELECT * FROM postgres_query('b', 'select 1')", TF + "postgres_query"),
    ("ExternalDatabaseFunctions", "CREATE MACRO m_pg() AS TABLE SELECT * FROM postgres_query('b', 'select 1')", TF + "postgres_query"),
    # NetworkUrlReadsAreGated
    ("NetworkUrlReads", "SELECT * FROM read_parquet('http://169.254.169.254/x')", "READ HTTP"),
    ("NetworkUrlReads", "SELECT * FROM read_json('hf://datasets/x/y.json')", "READ HTTP"),
    # QueryFunctionArgumentIsChecked / QueryFunctionNestingAndNonLiteralAreGated / SerializedSqlExecutionIsGated
    ("QueryFunctionArgument", "SELECT * FROM query('SELECT * FROM read_csv(''https://x.invalid/y.csv'')')", QUERY),
    ("QueryFunctionArgument", "SELECT * FROM query('SELECT * FROM duckdb_secrets()')", QUERY),
    ("QueryFunctionArgument", "SELECT * FROM query('COPY (SELECT 1) TO ''{D}/x.csv''')", QUERY),
    ("QueryFunctionArgument", "SELECT * FROM (SELECT * FROM query('SELECT * FROM glob(''{D}/*'')')) s", QUERY),
    ("QueryFunctionNesting", "SELECT * FROM query(some_column)", QUERY),
    ("QueryFunctionNesting", "SELECT * FROM query(concat('SELECT ', '1'))", QUERY),
    ("QueryFunctionNesting", "SELECT * FROM query('SELECT * FROM query(''SELECT * FROM read_csv(''''{F}'''')'')')", QUERY),
    ("SerializedSqlExecution", "SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT 1'))", TF + "json_execute_serialized_sql"),
    # MacroBodiesAreChecked (a non-admin creates no macro at all here; the body is refused first)
    ("MacroBodies", "CREATE MACRO m1(s) AS TABLE SELECT * FROM query(s)", QUERY),
    ("MacroBodies", "CREATE OR REPLACE TEMP MACRO m2(p) AS TABLE SELECT * FROM read_csv(p)", "read_csv() " + LOCAL),
    ("MacroBodies", "CREATE MACRO m3() AS TABLE SELECT * FROM query('SELECT * FROM duckdb_' || 'secrets()')", QUERY),
    ("MacroBodies", "CREATE MACRO m4(s := 'SELECT 1') AS TABLE SELECT * FROM query(s)", QUERY),
    ("MacroBodies", "CREATE MACRO m5(s) AS (SELECT count(*) FROM query(s))", QUERY),
    ("MacroBodies", "CREATE MACRO m6() AS (SELECT count(*) FROM read_csv('{F}'))", "read_csv() " + LOCAL),
    ("MacroBodies", "CREATE MACRO m7() AS TABLE SELECT * FROM glob('{D}/*')", "glob() " + LOCAL),
    ("MacroBodies", "CREATE MACRO m8(t) AS TABLE SELECT * FROM query_table(t)", "Hardcharger blocked query_table()"),
    ("MacroBodies", "CREATE MACRO m9() AS TABLE SELECT * FROM read_csv('https://a.invalid/b.csv')", "READ HTTP"),
    ("MacroBodies", "CREATE MACRO sniff_csv(p) AS TABLE SELECT * FROM read_csv(p)", "read_csv() " + LOCAL),
    ("MacroBodies", "CREATE MACRO s.settings() AS (SELECT count(*) FROM duckdb_secrets())", "duckdb_secrets()"),
    # CallIsHeldToTheTableFunctionRules
    ("CallIsHeld", "CALL read_csv('{F}')", "read_csv() " + LOCAL),
    ("CallIsHeld", "CALL read_text(getvariable('p'))", "read_text() " + LOCAL),
    ("CallIsHeld", "CALL read_csv('https://example.invalid/x.csv')", "READ HTTP"),
    ("CallIsHeld", "CALL query('SELECT * FROM duckdb_secrets()')", QUERY),
    ("CallIsHeld", "CALL query(getvariable('s'))", QUERY),
    ("CallIsHeld", "CALL json_execute_serialized_sql(json_serialize_sql('SELECT 1'))", TF + "json_execute_serialized_sql"),
    ("CallIsHeld", "PRAGMA json_execute_serialized_sql('{{}}')", "Hardcharger blocked json_execute_serialized_sql()"),
    ("CallIsHeld", "CALL some_function((SELECT count(*) FROM duckdb_secrets()))", "duckdb_secrets()"),
    # QueryTableNamesAreChecked
    ("QueryTableNames", "SELECT * FROM query_table('{F}')", "Hardcharger blocked query_table()"),
    ("QueryTableNames", "SELECT * FROM query_table('data.csv')", "Hardcharger blocked query_table()"),
    ("QueryTableNames", "SELECT * FROM query_table(['t', '{D}/x.parquet'], true)", "Hardcharger blocked query_table()"),
    ("QueryTableNames", "SELECT * FROM query_table('https://example.invalid/x.csv')", "Hardcharger blocked query_table()"),
    ("QueryTableNames", "SELECT * FROM query_table(getvariable('t'))", "Hardcharger blocked query_table()"),
    # StringArgumentsOfAnyTableFunctionAreChecked (1.x's m() / r(): here the administrator's route_sql / route_file)
    ("StringArguments", "SELECT * FROM route_sql('SELECT count(*) FROM duckdb_secrets()')", "duckdb_secrets()"),
    ("StringArguments", "SELECT * FROM route_sql(s := 'SELECT * FROM read_csv(''{F}'')')", "read_csv()"),
    ("StringArguments", "SELECT * FROM route_sql('COPY (SELECT 1) TO ''{D}/x.csv''')", "COPY"),
    ("StringArguments", "SELECT * FROM route_file('{F}')", "read_text()"),
    ("StringArguments", "SELECT * FROM route_file(['{F}'])", "read_text()"),
    ("StringArguments", "SELECT * FROM read_xlsx('{D}/book.xlsx')", "read_xlsx() " + LOCAL),
    ("StringArguments", "SELECT * FROM read_json_objects_auto('events.json')", "read_json_objects_auto() " + LOCAL),
    ("StringArguments", "SELECT * FROM st_read('file://{D}/shapes.shp')", "st_read() " + LOCAL),
    # CallsInUninspectedStatementFormsAreRefused
    *[("UninspectedForms", sql, why) for sql, why in [
        ("DESCRIBE SELECT * FROM read_csv('{F}')", "read_csv() " + LOCAL),
        ("SUMMARIZE SELECT * FROM read_csv('{F}')", "read_csv() " + LOCAL),
        ("SHOW (SELECT * FROM read_csv('{F}'))", "read_csv() " + LOCAL),
        ("SELECT * FROM (DESCRIBE SELECT * FROM read_csv('{F}'))", "read_csv() " + LOCAL),
        ("SET VARIABLE v = (SELECT a FROM read_csv('{F}'))", "read_csv() " + LOCAL),
        ("CREATE TYPE e AS ENUM (SELECT a FROM read_csv('{F}'))", "read_csv() " + LOCAL),
        ("PIVOT (SELECT * FROM read_csv('{F}')) ON a USING count(*)", "read_csv() " + LOCAL),
        ("UPDATE t SET x = (SELECT count(*) FROM duckdb_secrets())", "duckdb_secrets()"),
        ("UPDATE t SET x = r.a FROM read_csv('{F}') r", "read_csv() " + LOCAL),
        ("DELETE FROM t WHERE x IN (SELECT a FROM postgres_query('b', 'select 1'))", TF + "postgres_query"),
        ("DELETE FROM t USING read_csv('{F}') r WHERE t.x = r.a", "read_csv() " + LOCAL),
        ("MERGE INTO t USING read_csv('{F}') r ON t.x = r.a WHEN NOT MATCHED THEN INSERT VALUES (r.a)", "read_csv() " + LOCAL),
        ("DESCRIBE SELECT * FROM query('SELECT * FROM duckdb_secrets()')", QUERY),
        ("SUMMARIZE SELECT * FROM read_parquet('https://example.invalid/x.parquet')", "READ HTTP"),
        ("DESCRIBE SELECT * FROM read_parquet(getvariable('p'))", "read_parquet() " + LOCAL),
        ("DESCRIBE SELECT * FROM read_parquet('s3://b/x' || '.parquet')", "read_parquet() " + LOCAL),
        ("SUMMARIZE '{F}'", "Hardcharger blocked reading a path in FROM"),
        ("DESCRIBE 'data.csv'", "Hardcharger blocked reading a path in FROM"),
        ("DESCRIBE SELECT * FROM '{F}'", "Hardcharger blocked reading a local file"),
        ("SUMMARIZE SELECT * FROM \"{F}\"", "Hardcharger blocked reading a local file"),
        ("SUMMARIZE SELECT * FROM 'https://example.invalid/x.csv'", "READ HTTP"),
        ("UPDATE t SET x = s.a FROM '{F}' s", "Hardcharger blocked reading a path in FROM"),
        ("UPDATE t SET x = s.a FROM u JOIN v ON true, '{F}' s", "Hardcharger blocked reading a local file"),
        ("DELETE FROM t USING '~/x.csv' s WHERE t.x = s.a", "Hardcharger blocked reading a path in FROM"),
        ("MERGE INTO t USING 'data.parquet' s ON t.x = s.a WHEN NOT MATCHED THEN INSERT VALUES (s.a)",
         "Hardcharger blocked reading a path in FROM"),
        ("PIVOT '{F}' ON a USING count(*)", "Hardcharger blocked reading a path in FROM"),
        ("SET VARIABLE v = (SELECT a FROM t, '{F}')", "Hardcharger blocked reading a local file"),
        ("DESCRIBE SELECT * FROM \"read_csv\"('{F}')", "read_csv() " + LOCAL),
        ("DESCRIBE SELECT * FROM READ_CSV /* c */ ('{F}')", "read_csv() " + LOCAL),
        ("DESCRIBE SELECT * FROM main.read_csv('{F}')", "read_csv() " + LOCAL),
        ("DESCRIBE SELECT 'it''s' AS q, * FROM read_csv('{F}')", "read_csv() " + LOCAL),
        ("DESCRIBE SELECT E'\\'' AS q, * FROM read_csv('{F}')", "read_csv() " + LOCAL),
        ("DESCRIBE SELECT $$x$$ AS q, * FROM read_csv('{F}')", "read_csv() " + LOCAL),
        ("DESCRIBE SELECT 1 -- c\n FROM read_csv('{F}')", "read_csv() " + LOCAL),
    ]],
]


@pytest.mark.parametrize("case,template,why", GATED, ids=[f"{c}:{t[:60]}" for c, t, _ in GATED])
def test_1_42_3_gated_statements_are_refused(reach, non_admin, case, template, why):
    refused(session=non_admin, sql=sql_of(reach=reach, template=template), why=why)


# 1.x's "not gated" statements that run for a non-admin here too.
ALLOWED = [
    "SELECT * FROM route_sql('SELECT 42 AS answer')", "SELECT * FROM fine_t(3)", "DESCRIBE SELECT 1 AS x",
    "SUMMARIZE SELECT 1 AS x", "SET VARIABLE harmless = (SELECT 42)",
    "SELECT 'CALL duckdb_secrets() and read_text(''/etc/hosts'')' AS s", "SELECT pg_typeof(1)",
    "CALL pragma_table_info('t')", "PRAGMA table_info('t')", "SELECT * FROM range(10)", "SELECT * FROM generate_series(1, 5)",
    "SUMMARIZE SELECT * FROM orders", "SET VARIABLE v = (SELECT max(id) FROM orders)",
    "PIVOT orders ON region USING sum(amount)", "UPDATE t SET x = (SELECT max(id) FROM orders)",
    "DELETE FROM t WHERE x IN (SELECT id FROM returns)",
    "MERGE INTO t USING staged s ON t.x = s.x WHEN NOT MATCHED THEN INSERT *",
    "UPDATE t SET note = 'see read_csv(''/etc/passwd'') and duckdb_secrets()'",
    "DESCRIBE SELECT 'CALL query(1)' AS s", "DESCRIBE SELECT $$ read_csv('/etc/passwd') $$ AS s",
    "DESCRIBE SELECT E'read_csv(\\'/etc/passwd\\')' AS s", "DESCRIBE SELECT 1 AS x -- read_csv('/etc/passwd')",
    "DESCRIBE SELECT 1 /* duckdb_secrets() */ AS x", "DESCRIBE SELECT 1 AS \"read_csv\"",
    "UPDATE t SET path = '/data/x.csv', note = 'a, /b.csv' WHERE x = 1",
    "UPDATE t SET x = 1 FROM u WHERE u.path IN ('/a.csv', '/b.csv')", "DELETE FROM t WHERE path = '/data/x.csv'",
    "SUMMARIZE SELECT '/etc/passwd' AS p, 'x.csv' AS q FROM orders", "SELECT * FROM unnest(['report.csv', '/tmp/x.txt'])",
    "SELECT * FROM orders, returns WHERE orders.file = '/x.csv'",
    "SELECT \"query\", \"glob\" FROM audit_log WHERE \"query\" IN ('a', 'b')",
    "INSERT INTO t (x) SELECT 1",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_1_42_3_ungated_statements_run(non_admin, sql):
    non_admin.execute(sql=sql)


# 1.x's "not gated" statements Hardcharger refuses a non-admin token, by design (module comment).
STRICTER = [
    ("COPY my_table TO 's3://bucket/out.parquet' (FORMAT parquet)", "WRITE OBJECT STORAGE"),
    ("COPY (SELECT 1 AS i) TO 'gs://bucket/org/main/t/x.parquet' (FORMAT parquet)", "WRITE OBJECT STORAGE"),
    ("COPY my_table FROM 's3://bucket/data.csv'", "READ OBJECT STORAGE"),
    ("SELECT * FROM read_parquet('s3://bucket/x.parquet')", "READ OBJECT STORAGE"),
    ("SELECT * FROM read_json('gs://bucket/data.json')", "READ OBJECT STORAGE"),
    ("SELECT * FROM glob('s3://bucket/*')", "READ OBJECT STORAGE"),
    ("SELECT * FROM 's3://bucket/data.parquet'", "READ OBJECT STORAGE"),
    ("SELECT * FROM 'gs://bucket/other_catalog/data.parquet'", "READ OBJECT STORAGE"),
    ("WITH c AS (COPY (SELECT 42) TO 's3://bucket/out.csv') SELECT 1", "WRITE OBJECT STORAGE"),
    ("DESCRIBE SELECT * FROM read_parquet('s3://bucket/x.parquet')", "READ OBJECT STORAGE"),
    ("DESCRIBE SELECT * FROM 's3://bucket/x.parquet'", "READ OBJECT STORAGE"),
    ("MERGE INTO t USING read_parquet('s3://bucket/staged/*.parquet') s ON t.x = s.x WHEN NOT MATCHED THEN INSERT *",
     "READ OBJECT STORAGE"),
    ("SELECT * FROM delta_scan('s3://bucket/table')", TF + "delta_scan"),
    ("SELECT * FROM query('SELECT 1 AS x')", QUERY),
    ("CALL query('SELECT 1 AS x')", QUERY),
    ("SELECT * FROM query('SELECT * FROM read_parquet(''s3://b/x.parquet'')')", QUERY),
    ("SELECT * FROM query_table('orders')", "Hardcharger blocked query_table()"),
    ("SELECT * FROM query_table(['orders', 'returns'], true)", "Hardcharger blocked query_table()"),
    ("CREATE OR REPLACE TEMP MACRO fine(x) AS x + 1", "creating temporary macros needs an administrator"),
    ("CREATE MACRO add1(x) AS x + 1", "creating macros needs an administrator"),
    ("CREATE MACRO recent(n) AS TABLE SELECT * FROM orders LIMIT n", "creating macros needs an administrator"),
    ("CREATE OR REPLACE MACRO main.settings() AS 42", "creating macros needs an administrator"),
    ("CREATE MACRO IF NOT EXISTS sniff_csv(x) AS x + 1", "creating macros needs an administrator"),
    ("CREATE FUNCTION main.snapshots() AS TABLE SELECT 1 AS x", "creating macros needs an administrator"),
    ("CREATE TYPE mood AS ENUM ('sad', 'ok', 'happy')", "creating types needs an administrator"),
]


@pytest.mark.parametrize("sql,why", STRICTER)
def test_1_42_3_ungated_statements_hardcharger_keeps_from_a_non_admin_token(non_admin, sql, why):
    refused(session=non_admin, sql=sql, why=why)


def test_1_42_3_admin_is_not_blocked_from_cross_catalog_functions(reach):
    admin = reach["server"].admin()
    problem = admin.error(sql="SELECT * FROM postgres_query('no_such_db', 'select 1')")
    assert problem["code"] != "permission_denied" and "Hardcharger blocked" not in problem["detail"], problem
    # And the indirect routes work for the administrator (who made the macros).
    assert admin.scalar(sql="SELECT * FROM route_sql('SELECT count(*) FROM duckdb_secrets()')") >= 0
    admin.execute(sql="CALL duckdb_secrets()")
    assert admin.scalar(sql=sql_of(reach=reach, template="SELECT content FROM route_file('{H}')")) == "host-secret-word\n"
    assert admin.scalar(sql=sql_of(reach=reach, template="SELECT a FROM read_csv('{F}')")) == "file-secret-word"
