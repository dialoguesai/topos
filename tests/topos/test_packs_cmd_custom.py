"""`topos-node packs add|remove`, and what `packs list` says about a pack file that stopped loading.

protects: a pack is added only after a person answers `[y/N]` on a terminal. With no terminal the command asks no
one and writes nothing; a no writes nothing; a yes writes exactly one file, the one that was checked. `--check`
validates without a terminal and writes nothing. A refusal is `Not added.` and one plain sentence, exit 1, and
nothing the file holds beyond its id, version, title and predicate names is printed. The command sends nothing to
the node: adding a pack is not something the owner socket, the relay or any tool can do.

A terminal and a person are patched (``isatty`` on the module's ``sys``, ``click.confirm``); the pack directory
is a temp directory. Synthetic only.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import click
import pytest
import yaml
from click.testing import CliRunner

from topos.cli import packs_cmd
from topos.features.derivation import custom_packs

pytestmark = pytest.mark.public

PACK = "custom.gardening"
PLANTED = "tpn_" + "planted" + "0123456789abcdef" * 2


def definition(**changes):
    data = {
        "pack": PACK, "version": "1.0.0", "title": "My gardening",
        "sensitivity_class": "personal", "role_policy": "authored_addressed", "net_subject": "deny",
        "routing": {"descriptors": [f"Plants the owner grows {PLANTED}"]},
        "guidance": {"definitions": f"Plants the owner says they grow. {PLANTED}"},
        "predicates": [{"name": "custom.plant", "value_type": "structured", "cardinality": "multi",
                        "temporal": "interval", "altitude": "stated", "value_schema": {"name": "string"}},
                       {"name": "custom.bed", "value_type": "string", "cardinality": "multi",
                        "temporal": "interval", "altitude": "stated"}],
        "eval": {"gold": [{"text": f"I grow tomatoes {PLANTED}", "expect": []}],
                 "negative_controls": [{"text": "A film about a garden", "expect": []}]},
        "consumers": ["assistant_context"],
    }
    data.update(changes)
    return data


class _Stream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.fixture
def cli(tmp_path, monkeypatch):
    home = tmp_path / "node" / "fact-packs"
    monkeypatch.setattr(custom_packs, "custom_pack_dir", lambda: home)
    custom_packs._SAID.clear()
    asked = []

    def socket(*args, **kwargs):
        raise AssertionError("adding or removing a pack must not talk to the node")

    def run(args, *, tty=True, answer=True):
        def confirm(text, **kwargs):
            asked.append(text)
            return answer

        monkeypatch.setattr(packs_cmd, "_socket", socket)
        monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=_Stream(tty), stdout=_Stream(tty), exit=sys.exit))
        monkeypatch.setattr(click, "confirm", confirm)
        return CliRunner().invoke(packs_cmd.packs_group, args)

    def write(data, name="my-pack.yaml"):
        path = tmp_path / name
        path.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
        return str(path)

    return SimpleNamespace(run=run, write=write, home=home, asked=asked,
                           files=lambda: sorted(p.name for p in home.iterdir()) if home.exists() else [])


def test_a_yes_on_a_terminal_adds_the_one_file_that_was_checked(cli):
    path = cli.write(definition())
    result = cli.run(["add", path])
    assert result.exit_code == 0, result.output
    assert cli.asked == ["Add it?"]
    assert cli.files() == [f"{PACK}.yaml"]
    assert (cli.home / f"{PACK}.yaml").read_bytes() == open(path, "rb").read()
    out = result.output
    assert f"{PACK}  1.0.0  My gardening" in out
    assert "reads: what you wrote and what was written to you" in out
    assert "sensitivity: personal" in out
    assert "writes 2 kinds of fact, about you only: custom.bed, custom.plant" in out
    assert "It starts switched off." in out and "only if you approve a grant that names this pack" in out
    assert f"Added {PACK} 1.0.0." in out and "It is switched off." in out
    assert f"topos-node packs backfill --pack {PACK}" in out
    assert PLANTED not in out                      # guidance, routing and examples are never printed


def test_without_a_terminal_it_asks_no_one_and_writes_nothing(cli):
    result = cli.run(["add", cli.write(definition())], tty=False)
    assert result.exit_code == packs_cmd.EXIT_NEEDS_HUMAN
    assert "run this command in your own terminal" in result.output
    assert cli.asked == [] and cli.files() == []


def test_a_no_writes_nothing(cli):
    result = cli.run(["add", cli.write(definition())], answer=False)
    assert result.exit_code == 0 and "Nothing added." in result.output
    assert cli.asked == ["Add it?"] and cli.files() == []


def test_check_needs_no_terminal_asks_no_one_and_writes_nothing(cli):
    result = cli.run(["add", "--check", cli.write(definition())], tty=False)
    assert result.exit_code == 0, result.output
    assert "This file is a pack Topos can add. Nothing was added." in result.output
    assert cli.asked == [] and cli.files() == []
    bad = cli.run(["add", "--check", cli.write(definition(net_subject="allow"), "bad.yaml")], tty=False)
    assert bad.exit_code == packs_cmd.EXIT_FAILED and "Not added. A pack you add can only describe you." in bad.output


@pytest.mark.parametrize("data,says", [
    (definition(pack="work.career"), "must be an id that starts with `custom.`"),
    (definition(version=2), "`version` must be text"),
    (f"pack: [unclosed {PLANTED}\n", "The file is not valid YAML (line"),
])
def test_a_malformed_file_is_refused_before_anyone_is_asked(cli, data, says):
    result = cli.run(["add", cli.write(data)])
    assert result.exit_code == packs_cmd.EXIT_FAILED
    assert result.output.startswith("Not added. ") and says in result.output
    assert PLANTED not in result.output and len(result.output.strip().splitlines()) == 1
    assert cli.asked == [] and cli.files() == []


def test_a_new_version_replaces_the_pack_and_says_so_and_the_same_file_says_unchanged(cli):
    assert cli.run(["add", cli.write(definition())]).exit_code == 0
    same = cli.run(["add", cli.write(definition())])
    assert same.exit_code == 0 and f"{PACK} 1.0.0 is already added, unchanged." in same.output
    changed = cli.run(["add", cli.write(definition(title="Renamed"))])
    assert changed.exit_code == packs_cmd.EXIT_FAILED and "Give the changed pack a new `version`" in changed.output
    newer = cli.run(["add", cli.write(definition(title="Renamed", version="1.1.0"))])
    assert newer.exit_code == 0 and f"Added {PACK} 1.1.0 (it replaces 1.0.0)." in newer.output
    assert "switched off until you switch it on again" in newer.output
    assert cli.files() == [f"{PACK}.yaml"]


def test_remove_takes_out_only_a_pack_the_owner_added(cli):
    cli.run(["add", cli.write(definition())])
    bundled = cli.run(["remove", "work.career"])
    assert bundled.exit_code == packs_cmd.EXIT_FAILED and "Only a pack you added can be removed" in bundled.output
    removed = cli.run(["remove", PACK], tty=False)
    assert removed.exit_code == 0 and f"Removed {PACK}." in removed.output and "facts it already wrote are kept" in removed.output
    assert cli.files() == []
    again = cli.run(["remove", PACK])
    assert again.exit_code == packs_cmd.EXIT_FAILED and "is not a pack you added" in again.output


def test_list_names_a_pack_file_that_is_not_loading(cli, monkeypatch):
    cli.run(["add", cli.write(definition())])
    (cli.home / "custom.broken.yaml").write_text(f"pack: [unclosed {PLANTED}\n")
    listing = {"packs": [{"pack_id": PACK, "enabled": False, "fact_count": 0, "last_run_at": None}]}
    monkeypatch.setattr(packs_cmd, "_request", lambda sock, method, path, **kw: listing)
    monkeypatch.setattr(packs_cmd, "_socket", lambda *a, **kw: object())
    result = CliRunner().invoke(packs_cmd.packs_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "Not loaded: custom.broken.yaml. The file is not valid YAML (line " in result.output
    assert PLANTED not in result.output


def test_the_group_has_no_command_that_adds_a_pack_without_asking():
    """`add` is the only writer, and it takes no option that could answer for the person."""
    add = packs_cmd.packs_group.commands["add"]
    assert sorted(p.name for p in add.params) == ["check", "file"]
    assert not [p for p in add.params if p.name in ("yes", "force", "assume_yes")]
