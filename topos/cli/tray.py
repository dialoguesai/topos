"""Menu-bar / system-tray status icon for topos-node.

Port of the original open-source topos-cli ``menu_bar_app`` (pystray + PIL):
the Topos glyph, with a badge composited in the corner *only when there is
something to say*. A running node shows the bare mark. ``!`` means it needs
the user, ``x`` means it is failing (after repeated misses — a single late
probe is a busy node, not a dead one), ``v`` means a newer topos-node is on
PyPI, and a turning arc means it is starting. The menu offers API docs, the
hosted Topos app, a one-click update when one is available, and Quit.

There is deliberately no "healthy" badge. This used to keep a coloured dot lit
at all times — green running, yellow starting, red down — and a light that is
green almost all of the time says nothing, only continuously. Matches the macOS
shell's ``StatusIcon`` and the Windows shell's ``icons.py``; change all three
together.

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

#: Everything is drawn at this multiple of the final size and then
#: LANCZOS-downsampled. PIL's ImageDraw does not anti-alias, so a 2px ring with
#: a 2px symbol inside it at the shipping size came out chunky — the cross
#: burst through its own ring and the exclamation's dot welded itself to the
#: stem. Supersampling buys the fractional geometry the Swift shell gets for
#: free from AppKit, and lets the three trays share one set of proportions.
SUPERSAMPLE = 4

#: The mark's ink inside its own PNG, and how tall it should stand.
#: The artwork carries a ~10% margin of its own; drawn full-bleed the ink came
#: to 72% of the canvas, a visible step below every neighbour in the bar.
#: `_mark_box` pushes that margin off the canvas instead of paying for it twice.
#: 17.5/22 is what Apple's own menu-bar extras measure — see the Swift shell.
INK_FRACTION_H = 13.0 / 18.0
MARK_INK_FRACTION = 17.5 / 22.0

#: The badge's ring, the clear gap that separates it from the mark, and the
#: margin that keeps the whole thing off the canvas edge. The margin is
#: load-bearing: a cleared circle that runs off the edge is squared off by it,
#: and the straight edge that leaves reads as a notch bitten out of the corner.
BADGE_FRACTION = 8.4 / 22.0
CLEARANCE_FRACTION = 1.2 / 22.0
MARGIN_FRACTION = 0.5 / 22.0


def _mark_box(size: float) -> tuple[float, float, float, float]:
    """Where to draw the PNG so its INK stands MARK_INK_FRACTION of `size` tall.
    The box overflows the canvas, which is the point."""
    box = size * MARK_INK_FRACTION / INK_FRACTION_H
    off = (size - box) / 2
    return (off, off, off + box, off + box)


def _badge_ring(size: float) -> tuple[float, float, float, float]:
    u = size / 22.0
    d = 8.4 * u
    edge = size - 0.5 * u - 1.2 * u
    return (edge - d, edge - d, edge, edge)

HEALTH_POLL_SECONDS = 5.0
#: Must sit above ``probe_db_health`` and above a busy event loop. A 3s
#: client deadline raced the probe (and ``/device_info`` on the same tick)
#: and painted the tray red for a live node.
HEALTH_TIMEOUT_SECONDS = 10.0
#: One missed probe is a stalled event loop, not a down node. Two in a row
#: is enough to go red; a single success clears the count (same shape as
#: the web app's ``HEALTH_FAILURE_THRESHOLD``).
HEALTH_FAILURE_THRESHOLD = 2
TOPOS_APP_URL = "https://topos.dialogues.ai"
TOPOS_DOCS_URL = "https://topos.dialogues.ai/docs/welcome"


def resolve_tray_health_status(
    *,
    probe_ok: bool,
    consecutive_failures: int,
    current_status: str,
    failure_threshold: int = HEALTH_FAILURE_THRESHOLD,
) -> tuple[str, int]:
    """Map one ``/healthcheck`` result to (next_status, next_failure_count).

    A single timeout must not flip a green (or still-starting) icon to red.
    """
    if probe_ok:
        return "healthy", 0
    failures = consecutive_failures + 1
    if failures >= failure_threshold:
        return "down", failures
    return current_status, failures


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
                # Ink on a transparent ground, like every other tray icon.
                # topos_blk_rounded is ink on an opaque white TILE — that is
                # the app icon, and it drew a filled white box among bare
                # glyphs. Caveat: macOS tints the menu bar to the desktop
                # picture, so a light-mode Mac with a dark wallpaper has a DARK
                # bar that `defaults read` cannot see, and this returns the
                # dark glyph for it. The Swift shell reads the status button's
                # own effectiveAppearance and gets it right; pystray has no
                # equivalent, so this stays a best guess.
                return "topos_black.png"  # light menu bar → dark glyph
        except Exception:
            pass
    return "topos_white.png"


def create_status_image(status: str, glyph: str | None = None, phase: float | None = None):
    """Topos glyph, with a badge composited bottom-right when one is warranted.

    ``healthy`` draws nothing at all — the mark being in the tray is the claim
    that the node is up. ``down`` gets a cross, ``update`` a download arrow, and
    anything else (``starting``, and any status this does not recognise) gets an
    exclamation point: an unknown state is not a claim that all is well. With
    ``phase`` (0..1) the badge becomes a rotating three-quarter arc — the
    "starting" spinner. A static badge through a multi-minute first run reads as
    a hang; motion reads as "wait, it's working".

    Every badge is monochrome, drawn in whichever of black/white contrasts with
    the active glyph. Meaning is carried by the symbol, never by a hue, so the
    icon reads the same on a light tray, a dark tray, and to a colourblind user.
    """
    from PIL import Image, ImageDraw

    name = glyph or _glyph_filename()
    size = ICON_SIZE * SUPERSAMPLE
    base = Image.open(ASSETS_DIR / name).convert("RGBA")
    box = _mark_box(size)
    side = round(box[2] - box[0])
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    canvas.paste(base.resize((side, side), Image.Resampling.LANCZOS),
                 (round(box[0]), round(box[1])))

    ink = _badge_ink(name)
    if status != "healthy" or phase is not None:
        _clear_gap(canvas, size)
    dc = ImageDraw.Draw(canvas)
    if status == "update":
        _draw_download_badge(dc, ink, size)
    elif phase is not None:
        _draw_spinner_badge(dc, ink, size, phase)
    elif status == "healthy":
        pass  # the whole point: a working node decorates nothing
    elif status == "down":
        _draw_cross_badge(dc, ink, size)
    else:
        _draw_exclamation_badge(dc, ink, size)
    return canvas.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)


def _badge_ink(glyph: str) -> tuple[int, int, int, int]:
    """The ink the ring and symbol are stroked in — the same ink as the mark."""
    return (255, 255, 255, 255) if "white" in glyph else (0, 0, 0, 255)


def _pt(size: float) -> float:
    """One point of the macOS shell's 22pt canvas, in this canvas's pixels.

    Every number below is the Swift shell's own figure in points (StatusIcon),
    so the three trays are the same drawing at three resolutions rather than
    three drawings that resemble each other. Change one, change all three.
    """
    return size / 22.0


def _clear_gap(canvas, size: float) -> None:
    """Punch a transparent circle through the mark so the tray shows through.

    The badge used to be a solid disc in the tray's ink with the symbol knocked
    out of it — on a dark tray a bright blob that outweighed the mark it was
    there to annotate. A hole is lighter and theme-proof: the gap is
    transparency, not a colour, so it is right on a dark tray, a light tray, and
    anything in between, with nothing left to get wrong.
    """
    from PIL import Image, ImageDraw

    left, top, right, bottom = _badge_ring(size)
    c = 1.2 * _pt(size)
    keep = Image.new("L", canvas.size, 255)
    ImageDraw.Draw(keep).ellipse((left - c, top - c, right + c, bottom + c), fill=0)
    canvas.putalpha(
        Image.composite(canvas.getchannel("A"), Image.new("L", canvas.size, 0), keep)
    )


def _draw_ring(dc, ink, size: float) -> None:
    """The thin outlined ring all three symbols sit inside — one container, so
    what the user reads is the symbol and not the styling."""
    dc.ellipse(_badge_ring(size), outline=ink, width=max(1, round(1.35 * _pt(size))))


def _badge_center(size: float) -> tuple[float, float]:
    left, top, right, bottom = _badge_ring(size)
    return ((left + right) / 2, (top + bottom) / 2)


def _draw_exclamation_badge(dc, ink, size: float) -> None:
    """Something needs the user, but nothing is broken."""
    _draw_ring(dc, ink, size)
    cx, cy = _badge_center(size)
    u = _pt(size)
    w = max(1, round(1.3 * u))
    # PIL's y grows DOWNWARD, so the small y is the visual TOP of the stem and
    # the dot sits below it. (The Swift shell draws this y-up and is mirrored.)
    dc.line([(cx, cy - 1.4 * u), (cx, cy + 0.2 * u)], fill=ink, width=w)
    # Same width as the stem, and clear of its end, or the two weld into one
    # mark at tray scale.
    r = 0.65 * u
    dc.ellipse((cx - r, cy + 1.6 * u - r, cx + r, cy + 1.6 * u + r), fill=ink)


def _draw_cross_badge(dc, ink, size: float) -> None:
    """Topos is not working."""
    _draw_ring(dc, ink, size)
    cx, cy = _badge_center(size)
    u = _pt(size)
    # Reach measured along the DIAGONAL from the centre: the corners of a square
    # inset from the ring's bounding box sit outside the circle, which is how
    # the cross first came to burst through its own ring.
    a = 2.55 * u / (2 ** 0.5)
    w = max(1, round(1.3 * u))
    dc.line([(cx - a, cy - a), (cx + a, cy + a)], fill=ink, width=w)
    dc.line([(cx - a, cy + a), (cx + a, cy - a)], fill=ink, width=w)


def _draw_download_badge(dc, ink, size: float) -> None:
    """An update is waiting — the one badge that is good news."""
    _draw_ring(dc, ink, size)
    cx, cy = _badge_center(size)
    u = _pt(size)
    dc.line([(cx, cy - 1.7 * u), (cx, cy)], fill=ink, width=max(1, round(1.2 * u)))
    half = 1.7 * u
    dc.polygon([(cx - half, cy), (cx + half, cy), (cx, cy + 1.7 * u)], fill=ink)

def _draw_spinner_badge(dc, ink, size: float, phase: float) -> None:
    """Starting, installing, restarting — said by moving, not by colour.

    The container IS the spinner: a ring drawn around a second, smaller arc read
    as two concentric circles and said nothing.
    """
    start = (phase % 1.0) * 360
    dc.arc(_badge_ring(size), start=start, end=start + 260, fill=ink,
           width=max(1, round(1.5 * _pt(size))))


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

        consecutive_failures = 0
        while self._icon is not None and self._icon.visible:
            try:
                # 401/403 = health auth enabled; the node answered, so it's up.
                healthy = httpx.get(
                    self.health_url, timeout=HEALTH_TIMEOUT_SECONDS
                ).status_code in (200, 401, 403)
            except Exception:
                healthy = False
            if healthy:
                self._fetch_shell_status()
                self._maybe_fetch_topos_name()
            next_status, consecutive_failures = resolve_tray_health_status(
                probe_ok=healthy,
                consecutive_failures=consecutive_failures,
                current_status=self.status,
            )
            self._set_status(next_status)
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
    # execv keeps this pid, so a graph rebuild child's parent watch never fires,
    # and the new image starts with an empty child registry: stop it here or it
    # runs on unowned, holding the rebuild lock against the node we become.
    from topos.features.entities.rebuild_subprocess import stop_rebuild_children

    stop_rebuild_children()
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
        # Every tray exit comes through here (Quit, Select Topos, Ctrl+C). None
        # is a signal the node's shutdown hooks see — uvicorn runs off the main
        # thread here, so they never install — and uvicorn drains an in-flight
        # graph rebuild request before shutdown_event can stop the child. So
        # stop it now; the request then ends and the drain can finish.
        try:
            from topos.features.entities.rebuild_subprocess import signal_rebuild_children

            signal_rebuild_children()
        except Exception as exc:  # noqa: BLE001 — never let this cost the exit itself
            print(f"Could not stop graph rebuild children ({exc}); quitting anyway.")
        finally:
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
