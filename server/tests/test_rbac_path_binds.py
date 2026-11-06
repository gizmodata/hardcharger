"""Storage binds pass the same pre-I/O location checks as literal paths, on every run."""
import functools
import http.server
import threading

import pytest

from rbac import ChargewireError
from test_rbac_storage_symlinks import world  # noqa: F401


def denied(call):
    with pytest.raises(ChargewireError) as raised:
        call()
    assert raised.value.code == "permission_denied", str(raised.value)


def test_bound_paths_preserve_locations_symlinks_and_lake_isolation(world):
    lee, eve, work = world["lee"], world["eve"], world["work"]
    allowed = str(work / "shared/a.csv")
    for sql, parameters in [
        ("SELECT x FROM read_csv($path)", {"path": allowed}),
        ("SELECT x FROM read_csv(?)", [allowed]),
        ("SELECT x FROM read_csv($1)", [allowed]),
        ("SELECT x FROM read_csv($PaTh)", {"PATH": allowed}),
        ("SELECT x FROM read_csv($2) WHERE x = $1", [1, allowed]),
        ("SELECT x FROM read_csv([$first, $second])", {"first": allowed, "second": allowed}),
    ]:
        assert set(lee.run(sql=sql, parameters=parameters).column("x").to_pylist()) == {1}
    for path in [work / "secret/s.csv", work / "shared/../secret/s.csv", work / "shared/s_link.csv",
                 work / "shared/link/s.csv", work / "secret/not-present.csv"]:
        denied(call=lambda: lee.run(sql="SELECT * FROM read_csv($path)", parameters={"path": str(path)}))
    denied(call=lambda: lee.run(sql="SELECT * FROM read_csv([$a, $b])",
                               parameters={"a": allowed, "b": str(work / "secret/s.csv")}))
    denied(call=lambda: lee.run(sql="SELECT * FROM read_csv($path)",
                               parameters={"path": allowed, "PATH": str(work / "secret/s.csv")}))
    for parameters in [{}, {"path": None}, {"path": 42}]:
        denied(call=lambda: lee.run(sql="SELECT * FROM read_csv($path)", parameters=parameters))
    # An expression still requires broader privileges; binding is not expression evaluation.
    denied(call=lambda: lee.run(sql="SELECT * FROM read_csv($path || '')", parameters={"path": allowed}))
    denied(call=lambda: eve.run(sql="SELECT * FROM read_parquet($path)",
                               parameters={"path": str(work / "beside/lake_file.parquet")}))
    # COPY checks the destination value, too, before any file is created.
    destination = work / "inbox/bound.csv"
    lee.run(sql="COPY (SELECT 8 AS x) TO $path (FORMAT csv, HEADER)", parameters={"path": str(destination)})
    assert destination.exists()
    outside = work / "out/forbidden.csv"
    denied(call=lambda: lee.run(sql="COPY (SELECT 8 AS x) TO $path (FORMAT csv)", parameters={"path": str(outside)}))
    assert not outside.exists()


def test_prepared_path_rechecks_each_value_and_revocation(world):
    lee, work, admin = world["lee"], world["work"], world["admin"]
    prepared = lee.prepare(sql="SELECT x FROM read_csv($path)")
    allowed = str(work / "shared/a.csv")
    assert lee.execute_prepared(prepared_id=prepared, parameters={"path": allowed}).column("x").to_pylist() == [1]
    denied(call=lambda: lee.execute_prepared(prepared_id=prepared, parameters={"path": str(work / "secret/s.csv")}))
    assert lee.execute_prepared(prepared_id=prepared, parameters={"path": allowed}).column("x").to_pylist() == [1]
    # A fresh role/user keeps the shared fixture's grants intact for other tests.
    admin.run(sql="CREATE ROLE bound_revoke")
    admin.run(sql="CREATE USER bound_user")
    admin.run(sql="GRANT bound_revoke TO bound_user")
    grant = f"READ FILESYSTEM ON LOCATION '{work}/shared'"
    admin.run(sql=f"GRANT {grant} TO bound_revoke")
    user = world["server"].user(name="bound_user")
    prepared = user.prepare(sql="SELECT x FROM read_csv($1)")
    assert user.execute_prepared(prepared_id=prepared, parameters=[allowed]).column("x").to_pylist() == [1]
    admin.run(sql=f"REVOKE {grant} FROM bound_revoke")
    with pytest.raises(ChargewireError) as raised:
        user.execute_prepared(prepared_id=prepared, parameters=[allowed])
    assert raised.value.code in ("permission_denied", "session_ended"), str(raised.value)


def test_forbidden_http_bind_is_refused_before_any_request(world, tmp_path):
    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "public/a.csv").write_text("x\n1\n")
    (tmp_path / "private/secret.csv").write_text("secret\nnever-read\n")
    seen = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            super().do_GET()

        def do_HEAD(self):
            seen.append(self.path)
            super().do_HEAD()

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        admin = world["admin"]
        admin.run(sql="INSTALL httpfs; LOAD httpfs")
        admin.run(sql="CREATE ROLE bound_web")
        admin.run(sql="CREATE USER bound_web_user")
        admin.run(sql="GRANT bound_web TO bound_web_user")
        admin.run(sql=f"GRANT READ HTTP ON LOCATION '{base}/public' TO bound_web")
        user = world["server"].user(name="bound_web_user")
        prepared = user.prepare(sql="SELECT * FROM read_csv($path)")
        assert seen == []  # preparation with no path value cannot fetch anything
        allowed = {"path": base + "/public/a.csv"}
        assert user.execute_prepared(prepared_id=prepared, parameters=allowed).column("x").to_pylist() == [1]
        seen.clear()
        forbidden = {"path": base + "/private/secret.csv"}
        denied(call=lambda: user.run(sql="SELECT * FROM read_csv($path)", parameters=forbidden))
        denied(call=lambda: user.execute_prepared(prepared_id=prepared, parameters=forbidden))
        assert seen == []  # denial is before binding opens the URL, not after fetching
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
