#!/usr/bin/env python3
"""Unit tests for neighbor-aware resize + swap geometry."""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from geometry import (  # noqa: E402
    any_overlaps,
    find_neighbors,
    hit_test_handle,
    layout_is_valid,
    resize_edge,
    resize_handle,
    swap_windows,
    window_at,
)


def tile(i, x, y, w, h):
    return {
        "id": i,
        "address": f"0x{i}",
        "class": f"app{i}",
        "x": x,
        "y": y,
        "w": w,
        "h": h,
    }


class TestNeighbors(unittest.TestCase):
    def setUp(self):
        # 2x2 grid with 12px gap
        # A(0,0,100,100) B(112,0,100,100)
        # C(0,112,100,100) D(112,112,100,100)
        self.wins = [
            tile(0, 0, 0, 100, 100),
            tile(1, 112, 0, 100, 100),
            tile(2, 0, 112, 100, 100),
            tile(3, 112, 112, 100, 100),
        ]
        self.gap = 12

    def test_right_neighbor(self):
        self.assertEqual(find_neighbors(self.wins, 0, "right", gap=self.gap), [1])

    def test_left_neighbor(self):
        self.assertEqual(find_neighbors(self.wins, 1, "left", gap=self.gap), [0])

    def test_bottom_neighbor(self):
        self.assertEqual(find_neighbors(self.wins, 0, "bottom", gap=self.gap), [2])

    def test_top_neighbor(self):
        self.assertEqual(find_neighbors(self.wins, 2, "top", gap=self.gap), [0])

    def test_no_diagonal_neighbor(self):
        self.assertEqual(find_neighbors(self.wins, 0, "right", gap=self.gap), [1])
        self.assertNotIn(3, find_neighbors(self.wins, 0, "right", gap=self.gap))


class TestResizeEdge(unittest.TestCase):
    def setUp(self):
        self.wins = [
            tile(0, 0, 0, 100, 100),
            tile(1, 112, 0, 100, 100),
            tile(2, 0, 112, 100, 100),
            tile(3, 112, 112, 100, 100),
        ]
        self.gap = 12
        self.bounds = (0, 0, 224, 224)

    def test_right_edge_grows_shrinks_neighbor(self):
        out = resize_edge(self.wins, 0, "right", 20, gap=self.gap, min_w=40, min_h=40)
        a, b = out[0], out[1]
        self.assertEqual(a["w"], 120)
        self.assertEqual(b["x"], 132)  # 112 + 20
        self.assertEqual(b["w"], 80)
        self.assertEqual(a["x"] + a["w"] + self.gap, b["x"])
        self.assertEqual(out[2]["x"], 0)
        self.assertEqual(out[3]["w"], 100)
        self.assertFalse(any_overlaps(out))

    def test_left_edge_of_right_tile(self):
        out = resize_edge(self.wins, 1, "left", -20, gap=self.gap, min_w=40, min_h=40)
        a, b = out[0], out[1]
        self.assertEqual(b["x"], 92)
        self.assertEqual(b["w"], 120)
        self.assertEqual(a["w"], 80)
        self.assertEqual(a["x"] + a["w"] + self.gap, b["x"])
        self.assertFalse(any_overlaps(out))

    def test_bottom_edge(self):
        out = resize_edge(self.wins, 0, "bottom", 15, gap=self.gap, min_w=40, min_h=40)
        a, c = out[0], out[2]
        self.assertEqual(a["h"], 115)
        self.assertEqual(c["y"], 127)
        self.assertEqual(c["h"], 85)
        self.assertEqual(a["y"] + a["h"] + self.gap, c["y"])
        self.assertFalse(any_overlaps(out))

    def test_min_size_clamp(self):
        out = resize_edge(self.wins, 0, "right", 90, gap=self.gap, min_w=40, min_h=40)
        self.assertEqual(out[0]["w"], 160)
        self.assertEqual(out[1]["w"], 40)
        self.assertFalse(any_overlaps(out))

    def test_two_neighbors_on_same_edge(self):
        wins = [
            tile(0, 0, 0, 100, 212),
            tile(1, 112, 0, 100, 100),
            tile(2, 112, 112, 100, 100),
        ]
        out = resize_edge(wins, 0, "right", 10, gap=12, min_w=40, min_h=40)
        self.assertEqual(out[0]["w"], 110)
        self.assertEqual(out[1]["x"], 122)
        self.assertEqual(out[1]["w"], 90)
        self.assertEqual(out[2]["x"], 122)
        self.assertEqual(out[2]["w"], 90)
        self.assertFalse(any_overlaps(out))

    def test_no_neighbor_still_resizes_primary(self):
        wins = [tile(0, 50, 50, 200, 200)]
        out = resize_edge(
            wins, 0, "right", 30, gap=12, min_w=40, min_h=40, bounds=(0, 0, 400, 400)
        )
        self.assertEqual(out[0]["w"], 230)
        self.assertEqual(out[0]["x"], 50)

    def test_free_growth_stops_at_non_neighbor(self):
        # A left, C far right — no abutting neighbor. A cannot grow into C.
        wins = [
            tile(0, 0, 0, 100, 100),
            tile(1, 250, 0, 100, 100),
        ]
        out = resize_edge(
            wins, 0, "right", 200, gap=12, min_w=40, min_h=40, bounds=(0, 0, 400, 200)
        )
        # free space to obstacle: 250 - 12 - 100 = 138
        self.assertEqual(out[0]["w"], 238)
        self.assertEqual(out[1]["x"], 250)  # untouched
        self.assertFalse(any_overlaps(out))

    def test_bounds_clamp_does_not_shift_opposite_edge(self):
        wins = [tile(0, 50, 50, 100, 100), tile(1, 50, 200, 100, 100)]
        # Grow right past bounds — right edge stops at bounds, x stays 50.
        out = resize_edge(
            wins, 0, "right", 500, gap=12, min_w=40, min_h=40, bounds=(0, 0, 200, 400)
        )
        self.assertEqual(out[0]["x"], 50)
        self.assertEqual(out[0]["w"], 150)  # 200 - 50
        self.assertEqual(out[1]["y"], 200)
        self.assertFalse(any_overlaps(out))


class TestResizeHandle(unittest.TestCase):
    def test_corner_combines_axes(self):
        wins = [
            tile(0, 0, 0, 100, 100),
            tile(1, 112, 0, 100, 100),
            tile(2, 0, 112, 100, 100),
            tile(3, 112, 112, 100, 100),
        ]
        out = resize_handle(
            wins, 0, "bottom-right", 10, 10, gap=12, min_w=40, min_h=40
        )
        self.assertEqual(out[0]["w"], 110)
        self.assertEqual(out[0]["h"], 110)
        self.assertEqual(out[1]["w"], 90)
        self.assertEqual(out[2]["h"], 90)
        self.assertFalse(any_overlaps(out))


class TestNoOverlapStress(unittest.TestCase):
    """Multi-window successive resizes must never produce interior overlaps."""

    def _grid_3x2(self, gap=12):
        # 3-col × 2-row covering roughly 1400×900
        return [
            tile(0, 20, 40, 450, 420),
            tile(1, 482, 40, 450, 420),
            tile(2, 944, 40, 450, 420),
            tile(3, 20, 472, 450, 420),
            tile(4, 482, 472, 450, 420),
            tile(5, 944, 472, 450, 420),
        ], (20, 40, 1394, 892), gap

    def test_scripted_multi_resize_no_overlap(self):
        wins, bounds, gap = self._grid_3x2()
        ops = [
            (0, "right", 80, 0),
            (1, "bottom", 0, 60),
            (0, "bottom-right", 40, 50),
            (3, "right", 100, 0),
            (2, "left", -30, 0),
            (4, "top", 0, -40),
            (5, "left", -50, 0),
            (1, "right", 120, 0),
            (0, "bottom", 0, 80),
            (3, "top", 0, -25),
            (2, "bottom", 0, 90),
            (4, "right", 70, 0),
            (5, "top-left", -40, -30),
            (1, "bottom-left", -20, 40),
            (0, "top-right", 50, -10),
        ]
        cur = wins
        for idx, handle, dx, dy in ops:
            cur = resize_handle(
                cur,
                idx,
                handle,
                dx,
                dy,
                gap=gap,
                min_w=200,
                min_h=120,
                bounds=bounds,
            )
            self.assertTrue(
                layout_is_valid(cur, bounds, min_w=200, min_h=120),
                msg=f"invalid after {handle} on {idx}: {any_overlaps(cur)} {[ (w['x'],w['y'],w['w'],w['h']) for w in cur]}",
            )

    def test_random_stress_no_overlap(self):
        wins, bounds, gap = self._grid_3x2()
        rng = random.Random(1)
        handles = [
            "left",
            "right",
            "top",
            "bottom",
            "top-left",
            "top-right",
            "bottom-left",
            "bottom-right",
        ]
        cur = wins
        for _ in range(300):
            idx = rng.randrange(len(cur))
            handle = rng.choice(handles)
            dx = rng.randint(-100, 100)
            dy = rng.randint(-100, 100)
            cur = resize_handle(
                cur,
                idx,
                handle,
                dx,
                dy,
                gap=gap,
                min_w=200,
                min_h=120,
                bounds=bounds,
            )
            self.assertTrue(
                layout_is_valid(cur, bounds, min_w=200, min_h=120),
                msg=f"stress overlap: {any_overlaps(cur)}",
            )

    def test_five_window_workspace_like(self):
        # Mimic the live 5-window arrange layout proportions.
        gap = 12
        wins = [
            tile(0, 22, 48, 828, 497),
            tile(1, 862, 48, 828, 497),
            tile(2, 22, 557, 548, 400),
            tile(3, 582, 557, 548, 400),
            tile(4, 1142, 557, 548, 400),
        ]
        bounds = (22, 48, 1690, 957)
        rng = random.Random(42)
        cur = wins
        for _ in range(200):
            idx = rng.randrange(len(cur))
            handle = rng.choice(
                [
                    "left",
                    "right",
                    "top",
                    "bottom",
                    "top-left",
                    "top-right",
                    "bottom-left",
                    "bottom-right",
                ]
            )
            cur = resize_handle(
                cur,
                idx,
                handle,
                rng.randint(-80, 80),
                rng.randint(-80, 80),
                gap=gap,
                min_w=200,
                min_h=120,
                bounds=bounds,
            )
            self.assertTrue(layout_is_valid(cur, bounds, min_w=200, min_h=120))


class TestSwap(unittest.TestCase):
    def test_swap_exchanges_geometry_keeps_identity(self):
        wins = [
            tile(0, 0, 0, 100, 80),
            tile(1, 200, 50, 150, 120),
        ]
        out = swap_windows(wins, 0, 1)
        self.assertEqual(out[0]["address"], "0x0")
        self.assertEqual(out[1]["address"], "0x1")
        self.assertEqual(
            (out[0]["x"], out[0]["y"], out[0]["w"], out[0]["h"]), (200, 50, 150, 120)
        )
        self.assertEqual(
            (out[1]["x"], out[1]["y"], out[1]["w"], out[1]["h"]), (0, 0, 100, 80)
        )

    def test_swap_self_noop(self):
        wins = [tile(0, 1, 2, 3, 4)]
        out = swap_windows(wins, 0, 0)
        self.assertEqual(out[0]["x"], 1)


class TestHitTest(unittest.TestCase):
    def setUp(self):
        self.wins = [tile(0, 100, 100, 200, 150)]

    def test_body(self):
        hit = hit_test_handle(self.wins, 200, 175)
        self.assertEqual(hit, (0, "body"))

    def test_right_edge(self):
        hit = hit_test_handle(self.wins, 295, 175, handle_px=10)
        self.assertEqual(hit, (0, "right"))

    def test_top_left_corner(self):
        hit = hit_test_handle(self.wins, 105, 105, corner_px=14)
        self.assertEqual(hit, (0, "top-left"))

    def test_miss(self):
        self.assertIsNone(hit_test_handle(self.wins, 10, 10))

    def test_window_at_exclude(self):
        wins = [
            tile(0, 0, 0, 100, 100),
            tile(1, 50, 50, 100, 100),
        ]
        self.assertEqual(window_at(wins, 60, 60), 1)
        self.assertEqual(window_at(wins, 60, 60, exclude=1), 0)


if __name__ == "__main__":
    unittest.main()
