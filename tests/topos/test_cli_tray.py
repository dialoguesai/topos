"""Tray icon module: enable/auto-detect logic and status image rendering."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.public

from topos.cli import tray
from topos.defaults import DEFAULT_NODE_HTTP_URL, DEFAULT_NODE_PORT


class TestShouldEnableTray:
    def test_explicit_off_wins(self, monkeypatch):
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=False) is False

    def test_env_off_wins(self, monkeypatch):
        monkeypatch.setenv("TOPOS_TRAY", "0")
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=None) is False

    def test_explicit_on_requires_deps(self, monkeypatch):
        monkeypatch.delenv("TOPOS_TRAY", raising=False)
        monkeypatch.setattr(tray, "tray_available", lambda: False)
        assert tray.should_enable_tray(cli_flag=True) is False
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=True) is True

    def test_auto_headless_linux_disabled(self, monkeypatch):
        monkeypatch.delenv("TOPOS_TRAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(tray.sys, "platform", "linux")
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=None) is False

    def test_auto_darwin_enabled_when_deps_import(self, monkeypatch):
        monkeypatch.delenv("TOPOS_TRAY", raising=False)
        monkeypatch.setattr(tray.sys, "platform", "darwin")
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=None) is True

    def test_env_on_enables_without_gui_detection(self, monkeypatch):
        monkeypatch.setenv("TOPOS_TRAY", "1")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(tray.sys, "platform", "linux")
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=None) is True


class TestStatusImage:
    @pytest.fixture(autouse=True)
    def _needs_pillow(self):
        pytest.importorskip("PIL")

    GLYPHS = ["topos_white.png", "topos_black.png"]

    @staticmethod
    def _bare_glyph(glyph: str):
        """The mark drawn alone, rebuilt from the same geometry the renderer
        uses but WITHOUT its badge dispatch — so "healthy draws nothing" stays
        a real claim rather than a comparison of the code against itself."""
        from PIL import Image

        size = tray.ICON_SIZE * tray.SUPERSAMPLE
        box = tray._mark_box(size)
        side = round(box[2] - box[0])
        base = Image.open(tray.ASSETS_DIR / glyph).convert("RGBA")
        canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        canvas.paste(base.resize((side, side), Image.Resampling.LANCZOS),
                     (round(box[0]), round(box[1])))
        return canvas.resize((tray.ICON_SIZE, tray.ICON_SIZE), Image.Resampling.LANCZOS)

    @staticmethod
    def _ink_box(image, threshold: int = 25):
        """Alpha bounding box of what the eye actually sees.

        Thresholded, not `getbbox()`: downsampling from the supersampled canvas
        is LANCZOS, whose negative lobes leave alpha of 1-3 out at the borders.
        Counting those as ink puts the bounding box at the canvas edge for every
        icon and makes an edge test that can never pass.
        """
        pixels = image.load()
        w, h = image.size
        xs = [x for x in range(w) for y in range(h) if pixels[x, y][3] > threshold]
        ys = [y for y in range(w) for x in range(h) if pixels[x, y][3] > threshold]
        return (min(xs), min(ys), max(xs) + 1, max(ys) + 1)

    def test_the_mark_fills_the_icon_the_way_its_neighbours_do(self):
        """The mark used to be drawn full-bleed from artwork that carries its
        own ~10% margin, so its ink came to 72% of the canvas and it read as a
        smaller, lighter icon than everything beside it in the bar.

        The floor is ABSOLUTE, not a comparison against MARK_INK_FRACTION —
        checking the render against the constant that produced it is a
        tautology and passes happily with the constant set back."""
        image = tray.create_status_image("healthy", glyph="topos_white.png")
        box = self._ink_box(image)
        height = (box[3] - box[1]) / tray.ICON_SIZE
        assert height >= 16.5 / 22.0, (
            f"the mark stands {height * 22:.1f}pt-equivalent tall; Apple's own "
            "menu-bar extras measure 18-19pt and this shipped at 13"
        )

    @pytest.mark.parametrize("status", ["starting", "down", "update"])
    def test_no_badge_touches_the_canvas_edge(self, status):
        """A cleared circle that runs off the edge is squared off by it, and the
        straight edge that leaves reads as a notch bitten out of the corner."""
        image = tray.create_status_image(status, glyph="topos_white.png")
        box = self._ink_box(image)
        margin = min(box[0], box[1], tray.ICON_SIZE - box[2], tray.ICON_SIZE - box[3])
        assert margin >= 1, f"{status} reaches the canvas edge (margin {margin}px)"

    @pytest.mark.parametrize(
        "status,phase",
        [("starting", None), ("down", None), ("update", None), ("starting", 0.25)],
    )
    def test_the_badge_is_a_hole_not_a_disc(self, status, phase):
        """The gap is transparency, not a colour — which is what makes the badge
        right on a dark tray, a light tray and anything in between. If it ever
        goes back to a filled disc, the mark's pixels there stop being cleared."""
        badged = tray.create_status_image(status, glyph="topos_white.png", phase=phase)
        bare = self._bare_glyph("topos_white.png")
        left, top, right, bottom = tray._badge_ring(tray.ICON_SIZE)
        cleared = 0
        for x in range(int(left), min(int(right) + 1, tray.ICON_SIZE)):
            for y in range(int(top), min(int(bottom) + 1, tray.ICON_SIZE)):
                if bare.getpixel((x, y))[3] > 40 and badged.getpixel((x, y))[3] < 10:
                    cleared += 1
        assert cleared > 0, (
            f"{status} (phase={phase}) drew over the mark instead of clearing a gap in it"
        )

    @pytest.mark.parametrize("glyph", GLYPHS)
    def test_glyph_assets_ship_and_render(self, glyph):
        image = tray.create_status_image("healthy", glyph=glyph)
        assert image.size == (tray.ICON_SIZE, tray.ICON_SIZE)
        assert image.mode == "RGBA"

    @pytest.mark.parametrize("glyph", GLYPHS)
    def test_healthy_draws_no_badge_at_all(self, glyph):
        """The rule the whole design rests on, and the one most likely to be
        undone by accident: a running node decorates nothing, so the icon is the
        bare mark pixel for pixel."""
        image = tray.create_status_image("healthy", glyph=glyph)
        assert list(image.getdata()) == list(self._bare_glyph(glyph).getdata())

    @pytest.mark.parametrize("status", ["starting", "down", "update"])
    @pytest.mark.parametrize("glyph", GLYPHS)
    def test_every_other_status_draws_something(self, status, glyph):
        image = tray.create_status_image(status, glyph=glyph)
        assert list(image.getdata()) != list(self._bare_glyph(glyph).getdata())

    def test_the_states_are_four_different_icons(self):
        drawn = {
            status: tuple(tray.create_status_image(status, glyph="topos_white.png").getdata())
            for status in ("healthy", "starting", "down", "update")
        }
        assert len(set(drawn.values())) == 4, "two states render the same icon"

    @pytest.mark.parametrize(
        "status,phase",
        [
            ("healthy", None),
            ("starting", None),
            ("down", None),
            ("update", None),
            ("starting", 0.3),
        ],
    )
    @pytest.mark.parametrize("glyph", GLYPHS)
    def test_every_badge_is_monochrome(self, status, phase, glyph):
        """Meaning stopped being carried by hue when the coloured dots went, so
        one coloured pixel is the status light back in through the side door.
        Read off the rendered bitmap rather than grepping the source, which
        would miss a tint applied some other way."""
        image = tray.create_status_image(status, glyph=glyph, phase=phase)
        for pixel in image.getdata():
            r, g, b, a = pixel
            if a == 0:
                continue
            assert r == g == b, f"{status}/{glyph} has a coloured pixel: {pixel}"

    def test_unknown_status_asks_for_attention_rather_than_claiming_health(self):
        """An unrecognised state is not a claim that all is well — it gets the
        exclamation point, same as starting."""
        unknown = tray.create_status_image("nonsense", glyph="topos_white.png")
        starting = tray.create_status_image("starting", glyph="topos_white.png")
        assert list(unknown.getdata()) == list(starting.getdata())
        assert list(unknown.getdata()) != list(self._bare_glyph("topos_white.png").getdata())

    def test_the_update_arrow_points_down(self):
        """It once pointed UP: the badge was ported from AppKit, where y grows
        upward, to PIL, where it does not, and the arrowhead's apex landed at
        the wrong end. Nothing else here would notice.

        Measured as how far ink reaches BELOW the badge's centre. Two earlier
        versions of this check did not work and both passed with the apex
        flipped: comparing the widest row to the lowest (an up arrow and a down
        arrow have their widest row in the same place), and weighing mass below
        the centre against mass above it (the shaft outweighs the head, 8px to
        4px, so the correct arrow failed that one too). Both were found by
        flipping the apex and watching the test pass.
        """
        image = tray.create_status_image("update", glyph="topos_white.png")
        left, top, right, bottom = tray._badge_ring(tray.ICON_SIZE)
        cx, cy = (left + right) / 2, (top + bottom) / 2
        # Only the symbol, and masked to a CIRCLE. `ImageDraw.ellipse` strokes
        # its outline INSIDE the box, so the ring's inner edge cuts through the
        # corners of a square scan box — which put ring pixels in the sample and
        # made the flipped arrow measure identically to the correct one.
        u = tray.ICON_SIZE / 22.0
        reach = 2.4 * u
        pixels = image.load()
        lowest = None
        for y in range(round(cy - reach), round(cy + reach) + 1):
            for x in range(round(cx - reach), round(cx + reach) + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 > reach**2:
                    continue
                # 60, not 120: the arrowhead ends in a point, and after the
                # downsample its last visible row sits at alpha ~106. At 120
                # the tip is discarded and the correct arrow measures 0.72pt.
                if pixels[x, y][3] > 60:
                    lowest = y if lowest is None else max(lowest, y)
        assert lowest is not None, "the update badge drew no arrow"
        reach_below = (lowest - cy) / u
        assert reach_below > 1.0, (
            "the arrowhead is the lowest thing in the badge and it belongs "
            f"BELOW the centre — ink reaches only {reach_below:.2f}pt down"
        )


class TestTrayHealthHysteresis:
    """A busy node that answers /healthcheck late must not paint the tray red."""

    def test_single_miss_keeps_green(self):
        status, failures = tray.resolve_tray_health_status(
            probe_ok=False, consecutive_failures=0, current_status="healthy"
        )
        assert status == "healthy"
        assert failures == 1

    def test_second_consecutive_miss_goes_red(self):
        status, failures = tray.resolve_tray_health_status(
            probe_ok=False, consecutive_failures=1, current_status="healthy"
        )
        assert status == "down"
        assert failures == 2

    def test_success_clears_the_count_and_goes_green(self):
        status, failures = tray.resolve_tray_health_status(
            probe_ok=True, consecutive_failures=1, current_status="down"
        )
        assert status == "healthy"
        assert failures == 0

    def test_starting_does_not_flip_red_on_the_first_miss(self):
        status, failures = tray.resolve_tray_health_status(
            probe_ok=False, consecutive_failures=0, current_status="starting"
        )
        assert status == "starting"
        assert failures == 1

    def test_client_timeout_sits_above_the_database_probe(self):
        from topos.core.db_health import _PROBE_TIMEOUT_S

        assert tray.HEALTH_TIMEOUT_SECONDS > _PROBE_TIMEOUT_S
        assert tray.HEALTH_TIMEOUT_SECONDS >= 10.0
        assert tray.HEALTH_FAILURE_THRESHOLD == 2

    def test_poller_uses_hysteresis_and_the_raised_timeout(self):
        import inspect

        body = inspect.getsource(tray.ToposTray._poll_health)
        assert "resolve_tray_health_status(" in body
        assert "HEALTH_TIMEOUT_SECONDS" in body
        assert "timeout=3.0" not in body


class TestToposTray:
    def test_poll_host_rewrites_wildcard_bind(self):
        t = tray.ToposTray(
            host="0.0.0.0", port=DEFAULT_NODE_PORT, version="1.0.0", package_name="topos-node", on_quit=lambda: None
        )
        assert t.health_url == f"{DEFAULT_NODE_HTTP_URL}/healthcheck"
        assert t.docs_url == tray.TOPOS_DOCS_URL  # product docs, not the local API docs

    def test_specific_host_kept(self):
        t = tray.ToposTray(
            host="192.168.1.5", port=9100, version="1.0.0", package_name="topos-node", on_quit=lambda: None
        )
        assert t.health_url == "http://192.168.1.5:9100/healthcheck"


class TestQuitSemantics:
    """Quit means quit, attached or not — the design of record shared with the
    macOS shell (0.2.11). "Close Tray (node keeps running)" as the only exit
    stranded users whose tray attached after a crash or update restart."""

    def _tray(self, *, attached: bool) -> tray.ToposTray:
        return tray.ToposTray(
            host="127.0.0.1",
            port=DEFAULT_NODE_PORT,
            version="1.0.0",
            package_name="topos-node",
            on_quit=lambda: None,
            attached=attached,
        )

    def test_quit_is_always_the_primary_exit(self):
        for attached in (False, True):
            labels = self._tray(attached=attached)._menu_labels()
            assert "Quit Topos Node" in labels
            assert "Close Tray (node keeps running)" not in labels

    def test_tray_only_exit_appears_only_when_attached(self):
        assert "Close Tray Only (node keeps running)" in self._tray(attached=True)._menu_labels()
        assert (
            "Close Tray Only (node keeps running)"
            not in self._tray(attached=False)._menu_labels()
        )

    def test_quit_comes_before_the_tray_only_exit(self):
        labels = self._tray(attached=True)._menu_labels()
        assert labels.index("Quit Topos Node") < labels.index("Close Tray Only (node keeps running)")

    def test_attached_quit_stops_the_node_by_pid(self, monkeypatch):
        t = self._tray(attached=True)
        t.node_pid = 4242
        stopped = {}
        monkeypatch.setattr(tray.ToposTray, "_stop_node_by_pid", staticmethod(lambda pid: stopped.setdefault("pid", pid)))
        t._quit()
        assert stopped["pid"] == 4242

    def test_close_tray_only_never_touches_the_node(self, monkeypatch):
        t = self._tray(attached=True)
        t.node_pid = 4242
        monkeypatch.setattr(
            tray.ToposTray,
            "_stop_node_by_pid",
            staticmethod(lambda pid: (_ for _ in ()).throw(AssertionError("node was stopped"))),
        )
        t._close_tray_only()


class TestToposNameRow:
    def test_named_topos_gets_its_row(self):
        t = tray.ToposTray(
            host="127.0.0.1", port=DEFAULT_NODE_PORT, version="1.0.0", package_name="topos-node", on_quit=lambda: None
        )
        assert not any(l.startswith("Topos: ") for l in t._menu_labels())
        t.topos_name = "PersonalDB"
        assert "Topos: PersonalDB" in t._menu_labels()


class TestStartingSpinner:
    def test_phase_renders_distinct_frames(self):
        a = tray.create_status_image("starting", glyph="topos_white.png", phase=0.0)
        b = tray.create_status_image("starting", glyph="topos_white.png", phase=0.5)
        assert list(a.getdata()) != list(b.getdata()), "spinner frames must differ or nothing animates"

    def test_no_phase_keeps_the_static_badge(self):
        static1 = tray.create_status_image("starting", glyph="topos_white.png")
        static2 = tray.create_status_image("starting", glyph="topos_white.png")
        assert list(static1.getdata()) == list(static2.getdata())

    def test_the_spinner_is_not_the_static_badge(self):
        """A spinning tray and a waiting tray must not look identical, or the
        motion the state was given to prove is invisible."""
        spinning = tray.create_status_image("starting", glyph="topos_white.png", phase=0.0)
        still = tray.create_status_image("starting", glyph="topos_white.png")
        assert list(spinning.getdata()) != list(still.getdata())


class TestFailedUpdateIsVisible:
    """A failed update rendered nothing, so the menu fell back to "Update to
    vX" — indistinguishable from a click that never registered, which is
    exactly how the macOS bug was reported (2026-08-08)."""

    def _tray(self) -> tray.ToposTray:
        return tray.ToposTray(
            host="127.0.0.1", port=DEFAULT_NODE_PORT, version="1.3.6", package_name="topos-node", on_quit=lambda: None
        )

    def test_failure_is_named(self):
        t = self._tray()
        t.update = {"available": True, "latest": "1.3.7", "applying": False, "last_result": "failed"}
        labels = t._menu_labels()
        assert "Update failed — click to retry" in labels
        assert not any(l.startswith("Update to v") for l in labels)

    def test_success_and_applying_are_unchanged(self):
        t = self._tray()
        t.update = {"available": True, "latest": "1.3.7", "applying": True, "last_result": None}
        assert "Installing update…" in t._menu_labels()
        t.update = {"available": True, "latest": "1.3.7", "applying": False, "last_result": "success"}
        assert "Update installed — restart to finish" in t._menu_labels()


class TestSelectToposMenu:
    """Multi-Topos parity with the macOS shell (PLAN_MULTI_TOPOS_SWITCHING M5).

    The submenu itself needs a display; these pin the parts that do not —
    which profiles are offered, which are switchable, and the rule that a tray
    not owning the node must not offer to swap the database under it.
    """

    def _tray(self, *, attached: bool = False) -> tray.ToposTray:
        return tray.ToposTray(
            host="127.0.0.1",
            port=DEFAULT_NODE_PORT,
            version="1.3.15",
            package_name="topos-node",
            on_quit=lambda: None,
            attached=attached,
        )

    def _profiles(self, monkeypatch, entries):
        monkeypatch.setattr(tray.ToposTray, "_profiles", lambda self: entries)

    def test_profiles_are_listed_with_names_and_sizes(self, monkeypatch):
        from topos.profiles import ProfileInfo

        self._profiles(
            monkeypatch,
            [
                ProfileInfo("default", "PersonalDB", "/p/a", 2_800_000_000, active=True),
                ProfileInfo("work", "Work", "/p/b", 830_000_000),
            ],
        )
        labels = self._tray()._menu_labels()
        assert "Select Topos" in labels
        assert "PersonalDB (2.8 GB)" in labels
        assert "Work (0.8 GB)" in labels
        assert "New Topos…" in labels

    def test_unnamed_profile_falls_back_to_its_id(self, monkeypatch):
        from topos.profiles import ProfileInfo

        self._profiles(monkeypatch, [ProfileInfo("topos-20260812", None, "/p/a", 5_000_000)])
        assert "topos-20260812 (5 MB)" in self._tray()._menu_labels()

    def test_attached_tray_never_offers_to_switch(self, monkeypatch):
        from topos.profiles import ProfileInfo

        self._profiles(monkeypatch, [ProfileInfo("work", "Work", "/p/b", 1)])
        labels = self._tray(attached=True)._menu_labels()
        # It cannot restart a node it did not start; stopping one and moving
        # its database would leave the machine with nothing running.
        assert "Select Topos" not in labels
        assert "New Topos…" not in labels

    def test_profile_listing_failure_does_not_break_the_menu(self, monkeypatch):
        monkeypatch.setattr(
            tray.ToposTray,
            "_profiles",
            lambda self: (_ for _ in ()).throw(RuntimeError("disk gone")),
        )
        # _profiles swallows its own errors; assert the real one does too.
        t = self._tray()
        monkeypatch.undo()
        monkeypatch.setattr(
            "topos.profiles.list_profiles",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk gone")),
        )
        assert t._profiles() == []
        assert "Quit Topos Node" in t._menu_labels()

    def test_click_queues_the_action_rather_than_doing_it(self, monkeypatch):
        t = self._tray()
        closed = {}
        monkeypatch.setattr(tray.ToposTray, "_close_tray_only", lambda self: closed.setdefault("x", True))
        monkeypatch.setattr(tray.ToposTray, "_notify", lambda self, m: None)
        t._switch_profile("work")
        # The node to stop IS this process, so the click may only queue.
        assert t.pending_profile_action == ("switch", "work")
        assert closed["x"] is True

    def test_new_topos_queues_its_own_action(self, monkeypatch):
        t = self._tray()
        monkeypatch.setattr(tray.ToposTray, "_close_tray_only", lambda self: None)
        monkeypatch.setattr(tray.ToposTray, "_notify", lambda self, m: None)
        t._new_topos()
        assert t.pending_profile_action == ("new", "")


class TestApplyPendingProfileAction:
    def test_switch_asks_for_a_restart(self, monkeypatch):
        monkeypatch.setattr(
            "topos.profiles.switch_profile",
            lambda pid: {"activated": pid, "archived_as": "old"},
        )
        assert tray.apply_pending_profile_action(("switch", "work")) is True

    def test_new_does_not_restart_into_an_unbound_node(self, monkeypatch):
        monkeypatch.setattr(
            "topos.profiles.new_profile", lambda: {"archived": True, "archived_as": "personal"}
        )
        # A node with no key exits in a second; relaunching would bury the
        # pairing instructions under an error.
        assert tray.apply_pending_profile_action(("new", "")) is False

    def test_a_refused_switch_never_restarts(self, monkeypatch):
        from topos.profiles import ProfileError

        monkeypatch.setattr(
            "topos.profiles.switch_profile",
            lambda pid: (_ for _ in ()).throw(ProfileError("node is running")),
        )
        assert tray.apply_pending_profile_action(("switch", "work")) is False


class TestReexecStopsRebuildChildren:
    def test_rebuild_children_are_stopped_before_execv(self, monkeypatch):
        """execv keeps this pid, so a graph rebuild child's parent watch never
        fires, and the new image has an empty child registry: a child left
        running would hold the rebuild lock against the node it becomes."""
        from topos.features.entities import rebuild_subprocess

        order = []
        monkeypatch.setattr(
            rebuild_subprocess, "stop_rebuild_children", lambda *a, **k: order.append("stop") or 0
        )
        monkeypatch.setattr(tray.os, "execv", lambda *a: order.append("execv"))
        tray._reexec_topos_node()
        assert order == ["stop", "execv"]


class TestTrayQuitStopsRebuildChildren:
    def test_quit_stops_rebuild_children_before_uvicorn_drains(self, monkeypatch):
        """No tray exit is a signal the node's hooks see (uvicorn runs off the
        main thread here), and uvicorn drains an in-flight rebuild request
        before shutdown_event — so the tray's own stop path must end the child."""
        import time

        import uvicorn

        from topos.features.entities import rebuild_subprocess

        order = []

        class FakeServer:
            def __init__(self, config):
                self.should_exit = False

            def run(self):
                while not self.should_exit:
                    time.sleep(0.01)
                order.append("server-exit")

        class FakeTray:
            pending_profile_action = None

            def __init__(self, **kwargs):
                self._on_quit = kwargs["on_quit"]

            def run(self):
                self._on_quit()  # what ToposTray.run's finally does on Quit

        monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: None)
        monkeypatch.setattr(uvicorn, "Server", FakeServer)
        monkeypatch.setattr(tray, "ToposTray", FakeTray)
        monkeypatch.setattr(
            rebuild_subprocess, "signal_rebuild_children", lambda *a, **k: order.append("signal") or 0
        )
        tray.serve_with_tray(
            object(), host="127.0.0.1", port=0, log_config=None, version="t", package_name="p"
        )
        assert order == ["signal", "server-exit"]
