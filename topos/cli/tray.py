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
UPDATE_POLL_SECONDS = 3600.0
TOPOS_APP_URL = "https://topos.dialogues.ai"


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
    """Owns the pystray icon plus the health/update poller threads."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        version: str,
        package_name: str,
        on_quit,
    ) -> None:
        poll_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        self.health_url = f"http://{poll_host}:{port}/healthcheck"
        self.docs_url = f"http://{poll_host}:{port}/docs"
        self.version = version
        self.package_name = package_name
        self._on_quit = on_quit
        self._glyph = _glyph_filename()
        self.status = "starting"
        self.update_info = None  # runtime_update.UpdateInfo when one is available
        self._icon = None

    # -- status ------------------------------------------------------------

    def _poll_health(self) -> None:
        import httpx

        while self._icon is not None and self._icon.visible:
            try:
                healthy = httpx.get(self.health_url, timeout=3.0).status_code == 200
            except Exception:
                healthy = False
            self._set_status("healthy" if healthy else "down")
            time.sleep(HEALTH_POLL_SECONDS)

    def _poll_updates(self) -> None:
        from topos.runtime_update import check_for_update, should_skip_update_check

        if should_skip_update_check(cli_skip=False):
            return
        while self._icon is not None and self._icon.visible:
            try:
                self.update_info = check_for_update(self.package_name)
            except Exception:
                self.update_info = None
            self._refresh()
            time.sleep(UPDATE_POLL_SECONDS)

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
        if display == "healthy" and self.update_info is not None:
            display = "update"
        try:
            icon.icon = create_status_image(display, glyph=self._glyph)
            icon.menu = self._build_menu()
        except Exception:
            pass

    # -- menu --------------------------------------------------------------

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
            pystray.MenuItem("Open API Docs", self._open_docs),
        ]
        if self.update_info is not None:
            items.append(
                pystray.MenuItem(
                    f"Update to v{self.update_info.latest}", self._apply_update
                )
            )
        items.extend([pystray.Menu.SEPARATOR, pystray.MenuItem("Quit Topos Node", self._quit)])
        return pystray.Menu(*items)

    def _open_docs(self, icon=None, item=None) -> None:
        webbrowser.open_new(self.docs_url)

    def _open_app(self, icon=None, item=None) -> None:
        webbrowser.open_new(TOPOS_APP_URL)

    def _apply_update(self, icon=None, item=None) -> None:
        def worker() -> None:
            from topos.runtime_update import apply_package_update

            ok = apply_package_update(self.package_name)
            if ok:
                self.update_info = None
                self._notify("Update installed. Restart topos-node to use it.")
            else:
                self._notify(f"Update failed. Run `uv tool upgrade {self.package_name}`.")
            self._refresh()

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
            threading.Thread(target=self._poll_updates, daemon=True).start()

        try:
            icon.run(setup=on_setup)
        finally:
            self._icon = None
            self._on_quit()


def serve_with_tray(app, *, host: str, port: int, log_config, version: str, package_name: str) -> None:
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
