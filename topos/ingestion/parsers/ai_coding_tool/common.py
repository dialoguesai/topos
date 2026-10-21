"""Shared helpers for the AI coding-tool transcript readers (CONTRACTS §2.2–2.4, §2.7, §2.7b).

Three readers (Claude Code, Codex, Cursor) emit the same flat record and share
the same three policies, so the policies live here as data rather than code:

* :data:`SECRET_SHAPE_RULES` / :data:`PATH_RULES` — the §2.4 scrub, one row per
  rule ``(name, regex, replacement)``. A spike result that moves a boundary
  changes one line and one count.
* :data:`CLAUDE_CODE_KEYBOARD_ORIGIN` — the §2.3 keyboard-origin rule as
  measured by spike A (P0.12): the fields of a ``type=user`` record that must
  hold for its text to be the owner's own typing.
* :data:`HARNESS_TAGS` — the leading tags that mark text the harness injected
  into a user record (slash commands, notifications, IDE context).

Pure stdlib, no engine imports, no I/O: the readers, the parser classes and the
report scripts all call it, like ``chatgpt_export`` before it.
"""

from __future__ import annotations

import getpass
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..chatgpt_export import DropLedger

TOOLS = ("claude_code", "codex", "cursor")
ROLES = ("user", "assistant", "system")

# ---------------------------------------------------------------------------
# Drop reasons (§2.3). Stable strings: the receipt and the UI key off them.
# ---------------------------------------------------------------------------

DROP_THINKING = "thinking"
DROP_TOOL_USE = "tool_use"
DROP_TOOL_RESULT = "tool_result"
DROP_HARNESS_INJECTED = "harness_injected"
DROP_SYSTEM = "system"
DROP_SUBAGENT = "subagent"
DROP_NON_KEYBOARD_ORIGIN = "non_keyboard_origin"
DROP_EMPTY = "empty"
DROP_SKIPPED_FOLDER = "skipped_folder"
DROP_SIDE_FILE = "side_file"
DROP_FORK_DUPLICATE = "fork_duplicate"
# A cursor exists but the file does not: the cursor stays where it was.
DROP_FILE_GONE = "file_gone"
# Newest record older than ``since_unix``: the session is not part of this backfill.
DROP_OUTSIDE_WINDOW = "outside_window"

# Reasons decided per FILE rather than per record, kept apart in the receipt so
# ``message_nodes == turns_emitted + dropped_nodes_total + merged_blocks +
# meta_records`` stays a checkable invariant.
DROP_OUTSIDE_ROOT = "outside_root"
FILE_DROP_REASONS = frozenset({DROP_SKIPPED_FOLDER, DROP_SIDE_FILE, DROP_FILE_GONE, DROP_OUTSIDE_WINDOW, DROP_OUTSIDE_ROOT})


def non_keyboard_reason(tag: str) -> str:
    """``non_keyboard_origin:<tag>`` — the reason string for one origin violation."""
    return f"{DROP_NON_KEYBOARD_ORIGIN}:{tag}"


# ---------------------------------------------------------------------------
# §2.4 scrub table
# ---------------------------------------------------------------------------

SECRET_PLACEHOLDER = "[SECRET]"
TOPOS_SECRET_PLACEHOLDER = "[TOPOS_SECRET]"
HOST_PLACEHOLDER = "[HOST]"
HOME_PLACEHOLDER = "~"
# The one rule name for exact values of the node's own secrets (§2.4 step 1).
EXACT_SECRET_RULE = "topos_secret"
# Rule names for the machine's own hostname and login name (§2.4 step 3, last
# clause; spike B's EXACT_IDENT): both are exact values known only at run time.
MACHINE_HOSTNAME_RULE = "hostname_machine"
USER_PROMPT_RULE = "user_prompt"
# An exact secret shorter than this is not scrubbed: it would hit prose
# (spike B floor; every key the node mints is 32+).
MIN_EXACT_SECRET_LEN = 16


@dataclass(frozen=True)
class ScrubRule:
    name: str
    pattern: "re.Pattern[str]"
    replacement: str


def _rule(name: str, regex: str, replacement: str) -> ScrubRule:
    return ScrubRule(name, re.compile(regex), replacement)


# Step 2 of §2.4 in application order, aligned with spike B (P0.2,
# ``p02_prescrub_rules.py``, 92 synthetic cases, 0 misses): where the spike's
# rule scrubs MORE than the contract's text it is used; where the contract is
# the stricter one it stays; shapes the contract lacks are added. Names follow
# §2.4 where it has one. Vendor shapes run before the contextual rules
# (``env_assignment``, ``code_literal``, ``yaml_unquoted``) so a value is
# counted once, under its shape; the contextual rules refuse a value that is
# already a placeholder (``[`` is excluded from their value class).
SECRET_SHAPE_RULES: Tuple[ScrubRule, ...] = (
    _rule(
        "private_key_block",
        r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
        SECRET_PLACEHOLDER,
    ),
    # Topos-own prefixes (spike: any ``tp[nak]_`` + 16, not only the minted 40 hex,
    # so a truncated or hand-typed key still goes). They read as [TOPOS_SECRET]
    # like the exact values: the report is about Topos keys.
    _rule("topos_node_key", r"\btpn_[A-Za-z0-9][A-Za-z0-9._-]{15,}", TOPOS_SECRET_PLACEHOLDER),
    _rule("topos_agent_label", r"\btpa_[A-Za-z0-9][A-Za-z0-9._-]{15,}", TOPOS_SECRET_PLACEHOLDER),
    _rule("topos_client_token", r"\btpk_[A-Za-z0-9][A-Za-z0-9._-]{15,}", TOPOS_SECRET_PLACEHOLDER),
    # §2.4 writes ``ok_[0-9a-f]+``, the spike ``{32,64}``; a floor of 16 hex keeps
    # ``ok_1`` in prose out of the count and still takes every minted key (48).
    _rule("topos_owner_key", r"\bok_[0-9a-f]{16,}\b", TOPOS_SECRET_PLACEHOLDER),
    _rule("anthropic_key", r"\bsk-ant-[A-Za-z0-9_-]{16,}", SECRET_PLACEHOLDER),
    _rule("openai_key", r"\bsk-[A-Za-z0-9_-]{16,}", SECRET_PLACEHOLDER),
    _rule("stripe_key", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b|\bwhsec_[A-Za-z0-9]{20,}\b", SECRET_PLACEHOLDER),
    _rule("github_token", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b", SECRET_PLACEHOLDER),
    _rule("gitlab_token", r"\bglpat-[A-Za-z0-9_-]{20,}", SECRET_PLACEHOLDER),
    _rule("aws_access_key", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b", SECRET_PLACEHOLDER),
    _rule("aws_secret_key", r"(?i)(aws_secret_access_key\s*[=:]\s*[\"']?)[A-Za-z0-9/+=]{40}\b", r"\1" + SECRET_PLACEHOLDER),
    _rule("slack_token", r"\bxox[abeprs]-[A-Za-z0-9-]{10,}", SECRET_PLACEHOLDER),
    _rule("slack_webhook", r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+", SECRET_PLACEHOLDER),
    _rule(
        "google_api_key",
        r"\bAIza[0-9A-Za-z_-]{35}\b|\bGOCSPX-[A-Za-z0-9_-]{20,}|\bya29\.[A-Za-z0-9_-]{30,}",
        SECRET_PLACEHOLDER,
    ),
    _rule("npm_hf_token", r"\bnpm_[A-Za-z0-9]{36}\b|\bhf_[A-Za-z0-9]{30,}\b", SECRET_PLACEHOLDER),
    _rule("sendgrid_key", r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", SECRET_PLACEHOLDER),
    _rule("digitalocean_token", r"\bdop_v1_[0-9a-f]{64}\b", SECRET_PLACEHOLDER),
    _rule("vault_token", r"\bhvs\.[A-Za-z0-9_-]{20,}", SECRET_PLACEHOLDER),
    _rule("age_secret_key", r"\bAGE-SECRET-KEY-1[A-Z0-9]{50,}", SECRET_PLACEHOLDER),
    _rule("mapbox_token", r"\bpk\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", SECRET_PLACEHOLDER),
    _rule("supabase_key", r"\bsb_(?:secret|publishable)_[A-Za-z0-9_]{20,}", SECRET_PLACEHOLDER),
    _rule("twilio_sid", r"\bSK[0-9a-f]{32}\b", SECRET_PLACEHOLDER),
    # Three base64url segments, header ``eyJ``: covers Supabase anon/service keys.
    _rule("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", SECRET_PLACEHOLDER),
    # Auth headers: the scheme word stays so the sentence still reads; the credential goes.
    _rule(
        "bearer_token",
        r"(?i)((?:Authorization\s*:\s*)?(?:Bearer|Basic|Token)\s+)[A-Za-z0-9._~+/=-]{16,}",
        r"\1" + SECRET_PLACEHOLDER,
    ),
    _rule(
        "api_key_header",
        r"(?i)\b((?:X-API-Key|X-Auth-Token|Api-Key|Private-Token)[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._~+/=-]{16,}",
        r"\1" + SECRET_PLACEHOLDER,
    ),
    # ENV-style lines: the NAME stays, the value goes. Spike names + quotes;
    # the contract's ``\S+`` (any length) on the value, since a short secret is
    # still a secret.
    _rule(
        "env_assignment",
        r"(?m)^(\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIALS?|AUTH)[A-Z0-9_]*\s*=\s*[\"']?)([^\s\"'\[]+)",
        r"\1" + SECRET_PLACEHOLDER,
    ),
    # Code / JSON: a credential-named identifier assigned a STRING LITERAL
    # (``password = "…"``, ``{"token": "…"}``); expressions are left alone.
    _rule(
        "code_literal",
        r"(?i)\b((?:[a-z0-9_.]*(?:api[_-]?key|secret|client[_-]?secret|access[_-]?token|refresh[_-]?token|auth[_-]?token|password|passwd|token)[a-z0-9_]*)[\"']?\s*[=:]\s*[\"'])([^\"'\s\[]{8,})(?=[\"'])",
        r"\1" + SECRET_PLACEHOLDER,
    ),
    # YAML/TOML unquoted ``key: value`` whose key names a credential; the value
    # must be token-like and end the line.
    _rule(
        "yaml_unquoted",
        r"(?im)^(\s*[A-Za-z_-]*(?:key|token|secret|password)[A-Za-z_-]*\s*[:=]\s*)([A-Za-z0-9._~+/=!#%@-]{16,})\s*$",
        r"\1" + SECRET_PLACEHOLDER,
    ),
    # ``scheme://user:PASSWORD@host``
    _rule("url_credential", r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)@", r"\1" + SECRET_PLACEHOLDER + "@"),
)

# Step 3 of §2.4: every form a home path takes. ``home_slug`` covers both the
# Claude Code project folder (``-Users-<n>-dev-x``) and the scratchpad form
# (``/private/tmp/…/-Users-<n>-…``), which embeds the same slug.
PATH_RULES: Tuple[ScrubRule, ...] = (
    _rule("home_path", r"(?<![\w-])/(?:Users|home)/(?!Shared/)[^/\s'\"`:]+", HOME_PLACEHOLDER),
    _rule("home_windows", r"(?i)\b[A-Z]:\\Users\\[^\\\s\"']+", HOME_PLACEHOLDER),
    _rule("home_slug", r"(?<![\w])-(?:Users|home)-[A-Za-z0-9_.]+(?=-|/|\s|$|['\"`])", "-~"),
    _rule("hostname_local", r"\b[\w-]+\.local\b", HOST_PLACEHOLDER),
)

_GENERIC_HOSTNAMES = frozenset({"localhost", "local", "mac", "pc", "host"})
_GENERIC_USERNAMES = frozenset({"root", "user", "admin", "test", "guest"})


def machine_hostnames() -> Tuple[str, ...]:
    """This machine's hostname and its first label, minus names too generic to scrub."""
    try:
        full = socket.gethostname().strip()
    except OSError:
        return ()
    names = []
    for name in (full, full.split(".")[0]):
        if len(name) >= 4 and name.lower() not in _GENERIC_HOSTNAMES and name not in names:
            names.append(name)
    return tuple(names)


def machine_usernames() -> Tuple[str, ...]:
    """The login name this process runs under, for ``user@host`` shell prompts."""
    try:
        name = getpass.getuser().strip()
    except Exception:  # noqa: BLE001 — no login database in some sandboxes
        return ()
    if len(name) >= 3 and name.lower() not in _GENERIC_USERNAMES:
        return (name,)
    return ()


# §2.4 step 1: the node's own secrets. The three ``.env`` keys plus every
# plaintext label file (``mcp_clients`` stores only hashes; the file is the
# only copy). The node runs as the same uid as the agent, so it may read them.
NODE_ENV_SECRET_KEYS: Tuple[str, ...] = ("TOPOS_KEY", "TOPOS_OWNER_KEY", "TOPOS_NODE_KEY")
NODE_AGENTS_DIR = "agents"
NODE_LABEL_SUFFIX = ".token"
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


def _node_topos_home() -> Path:
    """The folder the node serves (``storage.db.paths.active_base``: ``~/.topos`` in production, the fixture's
    base in tests), where its label files live. ``~/.topos`` when the node's paths are not importable."""
    try:
        from ....storage.db.paths import active_base

        return Path(active_base())
    except Exception:  # noqa: BLE001 — a reader used outside the node
        return Path.home() / ".topos"


def _node_env_file(home: Path) -> Path:
    """The env file the node reads its keys from (``owner_key._env_path``, which honours ``TOPOS_ENV_FILE``)."""
    try:
        from ....owner_key import _env_path

        return _env_path()
    except Exception:  # noqa: BLE001
        return home / ".env"


def node_secrets(topos_home: Optional[Path] = None) -> Tuple[str, ...]:
    """Exact values to scrub: ``TOPOS_KEY``/``TOPOS_OWNER_KEY``/``TOPOS_NODE_KEY`` from
    ``<topos_home>/.env`` and the first line of every ``<topos_home>/agents/*.token``.

    ``topos_home`` defaults to ``~/.topos``; tests pass a ``tmp_path`` and never
    the real one. Values are returned only to be scrubbed — never logged, never
    stored. Missing files are simply absent.
    """
    explicit = topos_home is not None
    home = Path(topos_home) if explicit else _node_topos_home()
    env_file = home / ".env" if explicit else _node_env_file(home)
    found: List[str] = []
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        match = _ENV_LINE.match(line)
        if not match or match.group(1) not in NODE_ENV_SECRET_KEYS:
            continue
        value = match.group(2).strip().strip("\"'").strip()
        if value:
            found.append(value)
    agents = home / NODE_AGENTS_DIR
    try:
        label_files = sorted(p for p in agents.iterdir() if p.is_file() and p.name.endswith(NODE_LABEL_SUFFIX))
    except OSError:
        label_files = []
    for path in label_files:
        try:
            first = path.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            continue
        if first and first[0].strip():
            found.append(first[0].strip())
    return tuple(dict.fromkeys(s for s in found if len(s) >= MIN_EXACT_SECRET_LEN))


def scrub_text(
    text: str,
    *,
    secrets: Sequence[str] = (),
    hostnames: Optional[Sequence[str]] = None,
    usernames: Optional[Sequence[str]] = None,
) -> Tuple[str, Dict[str, int]]:
    """Apply §2.4 in order; return ``(scrubbed, {rule_name: count})``.

    ``secrets`` are the node's own exact values (``TOPOS_KEY``,
    ``TOPOS_OWNER_KEY``, every label it holds) — a 32-hex ``TOPOS_KEY`` has no
    shape a regex can find without hitting every hash, so only an exact match
    removes it (security finding 9). ``hostnames`` and ``usernames`` default to
    this machine's; tests pass their own. This is the deterministic pass the
    rows depend on; the model-based privacy filter downstream is a second pass
    whose recall on code text is unverified, never the one relied on.
    """
    counts: Dict[str, int] = {}
    if not text:
        return text, counts

    def bump(name: str, n: int) -> None:
        if n:
            counts[name] = counts.get(name, 0) + n

    # (1) exact values, longest first so a label never survives inside a longer one.
    for secret in sorted({s for s in secrets if s and len(s) >= MIN_EXACT_SECRET_LEN}, key=len, reverse=True):
        n = text.count(secret)
        if n:
            text = text.replace(secret, TOPOS_SECRET_PLACEHOLDER)
            bump(EXACT_SECRET_RULE, n)
    # (1b) the login name in a ``user@host`` prompt, before the host becomes a placeholder.
    users = machine_usernames() if usernames is None else tuple(usernames)
    for name in sorted({u for u in users if u}, key=len, reverse=True):
        text, n = re.subn(r"(?<![\w.-])" + re.escape(name) + r"(?=@\S)", HOME_PLACEHOLDER, text)
        bump(USER_PROMPT_RULE, n)
    names = machine_hostnames() if hostnames is None else tuple(hostnames)
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text, n = re.subn(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", HOST_PLACEHOLDER, text)
        bump(MACHINE_HOSTNAME_RULE, n)
    # (2) shapes, (3) paths and ``.local`` hostnames.
    for rule in SECRET_SHAPE_RULES + PATH_RULES:
        text, n = rule.pattern.subn(rule.replacement, text)
        bump(rule.name, n)
    return text, counts


def project_key_from_cwd(cwd: Any, *, usernames: Optional[Sequence[str]] = None) -> Optional[str]:
    """The §2.6 project facet for a working directory: its last path segment, on either separator, and only
    when the §2.4 path scrub leaves that segment as it was.

    None otherwise: the home folder itself (the scrub turns it into the placeholder, or into a fragment of
    it when the login name has a space), a folder named like a ``.local`` host, a path-slug folder, a drive
    or the root, anything that is not a string, and a segment equal to a login name (a home under a root the
    scrub does not know, such as ``/mnt/c/Users/x``). ``usernames`` default to this machine's.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    raw = cwd.strip()
    scrubbed, _ = scrub_text(raw, hostnames=(), usernames=())
    raw_segments = _path_segments(raw)
    segments = _path_segments(scrubbed)
    if not raw_segments or not segments:
        return None
    name = segments[-1]
    if name != raw_segments[-1] or name == HOME_PLACEHOLDER or name.endswith(":"):
        return None
    users = machine_usernames() if usernames is None else tuple(usernames)
    if any(user and name.casefold() == user.casefold() for user in users):
        return None
    return name


def _path_segments(path: str) -> List[str]:
    return [part for part in re.split(r"[\\/]+", path) if part]


# ---------------------------------------------------------------------------
# §2.3 harness text
# ---------------------------------------------------------------------------

# Leading tags that mark harness-injected user text (spike A). ``ide_`` is a
# prefix: ``<ide_opened_file>``, ``<ide_selection>``. A record whose text
# starts with one is not something the person typed, whatever its flags say.
HARNESS_TAGS: Tuple[str, ...] = (
    "task-notification",
    "command-name",
    "command-message",
    "local-command-stdout",
    "local-command-caveat",
    "bash-input",
    "ci-monitor-event",
    "artifact-view-context",
    "create-pr-command",
    "cross-session-message",
    "system-reminder",
    "ide_",
)
# Written by the harness when the person stops a turn; never typed.
INTERRUPT_MARKER = "[Request interrupted by user"
INTERRUPT_TAG = "interrupt_marker"
SYSTEM_REMINDER_TAG = "system-reminder"

_LEADING_REMINDERS = re.compile(r"^\s*(?:<system-reminder>.*?</system-reminder>\s*)+", re.DOTALL)
_LEADING_REMINDER_ONE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
# One complete harness span that starts a line and ends the text: ``<tag …>…</tag>`` with no other
# opening or closing of the same tag inside it, so a span never swallows typed text between two spans.
_TRAILING_HARNESS_SPAN = re.compile(
    r"(?:\A|\n)[ \t]*<(?P<tag>"
    + "|".join(re.escape(tag) + (r"[A-Za-z0-9_-]*" if tag.endswith("_") else "") for tag in HARNESS_TAGS)
    + r")(?:\s[^>]*)?>(?:(?!</?(?P=tag)[\s>]).)*</(?P=tag)>\s*\Z",
    re.DOTALL,
)


def strip_leading_system_reminders(text: str) -> Tuple[str, int]:
    """Remove complete ``<system-reminder>…</system-reminder>`` spans at the start.

    Spike A: 135 genuinely typed records start with one and keep their text
    after it, so the span is stripped and the rest classified, not dropped.
    Returns ``(remaining, spans_removed)``; ``remaining`` is stripped of
    surrounding whitespace.
    """
    match = _LEADING_REMINDERS.match(text or "")
    if not match:
        return (text or "").strip(), 0
    removed = len(_LEADING_REMINDER_ONE.findall(match.group(0)))
    return text[match.end():].strip(), removed


def strip_trailing_harness_spans(text: str) -> Tuple[str, int]:
    """Remove complete harness spans that start a line and run to the end of the text.

    Measured 20 Oct (live node, counts only): the 10 typed rows that still held
    a harness tag were all typed text, a newline, then one whole
    ``<system-reminder>`` span to the end. A span with typed text after it, or
    a tag named inline in a sentence, is the person's own words and stays.
    Returns ``(remaining, spans_removed)``; ``remaining`` is stripped.
    """
    remaining = (text or "").strip()
    removed = 0
    while True:
        match = _TRAILING_HARNESS_SPAN.search(remaining)
        if match is None:
            return remaining, removed
        remaining = remaining[:match.start()].rstrip()
        removed += 1


def harness_tag(text: str) -> Optional[str]:
    """The harness tag ``text`` starts with, or None when it starts with prose."""
    stripped = (text or "").lstrip()
    if stripped.startswith(INTERRUPT_MARKER):
        return INTERRUPT_TAG
    if not stripped.startswith("<"):
        return None
    body = stripped[1:]
    for tag in HARNESS_TAGS:
        if body.startswith(tag):
            return tag
    return None


def classify_user_text(text: str) -> Tuple[str, Optional[str], int]:
    """``(kept_text, harness_tag_or_None, reminder_spans_stripped)`` for a user record's text.

    A tag means the record is harness-injected; a bare system-reminder (nothing
    left once the span is gone) reports ``system-reminder``. Typed text keeps
    nothing of the complete harness spans that trail it; the count of spans
    removed covers both ends.
    """
    remaining, stripped = strip_leading_system_reminders(text)
    if stripped and not remaining:
        return "", SYSTEM_REMINDER_TAG, stripped
    tag = harness_tag(remaining)
    if tag is not None:
        return remaining, tag, stripped
    kept, trailing = strip_trailing_harness_spans(remaining)
    return kept, None, stripped + trailing


def is_harness_text(text: str) -> bool:
    """True when the text, past any leading reminder spans, starts with a harness tag."""
    return classify_user_text(text)[1] is not None


# ---------------------------------------------------------------------------
# §2.3 keyboard-origin rule (spike A, P0.12)
# ---------------------------------------------------------------------------


class _Absent:
    """The key is missing from the record (older builds lack ``origin``/``turnOrigin``)."""

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "ABSENT"


ABSENT = _Absent()


@dataclass(frozen=True)
class OriginRule:
    """One row of the rule: ``field`` (dotted for nested keys) must satisfy ``allowed``/``denied``."""

    field: str
    # Value must be one of these; ``ABSENT`` means the key may be missing. None = any.
    allowed: Optional[Tuple[Any, ...]] = None
    # Value must not be one of these.
    denied: Tuple[Any, ...] = ()


# A ``type=user`` record is keyboard-typed only when every row holds, in this
# order (the first failing row names the drop). Measured on real stores
# 2026-10-15, builds 2.1.170→2.1.295: ``origin`` (≥2.1.197) and ``turnOrigin``
# (≥2.1.280) may be absent on older records; ``userType`` is always "external"
# and ``promptSource`` is "sdk" for everything typed in the desktop app, so
# neither separates typed from injected and neither is a row here. Peer
# messages are plain text with no tag and are always ``isMeta``.
CLAUDE_CODE_KEYBOARD_ORIGIN: Tuple[OriginRule, ...] = (
    OriginRule("isMeta", allowed=(ABSENT, None, False)),
    OriginRule("isCompactSummary", allowed=(ABSENT, None, False)),
    OriginRule("isVisibleInTranscriptOnly", allowed=(ABSENT, None, False)),
    OriginRule("isSidechain", allowed=(ABSENT, None, False)),
    OriginRule("origin.kind", allowed=(ABSENT, None, "human")),
    OriginRule("turnOrigin", allowed=(ABSENT, None, "human")),
    OriginRule("promptSource", denied=("system",)),
)

# A ``queued_command`` attachment (a prompt delivered into a running turn) is the person's own only when every
# row holds, read off the attachment. Measured on the owner's store 2026-10-20 (counts only, 6,854 entries):
# task notifications carry ``commandMode=task-notification``; peer messages carry ``origin.kind=peer`` and
# ``isMeta``; the 9 prompt-mode entries with no ``origin`` were all harness events. So ``origin.kind`` must be
# present and human here, unlike the typed-record rule where older builds lack it.
CLAUDE_CODE_QUEUED_PROMPT_ORIGIN: Tuple[OriginRule, ...] = (
    OriginRule("commandMode", allowed=("prompt",)),
    OriginRule("origin.kind", allowed=("human",)),
    OriginRule("isMeta", allowed=(ABSENT, None, False)),
)


def _lookup(record: Mapping[str, Any], dotted: str) -> Any:
    value: Any = record
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return ABSENT
        value = value[part]
    return value


def origin_violation(record: Mapping[str, Any], rules: Sequence[OriginRule] = CLAUDE_CODE_KEYBOARD_ORIGIN) -> Optional[str]:
    """The tag of the first rule the record breaks, or None when it is keyboard-typed.

    Flag rows report the field name (``isMeta``); value rows report
    ``field=value`` (``origin.kind=peer``), which is what the ledger counts by.
    """
    for rule in rules:
        value = _lookup(record, rule.field)
        broken = False
        if rule.allowed is not None and not any(value is a or value == a for a in rule.allowed):
            broken = True
        if rule.denied and any(value is d or value == d for d in rule.denied):
            broken = True
        if not broken:
            continue
        if isinstance(value, bool) or value is ABSENT:
            return rule.field
        return f"{rule.field}={value}"
    return None


# ---------------------------------------------------------------------------
# §2.7 taint
# ---------------------------------------------------------------------------

# A tool call whose server or tool name matches, or a shell command that
# mentions one of these, touches a Topos surface. ``mcp__plugin_topos`` is
# already covered by ``topos`` and listed for the reader of the contract.
_TAINT_NAME = re.compile(r"topos|mcp__plugin_topos|remote__", re.IGNORECASE)
TAINT_COMMAND_MARKERS: Tuple[str, ...] = ("~/.topos", ".topos/", "/.topos", "engine.sock", "topos-node")


def taints(tool_or_server_name: Optional[str], command_text: Optional[str]) -> bool:
    """True when this tool call or shell command touches a Topos surface (§2.7)."""
    if tool_or_server_name and _TAINT_NAME.search(tool_or_server_name):
        return True
    if command_text:
        return any(marker in command_text for marker in TAINT_COMMAND_MARKERS)
    return False


# ---------------------------------------------------------------------------
# §2.2 record
# ---------------------------------------------------------------------------


def iso_millis(stamp: datetime) -> str:
    """ISO 8601, UTC, exactly three fractional digits, ``Z`` suffix: ``2026-10-01T09:00:00.250Z``
    (the form the Codex and Cursor readers and CONTRACTS §2.2 use; settled at integration, 16 Oct)."""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def make_record(
    *,
    tool: str,
    record_uuid: str,
    session_id: str,
    role: str,
    content: str,
    created_at: datetime,
    turn_index: int,
    title: str,
    origin: str,
    share_excluded: bool,
    timestamp_source: str,
    model_slug: Optional[str] = None,
) -> Dict[str, Any]:
    """The §2.2 flat record. Raises ``ValueError("empty")`` on blank content.

    ``content`` is expected scrubbed already; this only trims it. No cwd, path,
    branch or hostname has a field here, and none is accepted.
    """
    if tool not in TOOLS:
        raise ValueError(f"unknown tool {tool!r}")
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    if timestamp_source not in ("record", "session", "file_mtime"):
        raise ValueError(f"unknown timestamp_source {timestamp_source!r}")
    text = (content or "").strip()
    if not text:
        raise ValueError("empty")
    metadata: Dict[str, Any] = {
        "tool": tool,
        "session_id": session_id,
        "turn_index": int(turn_index),
        "conversation_title": title,
        "origin": origin,
        "share_excluded": bool(share_excluded),
        "timestamp_source": timestamp_source,
    }
    if model_slug:
        metadata["model_slug"] = model_slug
    return {
        "id": f"{tool}:{record_uuid}",
        "thread_id": session_id,
        "role": role,
        "content": text,
        "created_at": iso_millis(created_at),
        "_metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class Ledger(DropLedger):
    """``chatgpt_export.DropLedger`` plus what a transcript reader also has to say.

    ``conversations_seen``/``conversations_kept`` count session files;
    ``message_nodes`` counts records read. ``secret_sightings`` is the "key seen
    in N sessions — rotate" report: exact-secret scrubs per session.
    """

    scrub_counts: Dict[str, int] = field(default_factory=dict)
    tainted_turns: int = 0
    # Drop counts by origin tag (security finding 8): harness tags and origin-rule tags.
    origins: Dict[str, int] = field(default_factory=dict)
    secret_sightings: Dict[str, int] = field(default_factory=dict)
    # Assistant records folded into an earlier record of the same message.id.
    merged_blocks: int = 0
    # Records that are not turns and not drops: titles, summaries, snapshots.
    meta_records: int = 0
    harness_spans_stripped: int = 0
    subagent_files_scanned: int = 0

    def drop(self, reason: str, count: int = 1, *, origin: Optional[str] = None) -> None:
        super().drop(reason, count)
        if origin:
            self.origins[origin] = self.origins.get(origin, 0) + count

    def scrubbed(self, name: str, n: int = 1) -> None:
        if n:
            self.scrub_counts[name] = self.scrub_counts.get(name, 0) + n

    def record_scrubs(self, counts: Mapping[str, int], *, session_id: Optional[str] = None) -> None:
        """Fold one ``scrub_text`` result in; exact-secret hits also count for the session."""
        for name, n in counts.items():
            self.scrubbed(name, n)
        if session_id and counts.get(EXACT_SECRET_RULE):
            self.note_secret_sightings(session_id, counts[EXACT_SECRET_RULE])

    def tainted(self, n: int = 1) -> None:
        self.tainted_turns += n

    def note_secret_sightings(self, session_id: str, n: int = 1) -> None:
        self.secret_sightings[session_id] = self.secret_sightings.get(session_id, 0) + n

    def as_dict(self) -> Dict[str, Any]:
        node_drops = {k: v for k, v in self.dropped.items() if k not in FILE_DROP_REASONS}
        file_drops = {k: v for k, v in self.dropped.items() if k in FILE_DROP_REASONS}
        return {
            "conversations_seen": self.conversations_seen,
            "conversations_kept": self.conversations_kept,
            "message_nodes": self.message_nodes,
            "turns_emitted": self.turns_emitted,
            "dropped": dict(sorted(self.dropped.items())),
            "dropped_nodes": dict(sorted(node_drops.items())),
            "dropped_files": dict(sorted(file_drops.items())),
            "dropped_nodes_total": sum(node_drops.values()),
            "dropped_files_total": sum(file_drops.values()),
            "merged_blocks": self.merged_blocks,
            "meta_records": self.meta_records,
            "origins": dict(sorted(self.origins.items())),
            "scrubbed": dict(sorted(self.scrub_counts.items())),
            "tainted_turns": self.tainted_turns,
            "harness_spans_stripped": self.harness_spans_stripped,
            "subagent_files_scanned": self.subagent_files_scanned,
            "secret_sightings": dict(sorted(self.secret_sightings.items())),
            "sessions_with_secrets": len(self.secret_sightings),
        }


def content_blocks(content: Any) -> Iterable[Dict[str, Any]]:
    """Normalise a message ``content`` (str or block list) to a list of blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []
