#!/usr/bin/env python3
"""Unit tests for logical-scale layout planning.

Run: python3 -m unittest tests.test_layout -v
"""

from __future__ import annotations

import os
import sys
import unittest

# Repo root on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from layout import (  # noqa: E402
    adaptive_defaults,
    build_plan,
    choose_grid,
    logical_monitor_box,
    pack_window_cells,
    place_grid,
    rects_overlap,
    split_axis,
    toolkit_min_size,
    work_area,
)


def mon_hidpi() -> dict:
    """Framework-like panel: 2256x1504 @ 1.5667 → ~1440x960 logical."""
    return {
        "width": 2256,
        "height": 1504,
        "x": 0,
        "y": 0,
        "scale": 1.5666667,
        "reserved": [0, 26, 0, 0],
        "focused": True,
    }


def mon_scale1_maclike() -> dict:
    """Scale-1 logical desktop similar to a MacBook logical canvas."""
    return {
        "width": 1512,
        "height": 982,
        "x": 0,
        "y": 0,
        "scale": 1.0,
        "reserved": [0, 26, 0, 0],
        "focused": True,
    }


def fake_client(addr: str, cls: str, ws: int = 1, **extra) -> dict:
    base = {
        "address": addr,
        "class": cls,
        "title": cls,
        "mapped": True,
        "hidden": False,
        "floating": True,
        "fullscreen": 0,
        "pinned": False,
        "workspace": {"id": ws, "name": str(ws)},
    }
    base.update(extra)
    return base


def assert_no_plan_overlap(test: unittest.TestCase, plan: list) -> None:
    rects = [(p["x"], p["y"], p["w"], p["h"], p["class"]) for p in plan]
    for i, a in enumerate(rects):
        for b in rects[i + 1 :]:
            if rects_overlap(a[:4], b[:4]):
                test.fail(f"overlap {a} vs {b}")


class TestLogicalMonitor(unittest.TestCase):
    def test_hidpi_divides_by_scale(self):
        mx, my, lw, lh, scale = logical_monitor_box(mon_hidpi())
        self.assertEqual((mx, my), (0, 0))
        self.assertEqual(lw, 1440)
        self.assertEqual(lh, 960)
        self.assertAlmostEqual(scale, 1.5666667, places=5)

    def test_scale1_unchanged(self):
        _mx, _my, lw, lh, scale = logical_monitor_box(mon_scale1_maclike())
        self.assertEqual((lw, lh, scale), (1512, 982, 1.0))

    def test_work_area_respects_reserved_and_outer(self):
        x0, y0, x1, y1, aw, ah = work_area(mon_hidpi(), outer=16)
        self.assertEqual(x0, 16)
        self.assertEqual(y0, 42)  # reserved top 26 + outer 16
        self.assertEqual(x1, 1440 - 16)
        self.assertEqual(y1, 960 - 16)
        self.assertEqual(aw, x1 - x0)
        self.assertEqual(ah, y1 - y0)


class TestSplitAxis(unittest.TestCase):
    def test_exact_cover_with_gaps(self):
        bands = split_axis(1000, 3, 12)
        self.assertEqual(len(bands), 3)
        # last end == total
        last = bands[-1]
        self.assertEqual(last[0] + last[1], 1000)
        # gaps between
        for i in range(len(bands) - 1):
            end = bands[i][0] + bands[i][1]
            nxt = bands[i + 1][0]
            self.assertEqual(nxt - end, 12)

    def test_sizes_differ_by_at_most_one(self):
        bands = split_axis(1416, 3, 12)
        sizes = [b[1] for b in bands]
        self.assertLessEqual(max(sizes) - min(sizes), 1)


class TestPlaceGrid(unittest.TestCase):
    def test_five_cells_no_overlap_hidpi(self):
        # Match adaptive work area on this laptop-ish canvas.
        x0, y0, x1, y1, aw, ah = work_area(mon_hidpi(), outer=16)
        gap = 12
        # Chromium's 500px floor forces 2-col stacks (~698px half-width).
        cells = place_grid(5, x0, y0, aw, ah, gap, min_cell_w=500)
        self.assertEqual(len(cells), 5)
        for i, a in enumerate(cells):
            for b in cells[i + 1 :]:
                self.assertFalse(rects_overlap(a, b), f"{a} vs {b}")
        widths = sorted(w for _x, _y, w, _h in cells)
        heights = sorted(h for _x, _y, _w, h in cells)
        for _x, _y, w, h in cells:
            self.assertGreaterEqual(w, 600)  # half of ~1408
            self.assertLessEqual(_x + w, x1)
            self.assertLessEqual(_y + h, y1)
        # 2-stack side must clear Goose min height (~400).
        self.assertGreaterEqual(heights[-1], 400)
        # 3-stack side is shorter but still usable for terminals.
        self.assertGreaterEqual(heights[0], 250)

    def test_choose_grid_prefers_two_rows_for_five(self):
        cols, rows = choose_grid(5, 1400, 900, 12)
        # Uniform chooser may still say 2 rows; place_grid then upgrades to stacks.
        self.assertGreaterEqual(rows, 1)
        self.assertGreaterEqual(cols, 1)

    def test_six_windows_stack_when_chromium_min_blocks_three_col(self):
        # 3x2 on ~1408-wide yields ~461px cells < Chromium 500 → must stack.
        cells = place_grid(6, 16, 42, 1408, 902, 12, min_cell_w=500)
        self.assertEqual(len(cells), 6)
        widths = [w for _x, _y, w, _h in cells]
        # Two columns, each ≥ Chromium min.
        self.assertTrue(all(w >= 500 for w in widths), widths)
        for i, a in enumerate(cells):
            for b in cells[i + 1 :]:
                self.assertFalse(rects_overlap(a, b), f"{a} vs {b}")

    def test_pack_mixed_toolkit_mins_no_overlap(self):
        x0, y0, x1, y1, aw, ah = work_area(mon_hidpi(), outer=16)
        wins = [
            fake_client("0x1", "chromium"),
            fake_client("0x2", "goose"),
            fake_client("0x3", "foot"),
            fake_client("0x4", "org.gnome.Nautilus"),
            fake_client("0x5", "org.gnome.DiskUtility"),
            fake_client("0x6", "chromium", title="about:blank"),
        ]
        pairs = pack_window_cells(wins, x0, y0, aw, ah, 12)
        self.assertEqual(len(pairs), 6)
        cells = [cell for _w, cell in pairs]
        for i, a in enumerate(cells):
            for b in cells[i + 1 :]:
                self.assertFalse(rects_overlap(a, b), f"{a} vs {b}")
        # Wide apps land in cells at least as wide as their toolkit min when possible.
        for win, (x, y, w, h) in pairs:
            mw, mh = toolkit_min_size(win)
            if mw >= 480:
                self.assertGreaterEqual(w, 500, msg=(win["class"], w, mw))
            # Goose gets a tall cell when packing can spare it.
            if win["class"] == "goose":
                self.assertGreaterEqual(h, 350, msg=(h, mh))


class TestPlanBounds(unittest.TestCase):
    def _assert_in_logical(self, plan, mon, outer=16):
        _x0, _y0, x1, y1, _aw, _ah = work_area(mon, outer)
        mx, my, lw, lh, _ = logical_monitor_box(mon)
        for item in plan:
            self.assertGreaterEqual(item["x"], mx, msg=item)
            self.assertGreaterEqual(item["y"], my, msg=item)
            self.assertLessEqual(item["x"] + item["w"], mx + lw + 1, msg=item)
            self.assertLessEqual(item["y"] + item["h"], my + lh + 1, msg=item)
            self.assertLessEqual(item["x"] + item["w"], x1 + 1, msg=item)
            self.assertLessEqual(item["y"] + item["h"], y1 + 1, msg=item)
            self.assertGreater(item["w"], 0)
            self.assertGreater(item["h"], 0)

    def test_hidpi_five_windows_fit_no_overlap(self):
        mon = mon_hidpi()
        clients = [
            fake_client("0x1", "chromium"),
            fake_client("0x2", "code"),
            fake_client("0x3", "foot"),
            fake_client("0x4", "chrome-launchpad"),
            fake_client("0x5", "goose", fullscreen=2),
        ]
        plan = build_plan(
            [mon], clients, {"id": 1},
            gap=12, outer=16, auto_adapt=False,
        )
        self.assertEqual(len(plan), 5)
        self._assert_in_logical(plan, mon, outer=16)
        assert_no_plan_overlap(self, plan)
        for item in plan:
            self.assertLess(item["x"], 1440, msg=f"off-screen x: {item}")
            self.assertLess(item["y"], 960, msg=f"off-screen y: {item}")
            # 2-col stacks: half-width ≥ Chromium 500.
            self.assertGreaterEqual(item["w"], 500, msg=item)

    def test_hidpi_six_mixed_apps_no_overlap(self):
        mon = mon_hidpi()
        clients = [
            fake_client("0x1", "chromium"),
            fake_client("0x2", "code"),
            fake_client("0x3", "foot"),
            fake_client("0x4", "1password"),
            fake_client("0x5", "goose"),
            fake_client("0x6", "TUI.float"),
        ]
        plan = build_plan(
            [mon], clients, {"id": 1},
            gap=12, outer=16, auto_adapt=False,
        )
        self.assertEqual(len(plan), 6)
        self._assert_in_logical(plan, mon, outer=16)
        assert_no_plan_overlap(self, plan)
        # 1Password min_w is 784; on ~1440 logical that exceeds half-grid, so
        # packer still uses 2-col stacks (widest possible) and settle pins clamp.
        for item in plan:
            self.assertGreaterEqual(item["w"], 500, msg=item)
        widths = {p["class"]: p["w"] for p in plan}
        # Two-column stacks: every cell shares the half-width band.
        self.assertEqual(len(set(widths.values())), 1, msg=widths)
        op = next(p for p in plan if p["class"] == "1password")
        self.assertEqual(op.get("min_w"), 784, msg=op)
        # Goose (tall) should receive a weighted-taller cell when possible.
        goose = next(p for p in plan if p["class"] == "goose")
        self.assertGreaterEqual(goose["h"], 350, msg=goose)

    def test_1password_toolkit_min_width(self):
        """Live clamp is ~784 logical px; plan floor must match."""
        self.assertEqual(toolkit_min_size(fake_client("0x1", "1password")), (784, 400))
        self.assertEqual(toolkit_min_size(fake_client("0x2", "Bitwarden")), (784, 400))

    def test_wide_five_with_1password_no_overlap(self):
        """Ultrawide logical 2560x1080: 2+3 five-up must not collapse columns."""
        mon = {
            "name": "Virtual-1", "width": 5120, "height": 2160, "scale": 2.0,
            "x": 0, "y": 0, "reserved": [0, 26, 0, 0], "focused": True,
        }
        clients = [
            fake_client("0x1", "1password"),
            fake_client("0x2", "chromium"),
            fake_client("0x3", "code"),
            fake_client("0x4", "foot"),
            fake_client("0x5", "goose"),
        ]
        plan = build_plan([mon], clients, {"id": 1}, gap=12, outer=16, auto_adapt=False)
        self.assertEqual(len(plan), 5)
        assert_no_plan_overlap(self, plan)
        op = next(p for p in plan if p["class"] == "1password")
        self.assertGreaterEqual(op["w"], 784, msg=op)
        self.assertGreaterEqual(op["h"], 400, msg=op)

    def test_wide_seven_stacks_tall_mins(self):
        """Seven apps on ultrawide: 3-col stacks keep tall mins without overlap."""
        mon = {
            "name": "Virtual-1", "width": 5120, "height": 2160, "scale": 2.0,
            "x": 0, "y": 0, "reserved": [0, 26, 0, 0], "focused": True,
        }
        clients = [
            fake_client("0x1", "1password"),
            fake_client("0x2", "chromium"),
            fake_client("0x3", "code"),
            fake_client("0x4", "foot"),
            fake_client("0x5", "goose"),
            fake_client("0x6", "foot"),
            fake_client("0x7", "TUI.float"),
        ]
        plan = build_plan([mon], clients, {"id": 1}, gap=12, outer=16, auto_adapt=False)
        self.assertEqual(len(plan), 7)
        assert_no_plan_overlap(self, plan)
        op = next(p for p in plan if p["class"] == "1password")
        self.assertGreaterEqual(op["w"], 784, msg=op)
        self.assertGreaterEqual(op["h"], 400, msg=op)
        goose = next(p for p in plan if p["class"] == "goose")
        self.assertGreaterEqual(goose["h"], 400, msg=goose)

    def test_hidpi_files_chrome_goose_foot_disks(self):
        """Exact user mix: Nautilus + 2×Chromium + Goose + foot + Disks."""
        mon = mon_hidpi()
        clients = [
            fake_client("0x1", "org.gnome.Nautilus", title="Home"),
            fake_client("0x2", "chromium", title="LinkedIn"),
            fake_client("0x3", "goose"),
            fake_client("0x4", "foot", title="desktop"),
            fake_client("0x5", "org.gnome.DiskUtility", title="Disks"),
            fake_client("0x6", "chromium", title="about:blank"),
        ]
        plan = build_plan(
            [mon], clients, {"id": 1},
            gap=12, outer=16, auto_adapt=False,
        )
        self.assertEqual(len(plan), 6)
        self._assert_in_logical(plan, mon, outer=16)
        assert_no_plan_overlap(self, plan)
        by_cls = {}
        for p in plan:
            by_cls.setdefault(p["class"], []).append(p)
        for chrome in by_cls["chromium"]:
            self.assertGreaterEqual(chrome["w"], 500, msg=chrome)
        self.assertGreaterEqual(by_cls["goose"][0]["h"], 350)

    def test_scale1_maclike_fits(self):
        mon = mon_scale1_maclike()
        clients = [
            fake_client("0x1", "chromium"),
            fake_client("0x2", "code"),
            fake_client("0x3", "foot"),
            fake_client("0x4", "basecamp"),
            fake_client("0x5", "goose"),
        ]
        plan = build_plan(
            [mon], clients, {"id": 1},
            gap=12, outer=16, auto_adapt=False,
        )
        self.assertEqual(len(plan), 5)
        self._assert_in_logical(plan, mon, outer=16)
        assert_no_plan_overlap(self, plan)

    def test_phone_strip_on_right(self):
        mon = mon_hidpi()
        clients = [
            fake_client("0x1", "foot"),
            fake_client("0x2", "scrcpy", title="Pixel"),
        ]
        plan = build_plan(
            [mon], clients, {"id": 1},
            gap=12, outer=16, phone_w=360, phone_h=800, auto_adapt=False,
        )
        roles = {p["role"]: p for p in plan}
        self.assertIn("phone", roles)
        self.assertIn("grid", roles)
        phone = roles["phone"]
        grid = roles["grid"]
        self.assertGreaterEqual(phone["w"], 240)
        self.assertLessEqual(phone["w"], 360)
        self.assertGreaterEqual(phone["h"] / max(phone["w"], 1), 1.7)
        self.assertGreater(phone["x"], grid["x"])
        self.assertEqual(phone["x"] + phone["w"], work_area(mon, 16)[2])
        self.assertLessEqual(grid["x"] + grid["w"], phone["x"])
        assert_no_plan_overlap(self, plan)

    def test_auto_adapt_roomier_than_before(self):
        d = adaptive_defaults(1416, 910)
        self.assertGreaterEqual(d["gap"], 10)
        self.assertGreaterEqual(d["outer"], 14)

    def test_auto_adapt_changes_with_area(self):
        small = adaptive_defaults(800, 500)
        large = adaptive_defaults(2000, 1200)
        self.assertLessEqual(small["gap"], large["gap"])
        self.assertLessEqual(small["outer"], large["outer"])
        self.assertGreater(small["phone_w"], 0)

    def test_empty_workspace(self):
        plan = build_plan([mon_hidpi()], [], {"id": 1})
        self.assertEqual(plan, [])


class TestRegressionPhysicalVsLogical(unittest.TestCase):
    def test_unscaled_would_overflow(self):
        mon = mon_hidpi()
        clients = [fake_client(f"0x{i}", f"app{i}") for i in range(4)]
        plan = build_plan(
            [mon], clients, {"id": 1}, gap=12, outer=16, auto_adapt=False
        )
        max_right = max(p["x"] + p["w"] for p in plan)
        self.assertLessEqual(max_right, 1440 - 16 + 1)
        self.assertTrue(all(p["x"] < 1504 for p in plan))
        assert_no_plan_overlap(self, plan)


if __name__ == "__main__":
    unittest.main()
