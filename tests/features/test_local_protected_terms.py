"""Protected terms you type by hand, kept off the machine's only shared surface.

The database can only name people the node has already seen. It knows nothing
about a landlord, a doctor, a child's school, or a relative never messaged from
this machine — and those are exactly the names someone pastes into a docstring
while debugging, because they are on their mind and not in any table.

So there is a file for them, and it lives OUTSIDE the working tree. That is the
design, not a detail: a list of the names you are hiding is the worst possible
thing to commit, and `.gitignore` is one `git add -f`, one `git add -A` from
another directory, or one contributor deleting the line away from failing. A
path that is not in the tree cannot be added to it, so the scanner refuses a
terms file inside the repo rather than trusting the ignore rule.

Hand-written terms deliberately bypass the length floor and the GENERIC list.
Those exist to stop DATABASE-derived names flooding the hook; a term someone
typed on purpose is already a deliberate choice, and second-guessing it would
silently drop exactly the short name they went out of their way to protect.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

SCANNER = os.path.join("scripts", "scan_repo_for_owner_data.py")


def _run(*args):
    return subprocess.run([sys.executable, SCANNER, *args], capture_output=True, text=True)


@pytest.fixture()
def terms(tmp_path):
    p = tmp_path / "private-terms.txt"
    p.write_text(
        "# a comment\n"
        "\n"
        "Quilliam Thornbury\n"
        "Ada\n"
        "  Sunnyside Primary  \n",
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture()
def probe(tmp_path):
    def _write(text):
        f = tmp_path / "probe.py"
        f.write_text(text, encoding="utf-8")
        return str(f)
    return _write


def test_a_local_term_is_caught(terms, probe):
    r = _run("--local-terms", terms, probe("met Quilliam Thornbury today"))
    assert r.returncode == 1
    assert "Quilliam Thornbury" in r.stderr


def test_a_short_local_term_is_honoured(terms, probe):
    """Three characters. The floor protects the hook from the DATABASE, not
    from its owner — dropping a hand-written term would silently fail the
    person who deliberately added it."""
    r = _run("--local-terms", terms, probe('name = "Ada"'))
    assert r.returncode == 1


def test_whitespace_and_comments_are_ignored(terms, probe):
    r = _run("--local-terms", terms, probe("enrolled at Sunnyside Primary"))
    assert r.returncode == 1
    assert "Sunnyside Primary" in r.stderr
    clean = _run("--local-terms", terms, probe("# a comment"))
    assert clean.returncode == 0, "a comment line must not become a protected term"


def test_clean_content_passes(terms, probe):
    assert _run("--local-terms", terms, probe("nothing sensitive here")).returncode == 0


def test_a_terms_file_inside_the_repo_is_refused(tmp_path):
    """The guard on the guard.

    Putting the list in the tree "just for now" and gitignoring it is the
    obvious thing to do and the one thing that must not work.
    """
    inside = os.path.join(os.getcwd(), "_tmp_terms_probe.txt")
    try:
        with open(inside, "w", encoding="utf-8") as fh:
            fh.write("Quilliam Thornbury\n")
        r = _run("--local-terms", inside)
        assert r.returncode == 1
        assert "refusing" in r.stderr.lower()
    finally:
        if os.path.exists(inside):
            os.remove(inside)


def test_a_missing_terms_file_is_not_an_error(probe):
    """Contributors without a node have nothing of the owner's to leak, and a
    hook that always fails gets deleted."""
    r = _run("--local-terms", "/nonexistent/terms.txt", "--database", "/nonexistent.db")
    assert r.returncode == 0


def test_the_summary_says_whether_a_local_list_was_loaded(terms, probe):
    """Silence about an inactive guard is how a guard stays inactive."""
    with_list = _run("--local-terms", terms, probe("nothing here"))
    assert "private-terms.txt" in with_list.stdout
    without = _run("--local-terms", "/nonexistent/terms.txt", probe("nothing here"))
    assert "no local terms file" in without.stdout


def test_local_terms_apply_to_commit_messages_too(terms, tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("fix: spoke to Quilliam Thornbury about it\n", encoding="utf-8")
    r = _run("--local-terms", terms, "--message-file", str(msg))
    assert r.returncode == 1
