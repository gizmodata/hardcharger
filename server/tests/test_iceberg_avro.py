"""LOAD iceberg on a server that never installs or loads an extension by itself
(autoinstall_known_extensions and autoload_known_extensions are off): iceberg needs avro,
and loads it itself once it is installed. Without it, the error says first what to run.
Needs the network once to INSTALL iceberg and avro (DuckDB's extension repository);
without it these fail, they don't skip. Both LOADs run in the init SQL, before the server
runs anything else (the extensions are DuckDB's prebuilt downloads; see test_geoarrow.py)."""


def test_load_iceberg_without_avro_says_what_to_install(launcher, tmp_path):
    extensions = tmp_path / "extensions"  # empty: neither is installed
    running = launcher.start(args=["--init-sql-commands",
                                   f"SET extension_directory = '{extensions}'; INSTALL iceberg; LOAD iceberg;"],
                             expect_up=False, startup_seconds=120)
    assert running.process.wait(timeout=180) == 1
    log = running.log()
    assert ("init SQL failed: Hardcharger: the iceberg extension needs the avro extension installed. "
            "Run INSTALL avro; then LOAD iceberg again") in log, log


def test_load_iceberg_with_avro_installed_loads_both(launcher, tmp_path):
    extensions = tmp_path / "extensions"
    init = f"SET extension_directory = '{extensions}'; INSTALL iceberg; INSTALL avro; LOAD iceberg;"
    running = launcher.start(args=["--init-sql-commands", init],
                             startup_seconds=120)
    session = running.admin()
    loaded = session.query(sql="SELECT extension_name FROM duckdb_extensions() "
                               "WHERE loaded AND extension_name IN ('iceberg', 'avro') ORDER BY 1")
    assert loaded.column(0).to_pylist() == ["avro", "iceberg"]
