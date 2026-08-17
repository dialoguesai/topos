"""`topos-node setup-models` — get a local model onto this machine, from the terminal.

There are three install paths (macOS app, Windows, terminal) and the terminal
one stopped at a running node. Its instructions are three lines — install the
tool, set the key, run it — and none of them mention Ollama, which is a hard
dependency for local chat, topics, briefs and LLM facts. So a terminal owner
finished "setup" with no model and had to go find the web card, which offered a
macOS-only one-click button and a Homebrew command regardless of their OS.

This closes that: reachability, install instructions for THIS platform, and the
starter pull, without leaving the shell.

What it deliberately does NOT do is create the model pack. It does not need to.
The control plane seeds the local preset family from what the machine actually
has (`routes/model_packs.py::_local_preset_lead`), so once a model exists here
the next pack read leads with it. Reaching for the seed route from the CLI would
also mean holding a Keycloak user token, which a node never has — it carries an
engine key.

Installing is never done behind the owner's back: on a platform we can drive,
the pull is offered and the size named, and `--yes` is the only way past the
prompt. Elsewhere the command prints exactly what to run.
"""

from __future__ import annotations

import sys
from typing import Optional

import click

#: The curated starter, matching the web quick-start's only entry
#: (LOCAL_MODEL_STARTERS in LocalModelSetupCard.tsx). Kept identical on purpose:
#: two "recommended first model" answers that disagree is worse than either.
STARTER_TAG = "llama3.2:latest"
STARTER_SIZE = "2.0 GB"
#: The same figure in bytes, for the disk preflight. Approximate on purpose —
#: it is a floor to refuse against, not an accounting of the manifest.
STARTER_SIZE_BYTES = 2_000_000_000



#: Mirrors `control_plane/model_pack_capabilities.py` — BOTH of its rules, not
#: just the easy one. An Ollama library routinely holds embedding and reranking
#: models next to chat ones (topos pulls them itself for enrichment), and a
#: library holding only those is not a library with a chat model in it.
#:
#: The families matter as much as the markers: `bge-m3` says nothing about
#: embedding in its name and would otherwise read as a chat model, which here
#: means telling the owner they are set up when their next message has nothing
#: to answer it. Duplicated rather than imported because the node does not ship
#: the control plane; change one, change the other.
_NON_GENERATIVE_MARKERS = ("embed", "rerank")
_NON_GENERATIVE_FAMILIES = (
    "all-minilm",
    "bge-",
    "granite-embedding",
    "paraphrase-multilingual",
)


def _can_generate(tag: str) -> bool:
    # Provider prefix stripped FIRST, mirroring CP's `normalized_model_name`.
    # Without it the two copies disagree on namespaced tags: CP reads
    # `hf.co/BAAI/bge-m3:latest` as an embedding model, the node read it as a
    # chat model and told the owner they were set up on a library that cannot
    # answer a single turn.
    lowered = str(tag or "").strip().lower()
    if "/" in lowered:
        lowered = lowered.rsplit("/", 1)[-1]
    if not lowered:
        return False
    if any(marker in lowered for marker in _NON_GENERATIVE_MARKERS):
        return False
    return not lowered.startswith(_NON_GENERATIVE_FAMILIES)


def _echo_install_instructions(guidance: dict) -> None:
    click.echo("")
    click.secho(f"  Ollama is not running on this machine ({guidance['label']}).", fg="yellow")
    click.echo("")
    commands = guidance["commands"]
    for command in commands:
        click.secho(f"      {command}", bold=True)
    if commands:
        click.echo("")
    # Printed on every platform. The old branching skipped it exactly where it
    # was the only guidance there was — an unrecognised OS has no command, and
    # its note is "Download the build for your platform."
    if guidance["note"]:
        click.echo(f"  {guidance['note']}")
    click.echo(f"      {guidance['download_url']}")
    click.echo("")
    click.echo("  Then run `topos-node setup-models` again.")
    click.echo("")


@click.command("setup-models")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Do not prompt before downloading the starter model.",
)
@click.option(
    "--model",
    default=None,
    metavar="TAG",
    help=f"Pull this tag instead of the starter ({STARTER_TAG}).",
)
def setup_models_command(yes: bool, model: Optional[str]) -> None:
    """Check Ollama, install guidance, and pull a local model for this node."""
    # `main` returns at `if ctx.invoked_subcommand is not None` BEFORE it loads
    # these, so a subcommand that does not load them itself probes
    # localhost:11434 and tells an owner with ENGINE_OLLAMA_BASE_URL set that
    # the Ollama they are running is not running. `reprocess_cmd` does the same.
    from topos.cli.commands import LEGACY_ENV_PATH, USER_ENV_PATH, _load_env_file

    _load_env_file(USER_ENV_PATH)
    _load_env_file(LEGACY_ENV_PATH)

    from topos.engine.ollama_setup_guidance import install_guidance

    guidance = install_guidance()
    click.echo("")
    click.secho("Topos — local model setup", bold=True)

    try:
        from topos.engine.backends.ollama import OllamaAdapter

        adapter = OllamaAdapter()
        reachable = bool(adapter.is_reachable())
    except Exception as exc:  # noqa: BLE001 — a probe that raised is not a running Ollama
        click.secho(f"  Could not probe Ollama: {exc}", fg="yellow")
        reachable = False

    if not reachable:
        _echo_install_instructions(guidance)
        sys.exit(1)

    try:
        installed = _list_tags_or_raise(_ollama_base_url())
    except Exception as exc:  # noqa: BLE001
        # A failed list must NOT read as an empty library. `adapter.list_models`
        # swallows every error into `[]` (backends/ollama.py), so a timed-out
        # /api/tags on a machine holding twenty models would land in the branch
        # below and offer to download two more gigabytes.
        click.secho(f"  Ollama is running but would not list its models: {exc}", fg="red")
        click.echo("  Nothing was downloaded. Try again in a moment.")
        sys.exit(1)

    generative = sorted(tag for tag in installed if _can_generate(tag))

    if generative:
        click.secho(f"  Ollama is running with {len(generative)} model(s):", fg="green")
        for tag in generative:
            click.echo(f"      {tag}")
        click.echo("")
        click.echo("  Your Topos will lead its local model pack with one of these.")
        click.echo("")
        return

    wanted = str(model or STARTER_TAG).strip()
    size_note = f" ({STARTER_SIZE})" if wanted == STARTER_TAG else ""
    click.echo("")
    click.echo("  Ollama is running, but no chat model is installed.")
    click.echo("")
    if not yes:
        # Never a silent multi-GB pull — the size is named before the download,
        # exactly as the web quick-start's CTA does.
        if not click.confirm(f"  Download {wanted}{size_note} now?", default=True):
            click.echo("")
            click.echo(f"  Nothing downloaded. When you are ready:  ollama pull {wanted}")
            click.echo("")
            return

    # Refuse before the transfer, not at 97%. Only the starter's size is known
    # here (the CLI shells out to `ollama pull`, so it never sees the stream's
    # own total) — an arbitrary --model is checked by the node's pull job when
    # it is downloaded that way instead.
    if wanted == STARTER_TAG:
        from topos.engine.disk_space import check_space_for

        verdict = check_space_for(STARTER_SIZE_BYTES, base_url=_ollama_base_url())
        if verdict is not None:
            click.echo("")
            click.secho(f"  {verdict.message(wanted)}", fg="red")
            click.echo("")
            click.echo("  Free up some space and run `topos-node setup-models` again.")
            click.echo("")
            sys.exit(1)

    click.echo("")
    click.echo(f"  Pulling {wanted}{size_note} — this can take a few minutes…")
    from topos.engine.backends.ollama import OllamaPullFailed, PullAborted

    try:
        _pull_with_progress(wanted)
    except PullAborted as exc:
        # The stream reported a size that will not fit. Stopped seconds in
        # rather than at 97%, and before the write that would fill the volume
        # the node's database is on.
        click.echo("")
        click.secho(f"  {exc}", fg="red")
        click.echo("")
        click.echo("  Free up some space and run `topos-node setup-models` again.")
        click.echo("")
        sys.exit(1)
    except OllamaPullFailed as exc:
        # Ollama answers 200 and reports failure inside the stream, so this is
        # the only place a bad tag or a registry outage surfaces.
        click.echo("")
        click.secho(f"  Ollama could not download {wanted}: {exc}", fg="red")
        click.echo("")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — a dead socket is not a finished pull
        click.echo("")
        click.secho(f"  Download failed: {exc}", fg="red")
        click.echo("")
        sys.exit(1)

    click.echo("")
    click.secho(f"  {wanted} is installed.", fg="green")
    click.echo("  Your Topos will lead its local model pack with it.")
    click.echo("")


def _ollama_base_url() -> str:
    """Where this node talks to Ollama — the same setting the adapter reads."""
    from topos.config.settings import settings

    return str(
        getattr(settings, "engine_ollama_base_url", None) or "http://localhost:11434"
    ).rstrip("/")


def _list_tags_or_raise(base_url: str) -> list:
    """Installed tags, letting a failed request RAISE instead of reading empty.

    `OllamaAdapter.list_models()` wraps its request in `except Exception: return
    []`, which is right for callers that treat "no models" and "could not ask"
    the same way. This command must not: the difference decides whether it
    offers the owner a multi-gigabyte download.

    Takes the base URL rather than the adapter so it depends on the public
    setting, not on the adapter's private `_base_url`.
    """
    import httpx

    response = httpx.get(f"{base_url}/api/tags", timeout=10.0)
    response.raise_for_status()
    models = response.json().get("models") or []
    return [str(m.get("name") or "").strip() for m in models if (m or {}).get("name")]


def _pull_with_progress(tag: str) -> int:
    """Download `tag` over the streaming API, rendering progress as it goes.

    Deliberately NOT `subprocess.Popen(["ollama", "pull", ...])`, which is what
    this did first. Shelling out bought Ollama's own progress meter and cost
    three things:

      * it pulled through the local `ollama` binary while reachability had been
        probed against `engine_ollama_base_url` — so a node pointed at a remote
        Ollama downloaded into the wrong daemon, or failed for want of a binary
        it does not need;
      * the stream's `total` was invisible, so the disk check could only cover
        the one tag whose size is known up front; and
      * a `--model` the owner named was unguarded entirely.

    Going through the adapter puts the download on the same host we probed and
    lets the same space rule the node's pull job uses apply to every tag.
    """
    from topos.engine.backends.ollama import OllamaAdapter, PullAborted
    from topos.engine.disk_space import check_space_for, format_bytes

    base_url = _ollama_base_url()
    last_pct = -1

    def _on_frame(frame: dict) -> None:
        nonlocal last_pct
        total = int(frame.get("total") or 0)
        completed = int(frame.get("completed") or 0)
        if total <= 0:
            return
        # The same call `_apply_frame` makes, so both surfaces stop on the same
        # rule rather than each carrying their own copy of it.
        verdict = check_space_for(total, base_url=base_url)
        if verdict is not None:
            raise PullAborted(verdict.message(tag))
        pct = min(100, int(100 * completed / total))
        if pct != last_pct:
            last_pct = pct
            click.echo(
                f"\r      {pct:3d}%   {format_bytes(completed)} / {format_bytes(total)}   ",
                nl=False,
            )

    OllamaAdapter().pull_model(tag, stream=True, on_progress=_on_frame)
    if last_pct >= 0:
        click.echo("")
    return 0
