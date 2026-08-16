import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep the add-on's own log output out of the test report.
logging.getLogger("daily_occurrences").addHandler(logging.NullHandler())
logging.getLogger("daily_occurrences").propagate = False

from store import (
    MAX_ENTRIES_PER_BANK,
    DayDict,
    day_is_foreign,
    ensure_output_dir,
    purge_old_days,
)


class StoreRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="daily-occ-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trips_through_disk(self):
        dict_ = DayDict("2026-05-21")
        dict_.record("食べる", "たべる")
        dict_.record("食べる", "たべる")  # count 2
        dict_.record("本", "ほん")
        dict_.record("ねこ", "ねこ")  # kana-only → compact form
        dict_.write_to_dir(self.tmp)

        loaded = DayDict.load_from_dir(self.tmp, "2026-05-21")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.counts[("食べる", "たべる")], 2)
        self.assertEqual(loaded.total_occurrences(), 4)
        self.assertEqual(loaded.unique_words(), 3)
        self.assertEqual(loaded.counts, dict_.counts)

    def test_missing_folder_is_none(self):
        self.assertIsNone(DayDict.load_from_dir(self.tmp, "2099-01-01"))

    def test_index_has_required_yomitan_fields(self):
        dict_ = DayDict("2026-05-21")
        dict_.record("本", "ほん")
        dict_.write_to_dir(self.tmp)

        with open(
            os.path.join(self.tmp, "2026-05-21", "index.json"), encoding="utf-8"
        ) as handle:
            index = json.load(handle)
        self.assertEqual(index["format"], 3)
        self.assertEqual(index["frequencyMode"], "occurrence-based")
        self.assertIn("2026-05-21", index["title"])

    def test_blank_reading_merges_with_the_headword(self):
        # Both forms serialize to the same compact entry, so keeping them as
        # separate keys wrote two entries for one headword and lost a count on
        # the next load.
        dict_ = DayDict("2026-05-21")
        dict_.record("ABC", "")
        dict_.record("ABC", "ABC")
        self.assertEqual(dict_.unique_words(), 1)
        self.assertEqual(dict_.total_occurrences(), 2)

        dict_.write_to_dir(self.tmp)
        loaded = DayDict.load_from_dir(self.tmp, "2026-05-21")
        self.assertEqual(loaded.counts, {("ABC", "ABC"): 2})

    def test_total_survives_a_round_trip(self):
        # total_occurrences() is a running counter rather than a sum, so loading
        # has to seed it too.
        dict_ = DayDict("2026-05-21")
        for _ in range(3):
            dict_.record("本", "ほん")
        dict_.write_to_dir(self.tmp)
        self.assertEqual(DayDict.load_from_dir(self.tmp, "2026-05-21").total_occurrences(), 3)

    def test_splits_and_prunes_banks(self):
        dict_ = DayDict("2026-05-21")
        for i in range(MAX_ENTRIES_PER_BANK + 5):
            dict_.record(f"語{i}", f"ご{i}")
        dict_.write_to_dir(self.tmp)
        day = os.path.join(self.tmp, "2026-05-21")
        self.assertTrue(os.path.isfile(os.path.join(day, "term_meta_bank_2.json")))
        self.assertEqual(DayDict.load_from_dir(self.tmp, "2026-05-21").unique_words(),
                         MAX_ENTRIES_PER_BANK + 5)

        # A later, smaller day must not leave the second bank behind, or the
        # load would read words that are no longer recorded.
        smaller = DayDict("2026-05-21")
        smaller.record("本", "ほん")
        smaller.write_to_dir(self.tmp)
        self.assertFalse(os.path.isfile(os.path.join(day, "term_meta_bank_2.json")))
        self.assertEqual(DayDict.load_from_dir(self.tmp, "2026-05-21").unique_words(), 1)

    def test_unreadable_bank_is_skipped_not_raised(self):
        # This path runs on the worker thread, where raising would end recording
        # for the rest of the session.
        dict_ = DayDict("2026-05-21")
        dict_.record("本", "ほん")
        dict_.write_to_dir(self.tmp)
        bank = os.path.join(self.tmp, "2026-05-21", "term_meta_bank_1.json")
        with open(bank, "w", encoding="utf-8") as handle:
            handle.write('[["本","freq",{"value":1')  # truncated mid-write

        loaded = DayDict.load_from_dir(self.tmp, "2026-05-21")
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.is_empty())

    def test_a_write_never_resurrects_a_deleted_output_dir(self):
        # Anki deletes an add-on by removing its folder while running, and the
        # default output_dir lives inside it. A worker save moments later used to
        # recreate the tree, leaving a stray add-on folder behind and making the
        # delete look like it had failed.
        nested = os.path.join(self.tmp, "daily_occurrences", "user_files", "dicts")
        os.makedirs(nested)
        dict_ = DayDict("2026-05-21")
        dict_.record("本", "ほん")
        dict_.write_to_dir(nested)

        shutil.rmtree(os.path.join(self.tmp, "daily_occurrences"))

        dict_.record("猫", "ねこ")
        with self.assertRaises(OSError):
            dict_.write_to_dir(nested)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "daily_occurrences")))

    def test_ensure_output_dir_creates_the_tree_once(self):
        nested = os.path.join(self.tmp, "a", "b", "dicts")
        ensure_output_dir(nested)
        self.assertTrue(os.path.isdir(nested))
        # Idempotent: startup runs it on every profile open.
        ensure_output_dir(nested)
        DayDict("2026-05-21").write_to_dir(nested)  # now writes fine

    def test_leaves_no_temp_files_behind(self):
        dict_ = DayDict("2026-05-21")
        dict_.record("本", "ほん")
        dict_.write_to_dir(self.tmp)
        leftovers = [
            name
            for name in os.listdir(os.path.join(self.tmp, "2026-05-21"))
            if name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_concurrent_writes_never_corrupt_the_folder(self):
        # Recorder serializes its own writes, but the temp file is what makes an
        # unserialized race survivable: with a shared "<path>.tmp" two writers
        # truncated each other's file and a half-written bank reached disk.
        # Unique temp names mean the worst case is a *refused* rename (Windows
        # raises rather than clobbering), which _persist catches as an OSError.
        dicts = []
        for n in (1, 2, 3, 4):
            d = DayDict("2026-05-21")
            for i in range(200):
                for _ in range(n):
                    d.record(f"語{i}", f"ご{i}")
            dicts.append(d)

        failures = []

        def write(day_dict):
            try:
                day_dict.write_to_dir(self.tmp)
            except Exception as err:  # noqa: BLE001 - recorded and asserted below
                failures.append(err)

        threads = [threading.Thread(target=write, args=(d,)) for d in dicts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Anything that does go wrong must be an OSError, since that is the only
        # class _persist is prepared to swallow.
        for err in failures:
            self.assertIsInstance(err, OSError)

        loaded = DayDict.load_from_dir(self.tmp, "2026-05-21")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.unique_words(), 200)
        # Every count came from exactly one writer — no interleaved file.
        self.assertIn(loaded.counts[("語0", "ご0")], (1, 2, 3, 4))


class ForeignDayFolders(unittest.TestCase):
    """``output_dir`` is meant to be shareable (Priority Reorder's ``_seen``),
    so a folder written by anything else is neither read nor written."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="daily-occ-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_foreign(self, day, title="Some Other Dictionary"):
        directory = os.path.join(self.tmp, day)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "index.json"), "w", encoding="utf-8") as handle:
            json.dump({"title": title, "format": 3}, handle)
        with open(
            os.path.join(directory, "term_meta_bank_1.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump([["猫", "freq", {"value": 99}]], handle)
        return directory

    def test_foreign_day_is_reported_and_not_loaded(self):
        self._write_foreign("2026-05-21")
        self.assertTrue(day_is_foreign(self.tmp, "2026-05-21"))
        # Absorbing those counts would also re-title the folder as ours, which
        # would then make it eligible for the purge.
        self.assertIsNone(DayDict.load_from_dir(self.tmp, "2026-05-21"))

    def test_our_own_day_is_not_foreign(self):
        dict_ = DayDict("2026-05-21")
        dict_.record("本", "ほん")
        dict_.write_to_dir(self.tmp)
        self.assertFalse(day_is_foreign(self.tmp, "2026-05-21"))
        self.assertIsNotNone(DayDict.load_from_dir(self.tmp, "2026-05-21"))

    def test_missing_or_bare_folder_is_not_foreign(self):
        # Nothing of anyone else's to protect; a bare folder is also what a
        # half-finished write of our own leaves behind.
        self.assertFalse(day_is_foreign(self.tmp, "2026-05-21"))
        os.makedirs(os.path.join(self.tmp, "2026-05-22"))
        self.assertFalse(day_is_foreign(self.tmp, "2026-05-22"))


class PurgeOldDays(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="daily-occ-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_day(self, day):
        dict_ = DayDict(day)
        dict_.record("本", "ほん")
        dict_.write_to_dir(self.tmp)

    def _days(self):
        return sorted(os.listdir(self.tmp))

    def test_removes_only_days_past_the_window(self):
        for day in ("2026-01-01", "2026-03-02", "2026-03-03", "2026-05-01"):
            self._write_day(day)

        # 2026-03-02 is exactly 60 days old — the boundary is kept.
        removed = purge_old_days(self.tmp, 60, "2026-05-01")

        self.assertEqual(removed, ["2026-01-01"])
        self.assertEqual(self._days(), ["2026-03-02", "2026-03-03", "2026-05-01"])

    def test_zero_disables_purging(self):
        self._write_day("2020-01-01")
        self.assertEqual(purge_old_days(self.tmp, 0, "2026-05-01"), [])
        self.assertEqual(self._days(), ["2020-01-01"])

    def test_leaves_foreign_and_non_day_folders_alone(self):
        self._write_day("2020-01-01")  # ours, ancient

        # A day folder some other tool wrote, plus a non-date folder and a file.
        foreign = os.path.join(self.tmp, "2020-01-02")
        os.makedirs(foreign)
        with open(os.path.join(foreign, "index.json"), "w", encoding="utf-8") as handle:
            json.dump({"title": "Some Other Dictionary", "format": 3}, handle)
        os.makedirs(os.path.join(self.tmp, "notes"))
        with open(os.path.join(self.tmp, "readme.txt"), "w", encoding="utf-8") as handle:
            handle.write("keep me")

        removed = purge_old_days(self.tmp, 60, "2026-05-01")

        self.assertEqual(removed, ["2020-01-01"])
        self.assertEqual(self._days(), ["2020-01-02", "notes", "readme.txt"])

    def test_missing_output_dir_is_not_an_error(self):
        missing = os.path.join(self.tmp, "nope")
        self.assertEqual(purge_old_days(missing, 60, "2026-05-01"), [])


if __name__ == "__main__":
    unittest.main()
