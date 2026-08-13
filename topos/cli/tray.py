"""Menu-bar / system-tray status icon for topos-node.

Port of the original open-source topos-cli ``menu_bar_app`` (pystray + PIL):
the Topos glyph with a status dot composited in the corner — yellow while
starting, green while the node answers ``/healthcheck``, red while it does
not, orange when a newer topos-node is published on PyPI. The menu offers
API docs, the hosted Topos app, a one-click update when one is available,
and Quit.

pystray requires the process main thread (AppKit run loop on macOS), so
``serve_with_tray`` inverts the usual layout: uvicorn runs on a daemon
thread and the tray owns the main thread. Every pystray/PIL import is lazy —
a headless install must never pay for (or crash on) GUI deps.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICON_SIZE = 34

STATUS_COLORS = {
    "starting": (255, 210, 0, 255),  # yellow
    "healthy": (170, 255, 0, 255),  # green (original topos-cli green)
    "down": (255, 59, 48, 255),  # red
}

HEALTH_POLL_SECONDS = 5.0
TOPOS_APP_URL = "https://topos.dialogues.ai"
TOPOS_DOCS_URL = "https://topos.dialogues.ai/docs/welcome"


def tray_available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


def should_enable_tray(cli_flag: bool | None = None) -> bool:
    """Decide whether to run the tray: explicit flag wins, else auto-detect.

    Auto mode enables the tray only where a GUI session is plausible
    (macOS, Windows, or Linux with a display server) and the GUI deps import.
    """
    if cli_flag is False:
        return False
    env = (os.getenv("TOPOS_TRAY") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if cli_flag is None and env in ("1", "true", "yes", "on"):
        cli_flag = True

    gui_plausible = sys.platform in ("darwin", "win32") or bool(
        os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")
    )
    if cli_flag is True:
        return tray_available()
    return gui_plausible and tray_available()


def open_log_viewer(log_path: Path) -> None:
    """Open a live streaming view of the node log (Terminal + tail -F on macOS)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    if sys.platform == "darwin":
        # Single-quote the path for the shell; AppleScript wraps the command in "".
        quoted = "'" + str(log_path).replace("'", "'\\''") + "'"
        command = (
            "clear; echo 'Topos Node logs — Ctrl-C stops following (node keeps running)'; "
            f"echo; tail -n 200 -F {quoted}"
        )
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "Terminal"\nactivate\ndo script "{escaped}"\nend tell'
        subprocess.Popen(["/usr/bin/osascript", "-e", script])
    elif sys.platform == "win32":
        os.startfile(str(log_path))  # noqa: S606 — user-initiated menu action
    else:
        subprocess.Popen(["xdg-open", str(log_path)])


def _glyph_filename() -> str:
    """Pick the glyph that contrasts with the menu bar / taskbar."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if "Dark" not in result.stdout:
                return "topos_blk_rounded.png"  # light menu bar → dark glyph
        except Exception:
            pass
    return "topos_white.png"


def create_status_image(status: str, glyph: str | None = None, phase: float | None = None):
    """Topos glyph with a status badge composited bottom-right.

    Healthy/starting/down use a colored dot. ``update`` uses a download glyph
    (filled circle + down arrow) instead of an orange connection dot. With
    ``phase`` (0..1) the badge becomes a rotating three-quarter arc — the
    "starting" spinner. A static dot through a multi-minute first run reads as
    a hang; motion reads as "wait, it's working". Mirrors the macOS shell.
    """
    from PIL import Image, ImageDraw

    glyph_path = ASSETS_DIR / (glyph or _glyph_filename())
    base = Image.open(glyph_path).convert("RGBA")
    base = base.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)

    overlay = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (255, 255, 255, 0))
    dc = ImageDraw.Draw(overlay)
    if status == "update":
        _draw_download_badge(dc, ink=_badge_ink_for_glyph(glyph or _glyph_filename()))
    elif phase is not None:
        start = (phase % 1.0) * 360
        dc.arc((22, 22, 32, 32), start=start, end=start + 270,
               fill=STATUS_COLORS.get(status, STATUS_COLORS["starting"]), width=2)
    else:
        dc.ellipse((22, 22, 32, 32), fill=STATUS_COLORS.get(status, STATUS_COLORS["starting"]))
    return Image.alpha_composite(base, overlay)


def _badge_ink_for_glyph(glyph: str) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Return (circle_fill, arrow_fill) contrast for the active glyph."""
    if "white" in glyph:
        return (255, 255, 255, 255), (0, 0, 0, 255)
    return (0, 0, 0, 255), (255, 255, 255, 255)


def _draw_download_badge(dc, ink: tuple[tuple[int, int, int, int], tuple[int, int, int, int]]) -> None:
    circle, arrow = ink
    # Slightly larger than the status dot so the arrow reads at tray scale.
    left, top, right, bottom = 20, 20, 33, 33
    dc.ellipse((left, top, right, bottom), fill=circle)
    cx = (left + right) / 2
    shaft_top = top + 3
    shaft_bot = top + 7
    head_top = top + 6.5
    head_bot = bottom - 3
    head_half = 3.5
    dc.line([(cx, shaft_top), (cx, shaft_bot)], fill=arrow, width=2)
    dc.polygon(
        [(cx - head_half, head_top), (cx + head_half, head_top), (cx, head_bot)],
        fill=arrow,
    )


class ToposTray:
    """Owns the pystray icon plus the status poller thread.

    The tray talks to the node exclusively over the localhost shell contract
    (``/healthcheck`` + ``/v1/shell/*``) — it is the reference implementation
    of the same contract the Swift/Windows shells will consume. That is also
    what makes ``attached`` mode work: supervising a node this process did
    not start is no different from supervising its own.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        version: str,
        package_name: str,
        on_quit,
        log_path: Path | None = None,
        attached: bool = False,
    ) -> None:
        poll_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        base = f"http://{poll_host}:{port}"
        self.health_url = f"{base}/healthcheck"
        self.docs_url = TOPOS_DOCS_URL
        self.status_url = f"{base}/v1/shell/status"
        self.update_url = f"{base}/v1/shell/update"
        self.version = version
        self.package_name = package_name
        self._on_quit = on_quit
        self.log_path = log_path
        self.attached = attached
        self._glyph = _glyph_filename()
        self.status = "starting"
        self.update = {"available": False, "latest": None, "applying": False, "last_result": None}
        self._icon = None
        # From /v1/shell/status: lets Quit stop a node this process did not
        # start (the attached case — an app crash or update restart leaves a
        # user here, and the tray used to offer them no way out).
        self.node_pid: int | None = None
        # The user-chosen name of the bound Topos ("PersonalDB"). Known to the
        # CONTROL PLANE, not the node — same lesson as the macOS shell 0.2.10:
        # asking the node renders nothing, ever.
        self.topos_name: str | None = None
        self._topos_name_tick = 0
        # Set by the Select Topos submenu, acted on by serve_with_tray once the
        # server has actually stopped: ("switch", profile_id) or ("new", "").
        # A click cannot do the work itself — the node it must stop IS this
        # process, so the swap happens after the run loop unwinds.
        self.pending_profile_action: tuple[str, str] | None = None

    # -- status ------------------------------------------------------------

    def _poll_health(self) -> None:
        import httpx

        while self._icon is not None and self._icon.visible:
            try:
                # 401/403 = health auth enabled; the node answered, so it's up.
                healthy = httpx.get(self.health_url, timeout=3.0).status_code in (200, 401, 403)
            except Exception:
                healthy = False
            if healthy:
                self._fetch_shell_status()
                self._maybe_fetch_topos_name()
            self._set_status("healthy" if healthy else "down")
            time.sleep(HEALTH_POLL_SECONDS)

    def _fetch_shell_status(self) -> None:
        import httpx

        try:
            payload = httpx.get(self.status_url, timeout=3.0).json()
        except Exception:
            return
        update = payload.get("update") or {}
        changed = update != self.update
        self.update = update
        if isinstance(payload.get("pid"), int):
            self.node_pid = payload["pid"]
        if payload.get("version"):
            self.version = payload["version"]
        if self.log_path is None and payload.get("log_file"):
            self.log_path = Path(payload["log_file"])
            changed = True
        if changed:
            self._refresh()

    def _maybe_fetch_topos_name(self) -> None:
        """Ask the control plane for the bound Topos's name, once a minute.

        Best effort on every edge: no key, no network, or an unenriched control
        plane all just leave the row out of the menu.
        """
        self._topos_name_tick += 1
        if self.topos_name is not None and self._topos_name_tick % 12 != 1:
            return
        try:
            import httpx

            key = ""
            env_file = Path.home() / ".topos" / ".env"
            for line in env_file.read_text().splitlines():
                if line.startswith("TOPOS_KEY="):
                    key = line.split("=", 1)[1].strip().strip("\"'")
                    break
            if not key:
                return
            base = os.environ.get("TOPOS_CP_URL", "https://cp.logu3s.com").rstrip("/")
            payload = httpx.get(
                f"{base}/device_info",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15.0,
            ).json()
            name = str(payload.get("bound_topos_name") or "").strip()
            if name and name != self.topos_name:
                self.topos_name = name
                self._refresh()
        except Exception:  # noqa: BLE001 — a nameless menu beats a crashed tray
            pass

    def _animate_while_starting(self) -> None:
        """Spin the badge while the node is starting; idle otherwise."""
        phase = 0.0
        while self._icon is not None and self._icon.visible:
            if self.status == "starting":
                phase = (phase + 1.0 / 14.0) % 1.0
                try:
                    self._icon.icon = create_status_image("starting", glyph=self._glyph, phase=phase)
                except Exception:  # noqa: BLE001 — a still badge beats a dead tray
                    pass
                time.sleep(1.0 / 12.0)
            else:
                time.sleep(0.5)

    def _set_status(self, status: str) -> None:
        if self.status == status:
            return
        self.status = status
        self._refresh()

    def _refresh(self) -> None:
        icon = self._icon
        if icon is None:
            return
        display = self.status
        if display == "healthy" and self.update.get("available"):
            display = "update"
        try:
            icon.icon = create_status_image(display, glyph=self._glyph)
            icon.menu = self._build_menu()
        except Exception:
            pass

    # -- profiles ----------------------------------------------------------

    def _profiles(self) -> list:
        """Every Topos on this machine, active first. Empty on any error —
        a tray that cannot list profiles must still render its other rows."""
        try:
            from topos import profiles

            return profiles.list_profiles()
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        gb = size_bytes / 1_000_000_000
        return f"{gb:.1f} GB" if gb >= 0.1 else f"{size_bytes // 1_000_000} MB"

    def _profile_label(self, info) -> str:
        return f"{info.name or info.profile_id} ({self._format_size(info.size_bytes)})"

    def _offers_profile_switching(self) -> bool:
        """Only a tray that owns the server may swap the database under it.

        Attached mode supervises a node this process did not start and cannot
        restart, so a switch there would stop the node, move its database, and
        leave nothing running. The macOS shell has a supervisor and can do
        this; a bare tray cannot, and pretending otherwise is worse than
        omitting the row.
        """
        return not self.attached

    # -- menu --------------------------------------------------------------

    def _menu_labels(self) -> list[str]:
        """Plain menu labels — testable without initializing a GUI display."""
        labels = {
            "starting": "starting…",
            "healthy": "running",
            "down": "not responding",
        }
        items = [
            f"Topos Node v{self.version} — {labels.get(self.status, self.status)}",
        ]
        if self.topos_name:
            items.append(f"Topos: {self.topos_name}")
        items += [
            "Open Topos",
            "Open Docs",
        ]
        if self._offers_profile_switching():
            items.append("Select Topos")
            for info in self._profiles():
                items.append(self._profile_label(info))
            items.append("New Topos…")
        if self.log_path is not None:
            items.append("Show Logs")
        if self.update.get("applying"):
            items.append("Installing update…")
        elif self.update.get("last_result") == "failed":
            items.append("Update failed — click to retry")
        elif self.update.get("last_result") == "success":
            items.append("Update installed — restart to finish")
        elif self.update.get("available"):
            items.append(f"Update to v{self.update.get('latest')}")
        # Quit means quit, attached or not — "Close Tray (node keeps running)"
        # as the ONLY exit stranded anyone whose tray attached after an app
        # crash or update restart, with no way to ever stop the node. The
        # tray-only exit stays available, explicitly and second.
        items.append("Quit Topos Node")
        if self.attached:
            items.append("Close Tray Only (node keeps running)")
        return items

    def _build_menu(self):
        import pystray

        labels = {
            "starting": "starting…",
            "healthy": "running",
            "down": "not responding",
        }
        items = [
            pystray.MenuItem(
                f"Topos Node v{self.version} — {labels.get(self.status, self.status)}",
                None,
                enabled=False,
            ),
        ]
        if self.topos_name:
            items.append(pystray.MenuItem(f"Topos: {self.topos_name}", None, enabled=False))
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Topos", self._open_app),
            pystray.MenuItem("Open Docs", self._open_docs),
        ]
        if self._offers_profile_switching():
            items.append(pystray.MenuItem("Select Topos", self._build_profile_menu()))
        if self.log_path is not None:
            items.append(pystray.MenuItem("Show Logs", self._show_logs))
        if self.update.get("applying"):
            items.append(pystray.MenuItem("Installing update…", None, enabled=False))
        elif self.update.get("last_result") == "failed":
            # Clickable: a failed update that renders nothing is
            # indistinguishable from a click that never registered.
            items.append(pystray.MenuItem("Update failed — click to retry", self._apply_update))
        elif self.update.get("last_result") == "success":
            items.append(pystray.MenuItem("Update installed — restart to finish", None, enabled=False))
        elif self.update.get("available"):
            items.append(
                pystray.MenuItem(f"Update to v{self.update.get('latest')}", self._apply_update)
            )
        items.extend([pystray.Menu.SEPARATOR, pystray.MenuItem("Quit Topos Node", self._quit)])
        if self.attached:
            # pystray has no Option-key alternate; on Windows both exits are
            # simply visible, quit first and honest about the difference.
            items.append(
                pystray.MenuItem("Close Tray Only (node keeps running)", self._close_tray_only)
            )
        return pystray.Menu(*items)

    def _build_profile_menu(self):
        """The Select Topos submenu: active one checked, the rest switchable."""
        import pystray

        entries = []
        for info in self._profiles():
            label = self._profile_label(info)
            if info.active:
                entries.append(
                    pystray.MenuItem(label, None, enabled=False, checked=lambda item: True)
                )
            else:
                # Bind the id per iteration; a closure over the loop variable
                # would switch every row to the last profile listed.
                entries.append(
                    pystray.MenuItem(
                        label, lambda icon, item, pid=info.profile_id: self._switch_profile(pid)
                    )
                )
        entries.append(pystray.Menu.SEPARATOR)
        entries.append(pystray.MenuItem("New Topos…", self._new_topos))
        return pystray.Menu(*entries)

    def _switch_profile(self, profile_id: str) -> None:
        """Queue the switch and stand the node down; serve_with_tray finishes it."""
        self.pending_profile_action = ("switch", profile_id)
        self._notify("Switching Topos — Topos will restart.")
        self._close_tray_only()

    def _new_topos(self, icon=None, item=None) -> None:
        self.pending_profile_action = ("new", "")
        self._notify("Your current Topos is being set aside…")
        self._close_tray_only()

    def _open_docs(self, icon=None, item=None) -> None:
        webbrowser.open_new(self.docs_url)

    def _open_app(self, icon=None, item=None) -> None:
        webbrowser.open_new(TOPOS_APP_URL)

    def _show_logs(self, icon=None, item=None) -> None:
        if self.log_path is not None:
            open_log_viewer(self.log_path)

    def _apply_update(self, icon=None, item=None) -> None:
        def worker() -> None:
            import httpx

            try:
                result = httpx.post(self.update_url, timeout=10.0).json()
            except Exception:
                self._notify(f"Update failed to start. Run `uv tool upgrade {self.package_name}`.")
                return
            if result.get("started"):
                self._notify("Installing update… restart Topos Node when it finishes.")
            elif result.get("reason") == "already_applying":
                self._notify("An update is already installing.")
            self._fetch_shell_status()

        threading.Thread(target=worker, daemon=True).start()

    def _notify(self, message: str) -> None:
        try:
            if self._icon is not None:
                self._icon.notify(message, "Topos Node")
        except Exception:
            pass

    def _quit(self, icon=None, item=None) -> None:
        # Attached: stop the node this process did not start, escalating past
        # a wedged SIGTERM. In-process mode the node IS this process — closing
        # the tray ends run(), whose caller shuts the server down.
        if self.attached and self.node_pid and self.node_pid > 1:
            self._stop_node_by_pid(self.node_pid)
        self._close_tray_only()

    def _close_tray_only(self, icon=None, item=None) -> None:
        if self._icon is not None:
            self._icon.visible = False
            self._icon.stop()

    @staticmethod
    def _stop_node_by_pid(pid: int) -> None:
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return  # already gone
        # ~5s of grace, then the hard signal. SIGKILL does not exist on
        # Windows, where os.kill(SIGTERM) already terminates unconditionally.
        for _ in range(20):
            time.sleep(0.25)
            try:
                os.kill(pid, 0)
            except OSError:
                return
        hard = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            os.kill(pid, hard)
        except OSError:
            pass

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        """Run the tray on the calling (main) thread; blocks until Quit."""
        import pystray

        icon = pystray.Icon(
            "topos-node",
            icon=create_status_image("starting", glyph=self._glyph),
            title="Topos Node",
        )
        self._icon = icon
        icon.menu = self._build_menu()

        def on_setup(icon) -> None:
            icon.visible = True
            threading.Thread(target=self._poll_health, daemon=True).start()
            threading.Thread(target=self._animate_while_starting, daemon=True).start()

        try:
            icon.run(setup=on_setup)
        finally:
            self._icon = None
            self._on_quit()


def _reexec_topos_node() -> None:
    """Replace this process with a fresh topos-node, same arguments.

    The node's database is chosen at startup, so serving the switched-to Topos
    means starting over — and re-exec beats asking the user to relaunch an app
    they just clicked a menu item in. ``sys.executable -m`` rather than
    ``sys.argv[0]``: the console script is a shim whose path is not guaranteed
    to be executable from here (a uv tool shim, a wheel entry point, a
    PyInstaller bundle on Windows all differ), while the interpreter running
    us always is.
    """
    os.execv(sys.executable, [sys.executable, "-m", "topos.cli.commands", *sys.argv[1:]])


def apply_pending_profile_action(action: tuple[str, str]) -> bool:
    """Perform a queued Select Topos action. True when the node should restart.

    Runs only after the server has stopped — the profile guard refuses while
    anything answers the port, which is exactly the protection wanted here.
    """
    from topos import profiles

    kind, profile_id = action
    try:
        if kind == "switch":
            result = profiles.switch_profile(profile_id)
            print(f"Switched to '{result['activated']}'.")
            if result.get("archived_as"):
                print(f"Previous Topos kept as '{result['archived_as']}'.")
            return True
        if kind == "new":
            result = profiles.new_profile()
            if result["archived"]:
                print(f"Your Topos is kept as '{result['archived_as']}'.")
            # Deliberately does NOT restart: a node with no key exits within a
            # second, so relaunching would only print an error over the
            # instructions the user needs. This platform has no topos:// deep
            # link, so pairing is the documented terminal step.
            print(
                "This machine is now unbound. Create the new Topos at "
                f"{TOPOS_APP_URL}, then run:\n"
                "  topos-node --set-topos-key <YOUR_TOPOS_KEY>\n"
                "Your previous Topos stays available under Select Topos."
            )
            return False
    except Exception as exc:  # noqa: BLE001 — the message is the whole point
        print(f"Could not complete that: {exc}")
    return False


def attach_tray(
    *,
    host: str,
    port: int,
    version: str,
    package_name: str,
    log_path: Path | None = None,
) -> None:
    """Supervise an already-running node: tray only, no server; Quit closes just the tray."""
    ToposTray(
        host=host,
        port=port,
        version=version,
        package_name=package_name,
        on_quit=lambda: None,
        log_path=log_path,
        attached=True,
    ).run()


def serve_with_tray(
    app,
    *,
    host: str,
    port: int,
    log_config,
    version: str,
    package_name: str,
    log_path: Path | None = None,
) -> None:
    """Serve uvicorn on a daemon thread with the tray on the main thread.

    Falls back to plain foreground serving if the tray cannot start (e.g. no
    GUI session after all) — the node must come up either way.
    """
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_config=log_config)
    server = uvicorn.Server(config)

    def stop_server() -> None:
        server.should_exit = True

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    tray = ToposTray(
        host=host,
        port=port,
        version=version,
        package_name=package_name,
        on_quit=stop_server,
        log_path=log_path,
    )
    try:
        tray.run()
    except KeyboardInterrupt:
        stop_server()
    except Exception as exc:  # tray backend failed — keep serving headless
        print(f"Tray unavailable ({exc}); continuing without it.")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            stop_server()
    server_thread.join(timeout=10)

    # A Select Topos click can only be honoured here: the node it has to stop
    # is this process, so the swap waits until the server thread is done and
    # the port is free.
    if tray.pending_profile_action is not None:
        if apply_pending_profile_action(tray.pending_profile_action):
            _reexec_topos_node()
