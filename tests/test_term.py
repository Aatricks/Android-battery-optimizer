import unittest
from unittest.mock import MagicMock, patch
import os
from android_battery_optimizer.term import supports_color, Formatter, render_table


class TestTerm(unittest.TestCase):
    def test_supports_color_force(self) -> None:
        with patch.dict(os.environ, {"FORCE_COLOR": "1"}):
            self.assertTrue(supports_color(None))

    def test_supports_color_no_color(self) -> None:
        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            self.assertFalse(supports_color(None))

    def test_supports_color_dumb(self) -> None:
        with patch.dict(os.environ, {"TERM": "dumb"}, clear=True):
            self.assertFalse(supports_color(None))

    def test_supports_color_tty(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            stream = MagicMock()
            stream.isatty.return_value = True
            self.assertTrue(supports_color(stream))

            stream.isatty.return_value = False
            self.assertFalse(supports_color(stream))

    def test_formatter_identity(self) -> None:
        fmt = Formatter(enabled=False)
        self.assertEqual(fmt.bold("test"), "test")
        self.assertEqual(fmt.dim("test"), "test")
        self.assertEqual(fmt.ok("test"), "test")
        self.assertEqual(fmt.warn("test"), "test")
        self.assertEqual(fmt.err("test"), "test")
        self.assertEqual(fmt.accent("test"), "test")
        self.assertEqual(fmt.header("test"), "test")

    def test_formatter_colored(self) -> None:
        fmt = Formatter(enabled=True)
        self.assertEqual(fmt.bold("test"), "\x1b[1mtest\x1b[0m")
        self.assertEqual(fmt.dim("test"), "\x1b[2mtest\x1b[0m")
        self.assertEqual(fmt.ok("test"), "\x1b[32mtest\x1b[0m")
        self.assertEqual(fmt.warn("test"), "\x1b[33mtest\x1b[0m")
        self.assertEqual(fmt.err("test"), "\x1b[31mtest\x1b[0m")
        self.assertEqual(fmt.accent("test"), "\x1b[36mtest\x1b[0m")
        self.assertEqual(fmt.header("test"), "\x1b[1;35mtest\x1b[0m")

    def test_render_table_basic(self) -> None:
        headers = ["COL1", "COL2"]
        rows = [
            ["a", "bb"],
            ["ccc", "d"]
        ]
        lines = render_table(headers, rows)
        self.assertEqual(lines[0], "COL1  COL2")
        self.assertEqual(lines[1], "a     bb")
        self.assertEqual(lines[2], "ccc   d")

    def test_render_table_truncation(self) -> None:
        headers = ["C1", "C2"]
        rows = [
            ["verylongstringhere", "short"]
        ]
        lines = render_table(headers, rows, max_col=10)
        self.assertEqual(lines[1], "verylon...  short")

    def test_render_table_ansi_handling(self) -> None:
        headers = ["C1", "C2"]
        rows = [
            ["\x1b[32mkeep\x1b[0m", "reason"]
        ]
        lines = render_table(headers, rows)
        self.assertEqual(lines[1], "\x1b[32mkeep\x1b[0m  reason")
