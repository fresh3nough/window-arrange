#!/usr/bin/env python3
"""Unit tests for logical-scale layout planning.

Run: python3 -m unittest tests.test_layout -v
"""

from __future__ import annotations

import math
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
        x0, y0, x1, y1, aw, ah = work_area(mon_hidpi(), outer=12)
        # reserved top 26 + outer 12
        self.assertEqual(x0, 12)
        self.assertEqual(y0, 38)
        self.assertEqual(x1, 1440 - 12)
        self.assertEqual(y1, 960 - 12)
        self.assertEqual(aw, x1 - x0)
        self.assertEqual(ah, y1 - y0)


class TestPlanBounds(unittest.TestCase):
    def _assert_in_logical(self, plan, mon, outer=12):
        _x0, _y0, x1, y1, _aw, _ah = work_area(mon, outer)
        mx, my, lw, lh, _ = logical_monitor_box(mon)
        for item in plan:
            self.assertGreaterEqual(item["x"], mx, msg=item)
            self.assertGreaterEqual(item["y"], my, msg=item)
            self.assertLessEqual(item["x"] + item["w"], mx + lw + 1, msg=item)
            self.assertLessEqual(item["y"] + item["h"], my + lh + 1, msg=item)
            # Must not spill past padded right/bottom by more than clamp slack
            self.assertLessEqual(item["x"] + item["w"], x1 + 1, msg=item)
            self.assertLessEqual(item["y"] + item["h"], y1 + 1, msg=item)
            self.assertGreater(item["w"], 0)
            self.assertGreater(item["h"], 0)

    def test_hidpi_five_windows_fit(self):
        mon = mon_hidpi()
        clients = [
            fake_client("0x1", "chromium"),
            fake_client("0x2", "code"),
            fake_client("0x3", "foot"),
            fake_client("0x4", "1password"),
            fake_client("0x5", "goose", fullscreen=2),
        ]
        plan = build_plan(
            [mon], clients, {"id": 1},
            gap=8, outer=12, auto_adapt=False,
        )
        self.assertEqual(len(plan), 5)
        self._assert_in_logical(plan, mon, outer=12)
        # Old bug: physical x of 1504+ on a 1440-wide logical screen
        for item in plan:
            self.assertLess(item["x"], 1440, msg=f"off-screen x: {item}")
            self.assertLess(item["y"], 960, msg=f"off-screen y: {item}")

    def test_scale1_maclike_fits(self):
        mon = mon_scale1_maclike()
        clients = [
            fake_client("0x1", "chromium"),
            fake_client("0x2", "code"),
            fake_client("0x3", "foot"),
        ]
        plan = build_plan(
            [mon], clients, {"id": 1},
            gap=8, outer=12, auto_adapt=False,
        )
        self.assertEqual(len(plan), 3)
        self._assert_in_logical(plan, mon, outer=12)
        for item in plan:
            self.assertLess(item["x"] + item["w"], 1512 + 1)

    def test_phone_strip_on_right(self):
        mon = mon_hidpi()
        clients = [
            fake_client("0x1", "foot"),
            fake_client("0x2", "scrcpy", title="Pixel"),
        ]
        plan = build_plan(
            [mon], clients, {"id": 1},
            gap=8, outer=12, phone_w=360, phone_h=800, auto_adapt=False,
        )
        roles = {p["role"]: p for p in plan}
        self.assertIn("phone", roles)
        self.assertIn("grid", roles)
        phone = roles["phone"]
        grid = roles["grid"]
        # Width is min(requested, ~20% of area); must stay portrait and on-screen.
        self.assertGreaterEqual(phone["w"], 240)
        self.assertLessEqual(phone["w"], 360)
        self.assertGreaterEqual(phone["h"] / max(phone["w"], 1), 1.7)
        # Phone right edge on padded right; grid sits to its left
        self.assertGreater(phone["x"], grid["x"])
        self.assertEqual(phone["x"] + phone["w"], work_area(mon, 12)[2])
        self.assertLessEqual(grid["x"] + grid["w"], phone["x"])

    def test_auto_adapt_changes_with_area(self):
        small = adaptive_defaults(800, 500)
        large = adaptive_defaults(2000, 1200)
        self.assertLessEqual(small["gap"], large["gap"])
        self.assertLessEqual(small["outer"], large["outer"])
        self.assertGreater(small["phone_w"], 0)

    def test_empty_workspace(self):
        plan = build_plan([mon_hidpi()], [], {"id": 1})
        self.assertEqual(plan, [])

    def test_choose_grid_nonzero(self):
        cols, rows, cw, rh = choose_grid(4, 1000, 700, 8)
        self.assertGreater(cols, 0)
        self.assertGreater(rows, 0)
        self.assertGreater(cw, 0)
        self.assertGreater(rh, 0)
        self.assertEqual(cols * rows >= 4, True)


class TestRegressionPhysicalVsLogical(unittest.TestCase):
    """The Mac-perfect / laptop-broken case: same physical math without /scale."""

    def test_unscaled_would_overflow(self):
        mon = mon_hidpi()
        # Simulate old bug: treat width/height as logical without dividing.
        physical_w = mon["width"]  # 2256
        self.assertGreater(physical_w, 1440)
        clients = [fake_client(f"0x{i}", f"app{i}") for i in range(4)]
        plan = build_plan(
            [mon], clients, {"id": 1}, gap=8, outer=12, auto_adapt=False
        )
        max_right = max(p["x"] + p["w"] for p in plan)
        self.assertLessEqual(max_right, 1440 - 12 + 1)
        # And the bad coordinate from the live dry-run must not reappear
        self.assertTrue(all(p["x"] < 1504 for p in plan))


if __name__ == "__main__":
    unittest.main()
