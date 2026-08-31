# Manually verifying the two install paths on a machine that already runs Topos

Both paths end at the same proof: **the node connects, the react-app shows it as
Connected, and a chat turn returns an answer grounded in that node.** What differs
is how the node gets onto the machine.

- **Path A — terminal**: `uv tool install topos-node` → `--set-topos-key` → run.
- **Path B — macOS app**: download the DMG → drag to Applications → launch →
  Connect this Mac (`topos://` pairing).

Your dev Mac is also your production node. The rule below is what keeps that safe.

> **The isolation boundary is the home directory, not the database file.**
> Only three of the node's home-relative paths are parameterized
> (`TOPOS_DATABASE_PATH`, `TOPOS_INGESTION_BASE_PATH`, `TOPOS_LOG_FILE`). The
> `.env` holding your real `TOPOS_KEY`, `~/.topos/backups`, the macOS
> Application Support dir and the legacy `~/.topos_engine` probe are all
> hardcoded `Path.home() / ...`. Move the whole home, or you are not isolated.

---

## Path A — terminal install

```bash
cd topos && just sandbox
```

Creates `~/.topos-sandboxes/default/home`, installs `topos-node[local]` from PyPI
into a throwaway venv, and runs it under that HOME on port 9100. Your
`uv tool install` copy and your real `~/.topos` are untouched. Useful flags:
`--version 1.3.3` (install the previous release, then exercise the in-place
upgrade), `--local` (install the working tree instead of PyPI), `--reset`,
`--name second`, `--link-messages`.

### Connecting it to the react-app

**The deployed app works as-is** — no local frontend, no port forwarding. In
production `NEXT_PUBLIC_TOPOS_API_BASE_URL=https://cp.logu3s.com`: the browser
talks to the control plane, and the control plane reaches your node over the
node's own outbound sync socket. The sandbox node's port 9100 is never dialed by
a browser. (`NEXT_PUBLIC_ENGINE_HTTP_URL=http://127.0.0.1:8676` is a local-dev
direct path only.)

One account owns a **fleet** of nodes — `getFleetStatus()` returns a row per
`topos_key`, and `useSelfHostedEngineConnection` matches on the key you are
setting up. So you do **not** need a second Topos account:

1. In the app, create a second self-hosted Topos. The browser mints a new
   `TOPOS_KEY` and registers it against your account.
2. Copy the key from the Terminal tab and write it into the sandbox:
   ```bash
   HOME=~/.topos-sandboxes/default/home \
     ~/.topos-sandboxes/default/venv/bin/topos-node --set-topos-key <KEY>
   ```
3. Restart `just sandbox`. The setup panel's poller flips that row to Connected.
4. Select the new Topos and run a chat turn.

The sandbox sets `TOPOS_DEFAULT_DATASET_ID=sandbox-<name>` so the two nodes don't
share the `{user_id}:default` dataset namespace on CP.

### What this path does not prove

Gatekeeper, notarization, the DMG, and the `topos://` handoff. Those are Path B.

---

## Path B — the macOS app

### Why the HOME sandbox cannot be reused here

The Swift shell finds the node via Foundation's
`homeDirectoryForCurrentUser`. That API resolves through directory services and
**ignores `$HOME`** — verified directly:

```
--- real ---              homeDirectoryForCurrentUser = /Users/dialogues
--- HOME overridden ---   homeDirectoryForCurrentUser = /Users/dialogues
```

So `open --env HOME=...` does not redirect the app, and a GUI double-click never
inherits a shell's environment anyway. There is no env override for this path.

### Why your account can't test it even ignoring the database

The shell probes, in order, `~/.local/bin/topos-node`,
`~/.topos/runtime/bin/topos-node`, `/opt/homebrew/bin`, `/usr/local/bin`. You have
`~/.local/bin/topos-node` (the `uv tool install` copy), so the app takes the
"already installed" branch and **the bootstrap you are trying to test never
runs** — it just spawns your production node against your production database.

There is also a live hazard specific to this machine: `lsregister -dump` shows
**two** bundles claiming the `topos:` scheme under two different code signatures.
A `topos://connect` deep link may be delivered to the stale one. LaunchServices
claims are per-user, so this is another reason not to test app installs on the
account that already has one.

### Preparing this account instead (faster, lower fidelity)

```bash
just app-install-test                 # what currently shadows a fresh install
just app-install-test prepare --yes   # move it all aside
just app-install-test restore --yes   # put it back
```

Moves `~/.topos`, `~/.topos_engine`, the `~/.local/bin/topos-node` shim and the
Application Support dirs into `~/.topos-install-test/`. Same filesystem, so each
move is an atomic rename — your 480MB database is not copied and never rewritten.
`restore` keeps whatever the test created (as `test-run__*`) rather than deleting
it, so you can inspect the install afterwards.

It refuses to run while anything holds those databases open. That guard checks
the database *files* and the open files of every `topos-node`/`ToposShell`
process — `lsof <dir>` does not report processes holding files inside a
directory, so a directory-level check silently passes while a node is live.

This buys you the app's real bootstrap branch on a clean home. It does **not**
fake a Gatekeeper first-run, and it does not clear the `topos:` LaunchServices
claims (there are currently 3 registered on this machine). For those, use a
second account.

### The setup: a second macOS user account

Create a standard (non-admin) account, e.g. `toposqa`, and use Fast User
Switching. This is the whole fix, and it generalizes to any dev with the same
problem:

| Concern | Under a second account |
|---|---|
| `~/.topos`, `~/.local/bin`, uv tool dir | fresh — bootstrap branch actually runs |
| your real data | a different user's home, `drwxr-x---` — unreachable |
| `topos://` LaunchServices claim | per-user database, clean |
| Downloads quarantine xattr | fresh DMG → genuine Gatekeeper first-run |
| Keychain, Full Disk Access, Messages | per-user, granted from scratch (correct fidelity) |

Two caveats:

- **`/Applications` is shared.** "Drag to Applications" will land on the existing
  bundle. Either remove `/Applications/Topos.app` before the run, or have the
  test user install into their own `~/Applications`. Removing it is closer to
  what a new user sees.
- **Model caches re-download** (multi-GB torch + sentence-transformers) unless
  you set `HF_HOME` for that user. Leave it unset the first time — a cold model
  download *is* part of the first-run experience you want to measure.

### Steps

1. Sign out of your node (`~/.topos` node stopped) — not strictly required, but
   it keeps the fleet view unambiguous.
2. Remove `/Applications/Topos.app`, or plan to install to `~/Applications`.
3. Switch to `toposqa`. Confirm the machine looks cold:
   ```bash
   ls ~/.topos ~/.local/bin/topos-node 2>&1   # both should be "No such file"
   command -v topos-node uv                   # should be empty
   ```
4. Open the website in that account's browser, download the DMG, and note
   whether Gatekeeper's first-run dialog appears and whether it names Dialogues AI.
5. Launch the app. Expected: **needs-setup** state — yellow dot, "Finish Setup…",
   *not* a doomed node that exits (gap G1 in `PLAN_APP_SHELL_DISTRIBUTION.md`).
6. Sign in to Topos in that browser as the same account, create a self-hosted
   Topos, choose the **Download the app** tab, and click **Connect this Mac**.
7. Confirm the deep link reaches the app, the node bootstraps via the bundled
   `uv` into `~/.topos/runtime`, and the tray goes green.
8. Confirm the setup panel flips to Connected, then run a chat turn.

This is the "cold-machine E2E" left open as W4 in `PLAN_APP_SHELL_DISTRIBUTION.md`,
minus one thing: a second account on your Mac has already trusted your Developer
ID certificate at the system level. For a genuinely first-contact notarization
check you need a VM (UTM is free; Apple's Virtualization.framework allows two
macOS guests per host) or a different machine.

---

## Pairing capacity: the NAT ceiling

`topos://connect` hands the app a one-time code, which it redeems over HTTPS for
the real `TOPOS_KEY` (`control_plane/shell_pairing.py`). Four constants bound it:

| constant | value | scope |
|---|---|---|
| `PAIRING_CODE_TTL_SECONDS` | 600 | per code |
| `MAX_ACTIVE_CODES` | 2000 | outstanding codes, per CP instance |
| `REDEEM_WINDOW_SECONDS` | 600 | rate-limit window |
| `REDEEM_ATTEMPTS_PER_WINDOW` | 30 | **per client IP** |

The 2000 bounds codes that are *outstanding* — minted, not yet redeemed, not yet
expired. Redeeming removes the entry, so the store drains continuously and normal
throughput is far higher. You only accumulate 2000 if that many people click
Connect and never launch the app; the next mint then evicts the oldest code, so
the earliest clicker is the one who silently loses.

**The limit that bites is 30 per IP.** Redeems come from the user's machine, so
everyone behind one egress IP shares the budget: an office, a VPN, conference
wifi, a cohort onboarding together in one room. Person 31 within ten minutes gets
HTTP 429, and because the limiter counts *attempts* rather than successes,
clicking "Connect this Mac" again spends another one. The user-visible symptom is

> Pairing didn't finish. Click Connect this Mac again.

which reads as a broken app, not a rate limit — so people retry and dig deeper.
Plan around it before any group onboarding: stagger the room, or raise the
constant first. Check `b12` asserts these values have not drifted.

Both limits are per-instance and in memory, so a CP restart clears them — and a
restart *between* a user clicking Connect and the app redeeming invalidates that
code. During QA, a CP deploy mid-pairing is a false failure, not a defect.

## Known rough edge found while writing this — FIXED

`topos-node --discover` used to report `~/.topos_engine/database.db` (a 0-byte
legacy stub here) and **not** `~/.topos/database.db`: `discover_databases()`
checked config.json, Application Support, `~/.topos_engine` and
`TOPOS_DATABASE_PATH`, but never the `~/.topos` default that `state.py`
actually loads. Four resolvers, four candidate lists, no agreement.

That divergence had a worse consequence than a confusing diagnostic. When the
active slot held no `database.db` — every newly created Topos, before its first
write — the connection resolver fell through to those same legacy locations and
served one, so a new Topos ran against a database no profile owned and no
switch would ever archive.

Both now go through `resolve_active_database()` in `topos/storage/db/paths.py`:
`--discover` lists the active database first and then any legacy strays, and a
machine that uses profiles resolves to its slot and nothing else. What to check
during a manual install run:

- `topos-node --discover` names `~/.topos/database.db` first.
- The startup log carries one `Serving database … (source=…, profile=…, schema=…)`
  line, and `source` is `slot`, `new-slot` or `adopted` — never `legacy`.
- `GET /healthcheck` reports the same path in `database_path`, with
  `active_profile_id` matching the Topos you expect.
