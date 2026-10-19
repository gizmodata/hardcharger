"""Where DuckDB keeps installed extensions and persistent secrets when the server has no
usable home directory (services and containers often run without HOME, and a container
run as a user the image has no account for gets HOME=/).

DuckDB puts both under HOME and, without one, couldn't INSTALL at all ("Can't find the
home directory at ''", a 500) and kept persistent secrets relative to the working
directory. Now: with HOME unset, DuckDB's home_directory is the account's home (the
password database, as '~' is for the sensitive-path guard); with no writable home, both
directories go under the data directory (the database file's directory), in .duckdb/.

INSTALL is pointed at a repository that can't answer (port 9 on the loopback), so the
tests need no network: an INSTALL that gets as far as the download has a usable
extension directory, where the old failure came before it.
"""

import pathlib
import sys

import pytest

NOWHERE = "http://127.0.0.1:9/no-extensions"
# Init SQL: no download can succeed, and (HOME unset on a developer's machine means the
# real home) persistent secrets stay out of it.
OFFLINE = f"SET GLOBAL custom_extension_repository = '{NOWHERE}'; SET GLOBAL allow_persistent_secrets = false;"


def no_home(value: str) -> dict[str, str]:
    """HOME (USERPROFILE on Windows) as `value`: empty is unset for DuckDB and the server."""
    env = {"HOME": value, "HARDCHARGER_INIT_SQL_COMMANDS": OFFLINE}
    if sys.platform == "win32":
        env["USERPROFILE"] = value
    return env


def directories(session) -> dict:
    return session.rows(sql="SELECT current_setting('home_directory') AS home, "
                            "current_setting('extension_directory') AS extensions, "
                            "current_setting('secret_directory') AS secrets")[0]


def assert_install_reaches_the_download(session) -> None:
    detail = session.error(sql="INSTALL spatial")["detail"]
    assert "home directory" not in detail, detail
    assert "127.0.0.1:9" in detail, detail


@pytest.mark.skipif(sys.platform == "win32", reason="the password database is POSIX's")
def test_without_home_duckdb_uses_the_accounts_home(launcher):
    import pwd
    import os
    server = launcher.start(env=no_home(value=""))
    session = server.admin()
    account_home = pwd.getpwuid(os.getuid()).pw_dir
    assert directories(session=session)["home"] == account_home
    assert_install_reaches_the_download(session=session)
    assert "HOME is not set: DuckDB's home directory is the account's" in server.log()


def test_without_a_writable_home_the_directories_are_in_the_data_directory(launcher, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    server = launcher.start(env=no_home(value=str(tmp_path / "no-such-home")),
                            args=["--database", str(data / "db.duckdb")])
    session = server.admin()
    found = directories(session=session)
    assert pathlib.Path(found["extensions"]) == data / ".duckdb" / "extensions", found
    assert pathlib.Path(found["secrets"]) == data / ".duckdb" / "stored_secrets", found
    assert_install_reaches_the_download(session=session)
    assert "no writable home directory" in server.log()


def test_a_writable_home_is_left_to_duckdb(launcher, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = no_home(value=str(home))
    server = launcher.start(env=env)
    found = directories(session=server.admin())
    assert found["home"] == "" and found["extensions"] == "", found  # DuckDB's own default, under HOME
    assert pathlib.Path(found["secrets"]) == home / ".duckdb" / "stored_secrets", found
