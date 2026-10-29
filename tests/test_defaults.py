#!/usr/bin/env python3
"""Unit tests for preferred launch-size persistence."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from defaults import (  # noqa: E402
    LUA_BEGIN,
    LUA_END,
    ensure_hyprland_requires_sizes,
    load_preferred_sizes,
    merge_sizes_from_windows,
    preferred_size_for,
    render_sizes_lua,
    save_preferred_sizes_json,
    should_persist,
    write_sizes_lua,
)


def win(cls: str, w: int, h: int, *, title: str = "", address: str = "0x1") -> dict:
    return {
        "class": cls,
        "w": w,
        "h": h,
        "title": title,
        "address": address,
        "x": 0,
        "y": 0,
    }


class TestShouldPersist(unittest.TestCase):
    def test_skips_quickshell_and_owl(self):
        self.assertFalse(should_persist(win("org.quickshell", 400, 400)))
        self.assertFalse(should_persist(win("org.quickshell", 400, 400, title="owl:foo")))
        self.assertFalse(should_persist(win("goose", 400, 400, title="owl:x")))

    def test_keeps_normal_apps(self):
        self.assertTrue(should_persist(win("goose", 1112, 510)))
        self.assertTrue(should_persist(win("brave-browser", 900, 700)))

    def test_skips_tiny(self):
        self.assertFalse(should_persist(win("goose", 40, 40)))


class TestMergeAndStore(unittest.TestCase):
    def test_only_changed_updates_resized(self):
        initial = [
            win("goose", 800, 600, address="0xa"),
            win("brave-browser", 1000, 700, address="0xb"),
        ]
        final = [
            win("goose", 1112, 510, address="0xa"),  # resized
            win("brave-browser", 1000, 700, address="0xb"),  # unchanged
        ]
        existing = {"brave-browser": {"w": 1000, "h": 700}}
        merged = merge_sizes_from_windows(
            final, initial=initial, only_changed=True, existing=existing
        )
        self.assertEqual(merged["goose"], {"w": 1112, "h": 510})
        self.assertEqual(merged["brave-browser"], {"w": 1000, "h": 700})
        # Unchanged brave must not be rewritten from a swap-only path either.
        self.assertIn("goose", merged)

    def test_swap_only_does_not_clobber(self):
        initial = [win("goose", 800, 600, address="0xa")]
        final = [win("goose", 800, 600, address="0xa")]  # same size
        existing = {"goose": {"w": 1200, "h": 800}}
        merged = merge_sizes_from_windows(
            final, initial=initial, only_changed=True, existing=existing
        )
        self.assertEqual(merged["goose"], {"w": 1200, "h": 800})

    def test_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "preferred-sizes.json")
            save_preferred_sizes_json(
                {"goose": {"w": 1112, "h": 510}, "Brave": {"w": 900, "h": 700}},
                path=path,
            )
            loaded = load_preferred_sizes(path)
            self.assertEqual(loaded["goose"], {"w": 1112, "h": 510})
            self.assertEqual(preferred_size_for(win("goose", 1, 1), loaded), (1112, 510))
            # Case-insensitive lookup.
            self.assertEqual(preferred_size_for(win("brave", 1, 1), loaded), (900, 700))


class TestLuaRender(unittest.TestCase):
    def test_render_contains_markers_and_rules(self):
        text = render_sizes_lua({"goose": {"w": 1112, "h": 510}})
        self.assertIn(LUA_BEGIN, text)
        self.assertIn(LUA_END, text)
        self.assertIn(
            'o.window({ class = "goose" }, { float = true, size = { 1112, 510 } })',
            text,
        )

    def test_write_and_require(self):
        with tempfile.TemporaryDirectory() as td:
            lua_path = os.path.join(td, "window-arrange-sizes.lua")
            hypr_path = os.path.join(td, "hyprland.lua")
            Path(hypr_path).write_text(
                '-- stub\nrequire("hypr.bindings")\n', encoding="utf-8"
            )
            write_sizes_lua({"goose": {"w": 800, "h": 600}}, path=lua_path)
            self.assertTrue(os.path.isfile(lua_path))
            body = Path(lua_path).read_text(encoding="utf-8")
            self.assertIn("800, 600", body)

            changed = ensure_hyprland_requires_sizes(path=hypr_path)
            self.assertTrue(changed)
            hypr = Path(hypr_path).read_text(encoding="utf-8")
            self.assertIn('require("hypr.window-arrange-sizes")', hypr)
            # Second call is idempotent content-wise after strip+append.
            changed2 = ensure_hyprland_requires_sizes(path=hypr_path)
            # File may rewrite equivalently; require still present once.
            self.assertEqual(
                hypr.count('require("hypr.window-arrange-sizes")'),
                1,
            )
            hypr2 = Path(hypr_path).read_text(encoding="utf-8")
            self.assertEqual(
                hypr2.count('require("hypr.window-arrange-sizes")'),
                1,
            )
            self.assertIn("window-arrange:preferred-sizes-require-begin", hypr2)
            _ = changed2  # silence lint


if __name__ == "__main__":
    unittest.main()
