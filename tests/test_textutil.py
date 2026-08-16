import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from textutil import DedupWindow, day_key, extract_lines, flatten_text


class FlattenText(unittest.TestCase):
    def test_drops_newline_runs(self):
        self.assertEqual(flatten_text("hello\nworld"), "helloworld")
        self.assertEqual(flatten_text("  hello  \r\n  world  "), "helloworld")
        self.assertEqual(flatten_text("\n\n\nfoo\n"), "foo")
        self.assertEqual(flatten_text("hello  world"), "hello  world")
        self.assertEqual(flatten_text("   hi   "), "hi")


class ExtractLines(unittest.TestCase):
    def test_array_raw_and_object(self):
        self.assertEqual(extract_lines('["あ","い"]'), ["あ", "い"])
        self.assertEqual(extract_lines("  そのまま  "), ["そのまま"])
        self.assertEqual(extract_lines('{"text":"やあ"}'), ["やあ"])

    def test_picks_first_known_field(self):
        self.assertEqual(
            extract_lines('{"sentence":"S","text":"T","content":"C","message":"M"}'),
            ["S"],
        )
        self.assertEqual(extract_lines('{"text":"T","content":"C"}'), ["T"])
        self.assertEqual(extract_lines('{"other":"x"}'), [])


class Dedup(unittest.TestCase):
    def test_detects_exact_repeats(self):
        w = DedupWindow(3)
        self.assertTrue(w.observe("a"))
        self.assertTrue(w.observe("b"))
        self.assertFalse(w.observe("a"))
        self.assertFalse(w.observe("b"))
        self.assertTrue(w.observe("c"))

    def test_evicts_oldest_past_capacity(self):
        w = DedupWindow(2)
        self.assertTrue(w.observe("a"))
        self.assertTrue(w.observe("b"))
        self.assertTrue(w.observe("c"))  # evicts "a"
        self.assertTrue(w.observe("a"))  # "a" treated as new again

    def test_zero_capacity_disables(self):
        w = DedupWindow(0)
        self.assertTrue(w.observe("a"))
        self.assertTrue(w.observe("a"))

    def test_negative_capacity_behaves_like_zero(self):
        # Unclamped, the eviction check would pop an empty deque and raise on
        # every line — which websocket-client swallows, so recording would die
        # silently while the UI still claimed to be connected.
        w = DedupWindow(-5)
        self.assertTrue(w.observe("a"))
        self.assertTrue(w.observe("a"))


class DayKey(unittest.TestCase):
    @staticmethod
    def key_at(hour, minute, cutoff):
        return day_key(datetime(2026, 5, 21, hour, minute, 0), cutoff)

    def test_before_cutoff_is_previous_day(self):
        self.assertEqual(self.key_at(3, 59, 4), "2026-05-20")
        self.assertEqual(self.key_at(0, 0, 4), "2026-05-20")

    def test_at_and_after_cutoff_is_current_day(self):
        self.assertEqual(self.key_at(4, 0, 4), "2026-05-21")
        self.assertEqual(self.key_at(4, 1, 4), "2026-05-21")
        self.assertEqual(self.key_at(23, 59, 4), "2026-05-21")

    def test_zero_cutoff_matches_calendar_day(self):
        self.assertEqual(self.key_at(0, 0, 0), "2026-05-21")
        self.assertEqual(self.key_at(23, 59, 0), "2026-05-21")


if __name__ == "__main__":
    unittest.main()
