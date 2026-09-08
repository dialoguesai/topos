<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/dialoguesai/topos/main/docs/assets/topos_mark_white.png">
  <img src="https://raw.githubusercontent.com/dialoguesai/topos/main/docs/assets/topos_mark_black.png" alt="Topos" width="120" />
</picture>

# Topos

### A digital twin, built from your own data.

**Connect the data you choose, and it takes the shape of you. Keep it private, or give access to your friends, your AIs, or the organizations you belong to — each seeing only what you allow.**

[Website](https://topos.dialogues.ai) · [Docs](https://topos.dialogues.ai/docs/welcome) · [Quick start](#quick-start) · [How it works](#how-it-works) · [Discord](https://discord.gg/BSahgm54mD)

[![PyPI](https://img.shields.io/pypi/v/topos-node?style=flat-square&color=111111&label=topos-node)](https://pypi.org/project/topos-node/)
[![Python](https://img.shields.io/pypi/pyversions/topos-node?style=flat-square&color=111111)](https://pypi.org/project/topos-node/)
[![License](https://img.shields.io/badge/license-Apache%202.0-111111?style=flat-square)](LICENSE)
[![Discord](https://img.shields.io/badge/discord-join-5865F2?style=flat-square&logo=discord&logoColor=white)](https://discord.gg/BSahgm54mD)
[![X](https://img.shields.io/badge/@dialoguesai-111111?style=flat-square&logo=x&logoColor=white)](https://x.com/dialoguesai)

</div>

---

https://github.com/user-attachments/assets/11a92ab3-0aad-436f-9815-73b1ef2855e2

---

## What Topos is

Topos is a small program you run on your own computer.

You connect the data you already have — messages, mail, calendar, files, notes, browsing history, past AI chats — one source at a time, and only the ones you choose. Whatever you connect lands in one shape, and from that shape Topos builds a working model of you: what you know, who you know, where your attention goes, and how all of it has changed over time. Not a backup of your files — a representation of the person who made them.

That model stays on your disk. An engine sits on top of it to answer questions about your life, and a permission layer decides exactly what any app, assistant, or person gets back.

So you can ask it the things you would never paste into ChatGPT — and you can carry a slice of yourself to a friend, an AI, or an organization without handing over the rest.

```
  YOUR DATA             TOPOS DATABASE         TOPOS ENGINE           THE ANSWER
  ─────────             ──────────────         ────────────           ──────────
  mail · chat           one SQLite file        reads, reasons,        scoped
  calendar · files      on your disk,          enforces your          filtered
  notes · browsing  ─▶  yours alone        ─▶  permissions        ─▶  logged
  AI chat history
```

### Four things it does

|   | | |
| --- | --- | --- |
| 🔌 | **Connect** | Choose which sources to plug in — mail, chat, calendar, files, notes. Nothing comes in that you didn't connect. |
| 🧬 | **Shape** | What you connect is formed into a model of you — what you know, who you know, how it has changed. |
| 💬 | **Ask** | Put the questions to it that you would never send to a cloud assistant. |
| 🤝 | **Carry** | Bring a slice of yourself to friends, AIs, and the organizations you are part of. Each sees only what you allow. |

---

## Quick start

You need a Dialogues account to get a **Topos key** — the credential that pairs the node on your machine with your account. Sign up at **[topos.dialogues.ai](https://topos.dialogues.ai)** and open **Create Topos**; your key is on that screen.

### Option A — macOS app (easiest)

1. On the **Create Topos** screen, click **Download** to get `Topos.dmg` (signed and notarized).
2. Mount it, drag **Topos** into Applications, and launch it. A menu-bar icon appears.
3. Back in the browser, click **Connect this Mac** and allow the prompt. The app takes a one-time pairing code and stores your key.
4. Leave the page open while it prepares: the node downloads its engine (~900 MB) and language models (~2.9 GB). The menu-bar icon shows progress.

### Option B — terminal (macOS, Linux, Windows)

Install [uv](https://docs.astral.sh/uv/) if you do not have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then:

```bash
uv tool install topos-node
topos-node --set-topos-key "<YOUR_TOPOS_KEY>"
topos-node
```

Your key is written to `~/.topos/.env` with restricted permissions. The node starts on port **8676** and connects out to the control plane.

Check that it is up:

```bash
curl http://localhost:8676/healthcheck
```

Your Topos now shows as **Connected** in the web app, and you can start adding sources.

---

## What it connects to

| Kind | Sources available today |
| --- | --- |
| 💬 **Messages** | iMessage, Signal Desktop |
| 🤖 **AI history** | ChatGPT (export file, and live conversations) |
| 🗂️ **Work & docs** | GitHub activity, Notion pages, Google Drive |
| 📅 **Calendar** | Google Calendar |
| 🌐 **Web** | Browser visits, browser events (highlights, stars) |
| 🎙️ **Transcripts** | VoxTerm voice transcripts, YouTube transcripts |
| 👥 **People** | Canonical address book |
| 🧪 **Demo data** | Ten fixture sets — messenger, email, calendar, journal, resume, finance, browsing, places, contacts — so you can try Topos before connecting anything real |
| 🔧 **Your own** | Anything not on this list — build a connector for it. Keep it private to your node, or share it to the community catalog for others to install in one click. |

Sources are not hardcoded. Connectors are declared at **[sheaf.dialogues.ai](https://sheaf.dialogues.ai)** and installed by your node at runtime — one you build stays private to your node until you choose to share it.

### Connect an assistant

Your node speaks [MCP](https://modelcontextprotocol.io), so Claude and ChatGPT can query it — through the control plane, which routes the request to your machine rather than holding your data.

- **Claude Desktop** — point the MCP adapter at the control plane's `/mcp` endpoint with your Topos key as the bearer token.
- **ChatGPT** — add the same URL as a connector and sign in with Dialogues.

One node, one key, both assistants. See [Docs → Install Topos](https://topos.dialogues.ai/docs/install-topos).

---

## How it works

From the apps you plug in, to the assistant asking a question:

```
 ├─ WHAT YOU PLUG IN
 │  gmail . imessage . calendar . drive . notion . github
 │  browser history . your old chatgpt conversations
 ▼
 ├─ EVERYTHING LANDS IN THE SAME SHAPE
 │  imessage . telegram . signal      all become one kind of row
 │  ical . google calendar . outlook  become another
 │  chatgpt . claude . grok           become another
 ▼
 ├─ TOPOS READS BACK OVER IT
 │  models on your own machine tag it, link it, summarise it
 │  raw records turn into understanding
 ▼
 ├─ AND BUILDS FOUR THINGS
 └──┬──────────────────┬──────────────────┬──────────────────┐
    │                  │                  │                  │
    timeline           search             dossiers           people
    ────────           ──────             ────────           ──────
    temporal           vector database    everything topos   social graph
    knowledge graph    finds things by    knows about a      who you know,
    how things         what they mean,    person, project    and how you
    changed over time  not exact words    or topic           relate to them
    │                  │                  │                  │
    └──────────────────┴────────┬─────────┴──────────────────┘
                                ▼
 ═══════════════════════════════╧═════════════════════════════════════════════
    THE COGNITIVE FIREWALL
    nothing leaves without crossing this. it checks who is asking,
    what they may see, hands back only that, and logs what it sent.
 ═══════════════════════════════╤═════════════════════════════════════════════
    WHO GETS TO ASK             │
    ┌───────────────────────────┼───────────────────────────┐
    ▼                           ▼                           ▼
    claude and chatgpt          the topos app               any other app
    the ai apps you use         your own view of it         you handed a key
```

Underneath, that is two parts kept deliberately separate.

| Part | What it is | What it does |
| --- | --- | --- |
| 🗂️ **Topos Database** | Your memory, on your disk | Stores your records, keeps your history, makes your context searchable |
| 🧠 **Topos Engine** | Your decision layer | Understands the request, runs the AI work, and returns only what your permissions allow |

Because they are separate, you can change how your Topos thinks — models, pipelines, enrichment — without touching what it remembers.

### The Cognitive Firewall

The engine does not hand out rows. Every request is evaluated against **scopes** — named lanes over your data that you grant, and can revoke.

Grant a scheduling agent `schedule:read` and `availability:read`:

```
✅ granted      schedule:read, availability:read   →  "free Thursday afternoon"
❌ not granted  messages:read                      →  never sees a single message
🔒 revoked      any scope, at any time             →  the next request comes back empty
```

Some lanes are owner-only by design and are not offered for sharing at all.

Three rules hold across every path:

- **Permission-aware** — the grant is checked before data is read, not after.
- **Precision over data dumps** — the engine is built to answer the question, not to ship your history.
- **Transparent** — what was asked, what was disclosed, and what was withheld are all legible to you.

### Where your data actually lives

- Everything is under **`~/.topos`** — a SQLite database, your key, your logs, your backups.
- Processing runs on your machine, through **[Ollama](https://ollama.com)** for local models and **[Hugging Face](https://huggingface.co)** for model downloads.
- Nothing is uploaded to Topos for storage.

### Why there is a control plane

Your node sits behind your home network with no open ports. The control plane is
what makes it reachable anyway: leave your Topos running and online, and you can
use the web app from anywhere — phone, laptop, someone else's machine — and reach
your own node. It is a router, not a store.

The same layer is what lets nodes coordinate with each other, so sharing and
networked flows work between people rather than only inside one machine.

> The control plane is being prepared to run on confidential compute, so that even
> the routing tier cannot read what passes through it.

---

## Everyday commands

```bash
topos-node                                  # start the node
topos-node --set-topos-key "<KEY>"          # save your key and exit
topos-node --discover                       # show which database is being served
topos-node --port 9100 --host 127.0.0.1     # bind somewhere else
topos-node --app                            # menu-bar mode; logs to ~/.topos/logs/node.log
topos-node profile --help                   # run more than one Topos on this machine

uv tool upgrade topos-node                  # update
uv tool uninstall topos-node                # remove
```

Keep your `TOPOS_KEY` private, and never commit a real one. `env.example` lists every setting the node reads.

---

## For developers

```bash
uv sync --extra engine
just run
```

Tests:

```bash
pytest tests -q
```

The default lane is hermetic — temporary databases only. Lanes that touch your real `~/.topos` or a running node are opt-in by marker.

- **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** — local setup, test lanes, engine memory tuning
- **[docs/PLUGINS.md](docs/PLUGINS.md)** — write a plugin that registers handlers and connectors
- **[docs/testing/TEST_LANES.md](docs/testing/TEST_LANES.md)** — which tests run when, and why
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — scope, hooks, and security expectations

---

## Links

- **Product** — [topos.dialogues.ai](https://topos.dialogues.ai)
- **Docs** — [topos.dialogues.ai/docs](https://topos.dialogues.ai/docs/welcome)
- **Connector creator** — [sheaf.dialogues.ai](https://sheaf.dialogues.ai)
- **Company** — [dialogues.ai](https://dialogues.ai)
- **Community** — [Discord](https://discord.gg/BSahgm54mD) · [X](https://x.com/dialoguesai)

## License

[Apache 2.0](LICENSE)
