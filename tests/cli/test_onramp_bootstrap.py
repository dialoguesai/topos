"""Fresh CLI processes must not inherit the test runner's Settings or node key."""
import json
import os
import subprocess
import sys


def _run(tmp_path, code):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOPOS_")}
    env.update(HOME=str(tmp_path), TOPOS_ENV_FILE=str(tmp_path / ".topos" / ".env"),
               TOPOS_DATABASE_PATH=str(tmp_path / "node.db"))
    return subprocess.run([sys.executable, "-c", code], env=env, text=True,
                          capture_output=True, timeout=30)


def test_agent_list_bootstraps_saved_install_without_exported_key(tmp_path):
    folder = tmp_path / ".topos"
    folder.mkdir()
    (folder / ".env").write_text("TOPOS_KEY=synthetic-test-only\n")
    import sqlite3
    sqlite3.connect(tmp_path / "node.db").close()
    result = _run(tmp_path, "from topos.cli.commands import main; main(['agents','list','--json'])")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert "synthetic-test-only" not in result.stdout + result.stderr


def test_pair_origin_resolves_before_a_key_exists(tmp_path):
    result = _run(tmp_path, "from topos.cli.pair_cmd import _cp_http_base; print(_cp_http_base())")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://cp.logu3s.com"


def test_mcp_registry_bootstraps_saved_install_in_a_fresh_process(tmp_path):
    folder = tmp_path / ".topos"
    folder.mkdir()
    (folder / ".env").write_text("TOPOS_KEY=synthetic-test-only\n")
    result = _run(tmp_path, '''
import click
from topos.cli.commands import main
@click.command('mcp')
def probe():
    from topos.mcp_server import registry_entries
    print(len(registry_entries()))
main.add_command(probe)
main(['mcp'])
''')
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0
    assert "synthetic-test-only" not in result.stdout + result.stderr
