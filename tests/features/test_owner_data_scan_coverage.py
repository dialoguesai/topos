"""The owner-data scanner reads every file that is text, whatever it is called.

It used to keep only files whose names ended in one of eighteen suffixes and
skip everything else without a word: shell scripts, JavaScript, SVG,
Dockerfiles, justfiles. The filter applied to a path named on the command line
as well, so scanning a shell script BY NAME checked nothing and exited clean.
All three repos run this scanner at commit and again, over the whole tree,
before every push, so each of them reported "clean" over files it never opened.

A guard may be silent only where there is nothing to leak. So what a file
contains decides whether it is read, and a path someone named that cannot be
read as text is refused out loud rather than passed.

Every run here names ``--database`` explicitly. The default is the owner's live
database, and a test must never open it.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

SCANNER = os.path.abspath(os.path.join("scripts", "scan_repo_for_owner_data.py"))
#: Invented, and a name nobody has: the owner's own pre-commit scan reads this
#: file too, and must not fire on it.
PLANTED = "Pelloquin Zarathand"

#: One file of each kind the suffix list skipped, each ordinary for its type.
PLANTS = {
    "scripts/deploy.sh": f"#!/bin/sh\n# ask {PLANTED} before running this\necho ok\n",
    "web/app.js": f'export const reviewer = "{PLANTED}";\n',
    "web/logo.svg": (
        '<svg xmlns="http://www.w3.org/2000/svg">\n'
        f"  <title>drawn for {PLANTED}</title>\n</svg>\n"
    ),
    "Dockerfile": f'FROM scratch\nLABEL maintainer="{PLANTED}"\n',
}

#: What a PNG starts with: git's binary test fires on the NUL in its header.
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + PLANTED.encode()


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True)


def _scan(repo, *args):
    root, terms = repo
    return subprocess.run(
        [sys.executable, SCANNER, "--database", "/nonexistent.db",
         "--local-terms", str(terms), *args],
        cwd=str(root), capture_output=True, text=True,
    )


@pytest.fixture()
def repo(tmp_path):
    """A git checkout plus a terms file naming PLANTED.

    The terms file sits outside the checkout: the scanner refuses to read one
    from inside a repository.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", ".", cwd=root)
    # Git's default, pinned: a global quotePath=false would hide the quoting case.
    _git("config", "core.quotePath", "true", cwd=root)
    terms = tmp_path / "terms.txt"
    terms.write_text(f"{PLANTED}\n", encoding="utf-8")
    return root, terms


def _write(root, rel, body):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")
    return path


# ------------------------------------------------ every text file is read

@pytest.mark.parametrize("rel", sorted(PLANTS))
def test_the_pre_push_scan_reads_it_whatever_it_is_called(repo, rel):
    """``--all`` is the pre-push gate. It scans TRACKED files, so these are added."""
    root, _ = repo
    _write(root, rel, PLANTS[rel])
    _git("add", rel, cwd=root)
    out = _scan(repo, "--all")
    assert out.returncode == 1, f"{rel} was not read:\n{out.stdout}"
    assert f"{rel}:" in out.stderr


@pytest.mark.parametrize("rel", sorted(PLANTS))
def test_a_file_named_on_the_command_line_is_read(repo, rel):
    """How pre-commit calls it: the staged paths, by name."""
    root, _ = repo
    _write(root, rel, PLANTS[rel])
    out = _scan(repo, rel)
    assert out.returncode == 1, f"{rel} was not read:\n{out.stdout}"
    assert f"{rel}:" in out.stderr


def test_a_path_git_would_quote_is_read(repo):
    """``git ls-files`` quotes a name holding a non-ASCII byte; the quoted string
    names no file. One tracked Markdown file was skipped that way on every push."""
    root, _ = repo
    _write(root, "notes/café.md", f"met {PLANTED}\n")
    _git("add", "-A", cwd=root)
    out = _scan(repo, "--all")
    assert out.returncode == 1, out.stdout
    assert "café.md:" in out.stderr


def test_a_file_inside_a_new_directory_is_read(repo):
    """With no arguments the scanner reads the changed and untracked files. Git
    reports a new directory as one entry, ``dir/``, which is not a file."""
    root, _ = repo
    _write(root, "drafts/notes.md", f"met {PLANTED}\n")
    out = _scan(repo)
    assert out.returncode == 1, out.stdout
    assert "drafts/notes.md:" in out.stderr


def test_source_with_a_stray_nul_byte_is_still_read(repo):
    """Git's binary test (a NUL in the first 8000 bytes) calls this binary. It is
    TypeScript with a NUL inside a string, and the old reader read it: valid UTF-8
    is text."""
    root, _ = repo
    _write(root, "src/sep.ts", f'export const SEP = "\0";\n// {PLANTED}\n')
    out = _scan(repo, "src/sep.ts")
    assert out.returncode == 1, (out.stdout, out.stderr)
    assert "src/sep.ts:2" in out.stderr


def test_a_symlink_is_read_as_the_path_it_holds(repo, tmp_path):
    """Git commits a symlink's target PATH, so that is the text scanned. The file
    it points at is not in the commit and is never opened."""
    root, _ = repo
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text(f"met {PLANTED}\n", encoding="utf-8")
    (root / "points-elsewhere.md").symlink_to(elsewhere)
    assert _scan(repo, "points-elsewhere.md").returncode == 0

    named = tmp_path / f"{PLANTED}.md"
    named.write_text("nothing here\n", encoding="utf-8")
    (root / "points-at-a-name.md").symlink_to(named)
    out = _scan(repo, "points-at-a-name.md")
    assert out.returncode == 1
    assert "points-at-a-name.md:" in out.stderr


# -------------------------------------- a named path that cannot be read

def _over_the_cap(root):
    from scripts import scan_repo_for_owner_data as scanner

    path = root / "dump.jsonl"
    with open(path, "wb") as fh:
        # Text for longer than the binary test looks, then sparse to one byte over.
        fh.write(b'{"k": "v"}\n' * 1000)
        fh.truncate(scanner.MAX_SCAN_BYTES + 1)
    return "dump.jsonl"


REFUSALS = {
    "binary": (lambda root: _write(root, "logo.png", PNG_BYTES).name, "binary"),
    "missing": (lambda root: "gone.sh", "no such file"),
    "directory": (lambda root: (root / "adir").mkdir() or "adir", "not a regular file"),
    "skipped directory": (lambda root: str(_write(root, "node_modules/pkg/index.js",
                                                  f"// {PLANTED}\n").relative_to(root)),
                          "node_modules/"),
    "over the cap": (_over_the_cap, "cap"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_named_path_that_cannot_be_read_is_refused_not_passed(repo, case):
    """Before this change every one of these exited 0 and printed "clean"."""
    root, _ = repo
    make, why = REFUSALS[case]
    rel = make(root)
    out = _scan(repo, rel)
    assert out.returncode == 2, (out.returncode, out.stdout, out.stderr)
    assert "clean" not in out.stdout
    refusal = [ln for ln in out.stderr.splitlines() if ln.strip().startswith(f"{rel}:")]
    assert refusal, out.stderr
    assert why in refusal[0]


def test_a_refusal_does_not_hide_a_leak_in_the_other_files(repo):
    root, _ = repo
    _write(root, "logo.png", PNG_BYTES)
    _write(root, "scripts/deploy.sh", PLANTS["scripts/deploy.sh"])
    out = _scan(repo, "logo.png", "scripts/deploy.sh")
    assert out.returncode == 1
    assert "scripts/deploy.sh:" in out.stderr
    assert "logo.png: binary" in out.stderr


# ---------------------------------------- what --all skips, it says out loud

def test_the_pre_push_scan_passes_binary_files_and_counts_them(repo):
    """Refusing a tracked image would block every push, so --all skips binary
    files. It says how many; a skip nobody reports is how this bug lived."""
    root, _ = repo
    _write(root, "logo.png", PNG_BYTES)
    _write(root, "notes.md", "nothing here\n")
    _git("add", "-A", cwd=root)
    out = _scan(repo, "--all")
    assert out.returncode == 0, out.stderr
    assert "clean — 1 files checked" in out.stdout
    assert "not scanned: 1 binary" in out.stdout


def test_the_pre_push_scan_names_a_tracked_file_missing_from_disk(repo):
    """A push sends the COMMITTED copy; only the working copy is gone. Nothing
    read it, so it is named rather than counted."""
    root, _ = repo
    _write(root, "scripts/deploy.sh", PLANTS["scripts/deploy.sh"])
    _git("add", "-A", cwd=root)
    (root / "scripts" / "deploy.sh").unlink()
    out = _scan(repo, "--all")
    assert "scripts/deploy.sh: no such file" in out.stdout
    assert "NOT checked for leaks" in out.stdout


# ------------------------------------------------- text that is not UTF-8

#: Invented as well, with an accent: the shape a Latin-1 export changes.
ACCENTED = "Renée Quorvax"


@pytest.mark.parametrize("how", ["--all", "named"])
def test_a_latin1_export_is_read_and_an_accented_name_still_matches(repo, how):
    """Text that is not UTF-8 is still text. The reader this replaced dropped the
    bytes it could not decode, so an accented name lost its accent and passed."""
    root, terms = repo
    with open(terms, "a", encoding="utf-8") as fh:
        fh.write(f"{ACCENTED}\n")
    _write(root, "export.csv", f"id,name\n1,{ACCENTED}\n".encode("latin-1"))
    _git("add", "-A", cwd=root)
    out = _scan(repo, "--all") if how == "--all" else _scan(repo, "export.csv")
    assert out.returncode == 1, out.stdout
    assert "export.csv:2" in out.stderr


def test_the_summary_names_a_file_read_on_a_guessed_encoding(repo):
    root, _ = repo
    _write(root, "export.csv", "id,place\n1,caf\xe9\n".encode("latin-1"))
    _git("add", "-A", cwd=root)
    out = _scan(repo, "--all")
    assert out.returncode == 0, out.stderr
    assert "clean — 1 files checked" in out.stdout
    assert "export.csv: not UTF-8" in out.stdout
