#!/usr/bin/env python3
"""Unit tests for apply.lua generation (float enable + Omarchy tag strip)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apply import build_apply_lua  # noqa: E402


def plan_item(
    addr: str,
    cls: str,
    *,
    x: int = 10,
    y: int = 20,
    w: int = 800,
    h: int = 500,
    min_w: int = 200,
    min_h: int = 120,
    floating: bool = True,
    fullscreen: bool = False,
    pinned: bool = False,
) -> dict:
    return {
        "address": addr,
        "class": cls,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "min_w": min_w,
        "min_h": min_h,
        "floating": floating,
        "fullscreen": fullscreen,
        "pinned": pinned,
        "role": "grid",
        "mode": "float",
    }


class TestBuildApplyLua(unittest.TestCase):
    def test_float_uses_enable_not_toggle(self):
        """Bare float() toggles and tiles 1Password; action=enable is idempotent."""
        lua = build_apply_lua([plan_item("0x1", "1password")])
        self.assertIn("action = 'enable'", lua)
        # Conditional bare float was the old bug path.
        self.assertNotIn("need_float", lua)
        self.assertNotRegex(lua, r"window\.float\(\{ window = w \}\)")
        self.assertNotRegex(lua, r"window\.float\(\{ window = w, \}\)")

    def test_strips_floating_window_tag_before_and_after_float(self):
        lua = build_apply_lua([plan_item("0x1", "1password")])
        # prepare strips, enable-float, strip again; settle repeats.
        count = lua.count("tag = '-floating-window*'")
        self.assertGreaterEqual(count, 4, msg=lua)
        self.assertIn("tag = '-floating-window'", lua)

    def test_always_prepare_even_when_already_floating(self):
        """Already-floating clients must still run enable-float + tag strip."""
        clients = {
            "0xop": {
                "address": "0xop",
                "floating": True,
                "fullscreen": 0,
                "tags": ["floating-window*", "default-opacity*"],
            }
        }
        plan = [plan_item("0xop", "1password", floating=True)]
        lua = build_apply_lua(plan, clients)
        self.assertIn("apply('address:0xop'", lua)
        # No need_float gate — apply always calls prepare.
        self.assertIn("prepare(w, need_fs, need_pin)", lua)
        self.assertIn("action = 'enable'", lua)

    def test_settle_re_enables_float_and_strips_tags(self):
        lua = build_apply_lua([plan_item("0x1", "chromium")])
        # settle body must not be bare resize/move only.
        settle_idx = lua.index("local function settle")
        # Function runs until the matching bare `end` after place().
        settle_fn = lua[settle_idx : settle_idx + 700]
        self.assertIn("action = 'enable'", settle_fn)
        self.assertIn("-floating-window*", settle_fn)
        self.assertIn("place(w, x, y, rw, rh)", settle_fn)

    def test_soften_rule_per_class_including_1password(self):
        plan = [
            plan_item("0x1", "1password", min_w=480, min_h=400),
            plan_item("0x2", "chromium", min_w=500, min_h=200),
            plan_item("0x3", "1password", min_w=480, min_h=400),  # dedupe
        ]
        lua = build_apply_lua(plan)
        self.assertEqual(lua.count("soften('1password'"), 1)
        self.assertEqual(lua.count("soften('chromium'"), 1)
        # floor is half of toolkit min, clamped to >=80
        self.assertIn("soften('1password', 240, 200)", lua)
        self.assertIn("soften('chromium', 250, 100)", lua)

    def test_pop_tag_implies_pin(self):
        clients = {
            "0xp": {
                "address": "0xp",
                "floating": True,
                "fullscreen": 0,
                "tags": ["pop*"],
                "pinned": False,
            }
        }
        lua = build_apply_lua([plan_item("0xp", "foot")], clients)
        # apply(addr, x, y, w, h, need_fs, need_pin) — last arg true
        self.assertRegex(lua, r"apply\('address:0xp', 10, 20, 800, 500, false, true\)")


if __name__ == "__main__":
    unittest.main()
