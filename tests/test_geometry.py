#!/usr/bin/env python3
"""Unit tests for neighbor-aware resize + swap geometry."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from geometry import (  # noqa: E402
    find_neighbors,
    hit_test_handle,
    resize_edge,
    resize_handle,
    swap_windows,
    window_at,
)


def tile(i, x, y, w, h):
    return {"id": i, "address": f"0x{i}", "class": f"app{i}", "x": x, "y": y, "w": w, "h": h}


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
        # D is not a right-neighbor of A
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

    def test_right_edge_grows_shrinks_neighbor(self):
        out = resize_edge(self.wins, 0, "right", 20, gap=self.gap, min_w=40, min_h=40)
        a, b = out[0], out[1]
        self.assertEqual(a["w"], 120)
        self.assertEqual(b["x"], 132)  # 112 + 20
        self.assertEqual(b["w"], 80)
        # Gap preserved: a.right + gap == b.left
        self.assertEqual(a["x"] + a["w"] + self.gap, b["x"])
        # Untouched tiles stay put
        self.assertEqual(out[2]["x"], 0)
        self.assertEqual(out[3]["w"], 100)

    def test_left_edge_of_right_tile(self):
        out = resize_edge(self.wins, 1, "left", -20, gap=self.gap, min_w=40, min_h=40)
        # Moving B's left edge left by 20 grows B, shrinks A
        a, b = out[0], out[1]
        self.assertEqual(b["x"], 92)
        self.assertEqual(b["w"], 120)
        self.assertEqual(a["w"], 80)
        self.assertEqual(a["x"] + a["w"] + self.gap, b["x"])

    def test_bottom_edge(self):
        out = resize_edge(self.wins, 0, "bottom", 15, gap=self.gap, min_w=40, min_h=40)
        a, c = out[0], out[2]
        self.assertEqual(a["h"], 115)
        self.assertEqual(c["y"], 127)
        self.assertEqual(c["h"], 85)
        self.assertEqual(a["y"] + a["h"] + self.gap, c["y"])

    def test_min_size_clamp(self):
        out = resize_edge(self.wins, 0, "right", 90, gap=self.gap, min_w=40, min_h=40)
        # Neighbor starts at 100; can only shrink by 60
        self.assertEqual(out[0]["w"], 160)
        self.assertEqual(out[1]["w"], 40)

    def test_two_neighbors_on_same_edge(self):
        # A tall left tile abutting two stacked right tiles
        wins = [
            tile(0, 0, 0, 100, 212),          # left full height
            tile(1, 112, 0, 100, 100),        # top-right
            tile(2, 112, 112, 100, 100),      # bottom-right
        ]
        out = resize_edge(wins, 0, "right", 10, gap=12, min_w=40, min_h=40)
        self.assertEqual(out[0]["w"], 110)
        self.assertEqual(out[1]["x"], 122)
        self.assertEqual(out[1]["w"], 90)
        self.assertEqual(out[2]["x"], 122)
        self.assertEqual(out[2]["w"], 90)

    def test_no_neighbor_still_resizes_primary(self):
        wins = [tile(0, 50, 50, 200, 200)]
        out = resize_edge(wins, 0, "right", 30, gap=12, min_w=40, min_h=40)
        self.assertEqual(out[0]["w"], 230)
        self.assertEqual(out[0]["x"], 50)


class TestResizeHandle(unittest.TestCase):
    def test_corner_combines_axes(self):
        wins = [
            tile(0, 0, 0, 100, 100),
            tile(1, 112, 0, 100, 100),
            tile(2, 0, 112, 100, 100),
            tile(3, 112, 112, 100, 100),
        ]
        out = resize_handle(wins, 0, "bottom-right", 10, 10, gap=12, min_w=40, min_h=40)
        self.assertEqual(out[0]["w"], 110)
        self.assertEqual(out[0]["h"], 110)
        self.assertEqual(out[1]["w"], 90)
        self.assertEqual(out[2]["h"], 90)


class TestSwap(unittest.TestCase):
    def test_swap_exchanges_geometry_keeps_identity(self):
        wins = [
            tile(0, 0, 0, 100, 80),
            tile(1, 200, 50, 150, 120),
        ]
        out = swap_windows(wins, 0, 1)
        self.assertEqual(out[0]["address"], "0x0")
        self.assertEqual(out[1]["address"], "0x1")
        self.assertEqual((out[0]["x"], out[0]["y"], out[0]["w"], out[0]["h"]), (200, 50, 150, 120))
        self.assertEqual((out[1]["x"], out[1]["y"], out[1]["w"], out[1]["h"]), (0, 0, 100, 80))

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
            tile(1, 50, 50, 100, 100),  # overlaps
        ]
        # Topmost is index 1
        self.assertEqual(window_at(wins, 60, 60), 1)
        self.assertEqual(window_at(wins, 60, 60, exclude=1), 0)


if __name__ == "__main__":
    unittest.main()
