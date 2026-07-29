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
    "update": (255, 165, 0, 255),  # orange
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
    """Open the log file in the platform's live-ish viewer (Console.app on macOS)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)  # Console.app errors on a missing file
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Console", str(log_path)])
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


def create_status_image(status: str, glyph: str | None = None):
    """Topos glyph with a status dot composited bottom-right (original layout)."""
    from PIL import Image, ImageDraw

    glyph_path = ASSETS_DIR / (glyph or _glyph_filename())
    base = Image.open(glyph_path).convert("RGBA")
    base = base.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)

    overlay = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (255, 255, 255, 0))
    dc = ImageDraw.Draw(overlay)
    dc.ellipse((22, 22, 32, 32), fill=STATUS_COLORS.get(status, STATUS_COLORS["starting"]))
    return Image.alpha_composite(base, overlay)


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
        if payload.get("version"):
            self.version = payload["version"]
        if self.log_path is None and payload.get("log_file"):
            self.log_path = Path(payload["log_file"])
            changed = True
        if changed:
            self._refresh()

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
            "Open Topos",
            "Open Docs",
        ]
        if self.log_path is not None:
            items.append("Show Logs")
        if self.update.get("applying"):
            items.append("Installing update…")
        elif self.update.get("last_result") == "success":
            items.append("Update installed — restart to finish")
        elif self.update.get("available"):
            items.append(f"Update to v{self.update.get('latest')}")
        items.append(
            "Close Tray (node keeps running)" if self.attached else "Quit Topos Node"
        )
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
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Topos", self._open_app),
            pystray.MenuItem("Open Docs", self._open_docs),
        ]
        if self.log_path is not None:
            items.append(pystray.MenuItem("Show Logs", self._show_logs))
        if self.update.get("applying"):
            items.append(pystray.MenuItem("Installing update…", None, enabled=False))
        elif self.update.get("last_result") == "success":
            items.append(pystray.MenuItem("Update installed — restart to finish", None, enabled=False))
        elif self.update.get("available"):
            items.append(
                pystray.MenuItem(f"Update to v{self.update.get('latest')}", self._apply_update)
            )
        quit_label = "Close Tray (node keeps running)" if self.attached else "Quit Topos Node"
        items.extend([pystray.Menu.SEPARATOR, pystray.MenuItem(quit_label, self._quit)])
        return pystray.Menu(*items)

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
        if self._icon is not None:
            self._icon.visible = False
            self._icon.stop()

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

        try:
            icon.run(setup=on_setup)
        finally:
            self._icon = None
            self._on_quit()


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
