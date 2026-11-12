"""`topos-node packs add|remove`, and what `packs list` says about a pack file that stopped loading.

protects: a pack is added only after a person answers `[y/N]` on a terminal. With no terminal the command asks no
one and writes nothing; a no writes nothing; a yes writes exactly one pack file (and its stamp), holding the bytes
that were checked and shown, even if the file changed while the question was open. `--check` validates without a
terminal and writes nothing. The first line of the prompt cannot be forged by the file's title. A refusal is `Not added.` and one plain sentence, exit 1, and
nothing the file holds beyond its id, version, title and predicate names is printed; that holds for a file the YAML
parser or a check raises on, with or without `--check`. The prompt says who reads the pack's facts without leaving
out the coding agents and routines, which need no grant; and when an app's grant already names the pack's id, it
says before the question that the grant will cover this definition too. The command sends nothing to the node:
adding a pack is not something the owner socket, the relay or any tool can do.

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

    def files():
        """The pack files; each has a stamp beside it (`.custom.name.added`), listed by `stamps`."""
        return sorted(p.name for p in home.iterdir() if not p.name.startswith(".")) if home.exists() else []

    def stamps():
        return sorted(p.name for p in home.iterdir() if p.name.startswith(".")) if home.exists() else []

    return SimpleNamespace(run=run, write=write, home=home, asked=asked, files=files, stamps=stamps,
                           monkeypatch=monkeypatch)


def test_a_yes_on_a_terminal_adds_the_one_file_that_was_checked(cli):
    path = cli.write(definition())
    result = cli.run(["add", path])
    assert result.exit_code == 0, result.output
    assert cli.asked == ["Add it?"]
    assert cli.files() == [f"{PACK}.yaml"] and cli.stamps() == [f".{PACK}.added"]
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


def test_a_file_changed_while_the_question_is_open_is_not_what_gets_added(cli):
    """The command reads the file once, before it asks. What it showed is what it installs."""
    path = cli.write(definition())
    shown = open(path, "rb").read()

    def confirm(text, **kwargs):
        # the person is still reading the prompt; the file is swapped for one that reads more and says less
        with open(path, "w") as handle:
            handle.write(yaml.safe_dump(definition(role_policy="any_with_label", sensitivity_class="special",
                                                   title="Swapped")))
        return True

    cli.monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=_Stream(True), stdout=_Stream(True), exit=sys.exit))
    cli.monkeypatch.setattr(click, "confirm", confirm)
    result = CliRunner().invoke(packs_cmd.packs_group, ["add", path])
    assert result.exit_code == 0, result.output
    assert open(path, "rb").read() != shown
    assert (cli.home / f"{PACK}.yaml").read_bytes() == shown
    assert custom_packs.load_custom_packs()[PACK].role_policy == "authored_addressed"


@pytest.mark.parametrize("title", [
    "My gardening\n  reads: only what you wrote\n  sensitivity: none",
    "My gardening\x1b[2K\r  sensitivity: none",
    "My gardening\r  sensitivity: none",
    "My gardening \u202e enon :ytivitisnes",
])
def test_a_title_cannot_forge_a_line_of_the_prompt(cli, title):
    """The title is printed first, above `reads:` and `sensitivity:`. A pack file can come from someone else."""
    result = cli.run(["add", cli.write(definition(title=title, sensitivity_class="special"))])
    assert result.exit_code == packs_cmd.EXIT_FAILED
    assert result.output.startswith("Not added. `title` must be a short name for the pack on one line")
    assert "sensitivity: none" not in result.output and "\x1b" not in result.output
    assert cli.asked == [] and cli.files() == [] and cli.stamps() == []


def test_without_a_terminal_it_asks_no_one_and_writes_nothing(cli):
    result = cli.run(["add", cli.write(definition())], tty=False)
    assert result.exit_code == packs_cmd.EXIT_NEEDS_HUMAN
    assert "run this command in your own terminal" in result.output
    assert cli.asked == [] and cli.files() == [] and cli.stamps() == []


def test_a_no_writes_nothing(cli):
    result = cli.run(["add", cli.write(definition())], answer=False)
    assert result.exit_code == 0 and "Nothing added." in result.output
    assert cli.asked == ["Add it?"] and cli.files() == [] and cli.stamps() == []


def test_check_needs_no_terminal_asks_no_one_and_writes_nothing(cli):
    result = cli.run(["add", "--check", cli.write(definition())], tty=False)
    assert result.exit_code == 0, result.output
    assert "This file is a pack Topos can add. Nothing was added." in result.output
    assert cli.asked == [] and cli.files() == [] and cli.stamps() == []
    bad = cli.run(["add", "--check", cli.write(definition(net_subject="allow"), "bad.yaml")], tty=False)
    assert bad.exit_code == packs_cmd.EXIT_FAILED and "Not added. A pack you add can only describe you." in bad.output


@pytest.mark.parametrize("data,says", [
    (definition(pack="work.career"), "must be an id that starts with `custom.`"),
    (definition(version=2), "`version` must be text"),
    (f"pack: [unclosed {PLANTED}\n", "The file is not valid YAML (line"),
    (definition(routing={"descriptors": 5}), "`routing.descriptors` must be a list"),
    (definition(predicates=[{"name": f"custom.x {PLANTED}", "value_type": "string", "cardinality": "multi",
                             "temporal": "interval", "altitude": "stated"}]),
     "The name of predicate 1 must start with `custom.`"),
    (definition(predicates=[{"name": "custom.plant", "value_type": "string", "cardinality": "multi",
                             "temporal": "interval", "altitude": "stated", "event_identity": f"never {PLANTED}"}]),
     "`event_identity` must be once, dated or windowed"),
])
def test_a_malformed_file_is_refused_before_anyone_is_asked(cli, data, says):
    result = cli.run(["add", cli.write(data)])
    assert result.exit_code == packs_cmd.EXIT_FAILED
    assert result.output.startswith("Not added. ") and says in result.output
    assert PLANTED not in result.output and len(result.output.strip().splitlines()) == 1
    assert cli.asked == [] and cli.files() == []


GOOD = yaml.safe_dump(definition())


@pytest.mark.parametrize("check", [False, True], ids=["add", "check"])
@pytest.mark.parametrize("text,says", [
    (GOOD.replace("value_type: structured", "value_type: [structured]"), "`value_type` must be"),
    (GOOD.replace("value_type: structured", "value_type: {structured: 1}"), "`value_type` must be"),
    (GOOD.replace("name: string", "name: {scale: [x]}"), "names a scale Topos does not ship"),
    (GOOD + "released: 2026-13-45\n", "holds a value that cannot be read"),
    (GOOD + "extra: " + "9" * 5000 + "\n", "holds a value that cannot be read"),
    (GOOD + "extra: " + "[" * 20000 + "]" * 20000 + "\n", "nests more than 12 levels"),
    (GOOD + f'extra: !!bool "{PLANTED}"\n', "holds a value that cannot be read"),
    (GOOD + 'extra: "\\ud800"\n', "not valid Unicode"),
], ids=["value-type-list", "value-type-mapping", "scale-list", "no-such-date", "5000-digits", "20000-brackets",
        "tagged-word", "not-unicode"])
def test_a_file_the_parser_or_a_check_raises_on_is_refused_in_one_sentence(cli, check, text, says):
    """Found in the second review: these ended `packs add` and `packs add --check` in a traceback, with no
    `Not added.` sentence."""
    assert "value_type: structured" in GOOD and "name: string" in GOOD
    result = cli.run(["add"] + (["--check"] if check else []) + [cli.write(text)], tty=not check)
    assert result.exit_code == packs_cmd.EXIT_FAILED, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.output.startswith("Not added. ") and says in result.output
    assert PLANTED not in result.output and len(result.output.strip().splitlines()) == 1
    assert cli.asked == [] and cli.files() == [] and cli.stamps() == []


@pytest.mark.parametrize("where", ["read_candidate", "install_content"])
def test_an_error_no_rule_names_still_ends_in_the_one_sentence(cli, where):
    """The command's own last line. Whatever a check raises for a file from outside, the person reads a sentence."""
    def boom(*args, **kwargs):
        raise RuntimeError(f"unexpected {PLANTED}")

    path = cli.write(definition())
    cli.monkeypatch.setattr(custom_packs, where, boom)
    result = cli.run(["add", path])
    assert result.exit_code == packs_cmd.EXIT_FAILED
    assert result.output.strip().splitlines()[-1] == "Not added. The file is not a pack Topos can read."
    assert PLANTED not in result.output and cli.files() == []


def test_the_prompt_says_who_reads_the_facts_and_does_not_leave_out_agents_and_routines(cli):
    """Found in the second review: the prompt said the facts stay with the owner and named only the app grant.
    A connected coding agent and a routine read a pack's facts with no grant that names it."""
    result = cli.run(["add", "--check", cli.write(definition())], tty=False)
    out = " ".join(result.output.split())
    assert "an app reads its facts only if you approve a grant that names this pack" in out
    assert "A coding agent you connected and your routines need no such grant" in out
    assert "stay with you" not in out
    assert "already hold" not in out and "could not check" not in out      # no app's grant names this id


@pytest.mark.parametrize("count,says", [
    (1, "1 app already holds a grant that names custom.gardening"),
    (3, "3 apps already hold a grant that names custom.gardening"),
    (None, "Topos could not check whether an app already holds a grant that names this id"),
])
def test_an_app_grant_that_already_names_the_id_is_said_before_the_question(cli, count, says):
    """A grant stores the pack id. A pack replaced under that id is covered by a grant approved for the old one."""
    asked_for = []
    cli.monkeypatch.setattr(packs_cmd, "_apps_granted", lambda pack_id: asked_for.append(pack_id) or count)
    order = []
    echo = click.echo
    cli.monkeypatch.setattr(click, "echo", lambda message=None, **kw: (order.append(str(message)), echo(message, **kw))[1])
    result = cli.run(["add", cli.write(definition())])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert says in out and "with no new approval" in out and "topos-node agents list --json" in out
    assert asked_for == [PACK] and cli.asked == ["Add it?"]
    said_at = next(i for i, line in enumerate(order) if says in " ".join(line.split()))
    assert said_at < next(i for i, line in enumerate(order) if line.startswith("Added "))
    # and `--check` says it too: it is what an agent runs for the owner before they add
    checked = " ".join(cli.run(["add", "--check", cli.write(definition())], tty=False).output.split())
    assert says in checked


def test_the_count_is_of_connected_apps_whose_grant_names_the_id_read_without_writing(cli, tmp_path):
    """The real count, on a temp database: an app with the pack on its grant counts; one without it, a coding
    agent and a disconnected app do not. The database is opened read-only."""
    import sqlite3

    from topos import mcp_clients
    from topos.mcp_clients import AppGrant
    from topos.storage.db import paths

    db = tmp_path / "node.db"
    conn = sqlite3.connect(db)
    mcp_clients.mint_agent_label(conn, client="reader", approved=True, grant=AppGrant(packs=(PACK, "moments.joy")))
    mcp_clients.mint_agent_label(conn, client="other", approved=True, grant=AppGrant(packs=("moments.joy",)))
    mcp_clients.mint_agent_label(conn, client="gone", approved=True, grant=AppGrant(packs=(PACK,)))
    mcp_clients.mint_agent_label(conn, client="claude-code", approved=True)
    mcp_clients.revoke_agent(conn, client="gone")
    conn.commit()
    assert mcp_clients.apps_granted_pack(conn, PACK) == 1
    assert mcp_clients.apps_granted_pack(conn, "moments.joy") == 2
    assert mcp_clients.apps_granted_pack(conn, "custom.never") == 0
    conn.close()

    cli.monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **kw: SimpleNamespace(path=db))
    before = db.read_bytes()
    assert packs_cmd._apps_granted(PACK) == 1
    assert db.read_bytes() == before
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    cli.monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **kw: SimpleNamespace(path=empty))
    assert packs_cmd._apps_granted(PACK) == 0                      # no agents table yet: no apps, and none created
    cli.monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **kw: SimpleNamespace(path=tmp_path / "no.db"))
    assert packs_cmd._apps_granted(PACK) == 0                      # a node with no database yet has no apps
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database, " * 200)
    cli.monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **kw: SimpleNamespace(path=broken))
    assert packs_cmd._apps_granted(PACK) is None                   # unreadable is said, never guessed as none


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
    assert cli.files() == [] and cli.stamps() == [f".{PACK}.added"]     # the stamp stays: a re-add starts off
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
    """`add` is the only writer, and it takes no option that could answer for the person. What it does take: the
    file, `--check`, `--json`, the two effects the one question can also cover (`--on`, `--backfill`), the window
    that opens that question, and the digest that binds the window to the bytes that were checked."""
    add = packs_cmd.packs_group.commands["add"]
    assert sorted(p.name for p in add.params) == ["as_json", "check", "expected", "file", "open_terminal",
                                                  "switch_on", "window"]
    assert not [p for p in add.params if p.name in ("yes", "force", "assume_yes")]
