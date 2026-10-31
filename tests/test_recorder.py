"""The engine: the pause gate, the day/foreign-folder guard, and persistence.

Unlike the other modules under test, ``recorder.py`` uses package-relative
imports (``from .jiten import ...``, ``from .vendor import websocket``), so it
can't be imported bare. Load it under a stand-in parent package instead: that
resolves the relative imports — including the vendored websocket client — without
executing the real ``__init__.py``, which would pull in Anki.
"""

import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest

# Keep the add-on's own log output out of the test report. What the log says is
# asserted through the state snapshot, not by reading stderr.
logging.getLogger("daily_occurrences").addHandler(logging.NullHandler())
logging.getLogger("daily_occurrences").propagate = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PKG = "daily_occurrences_under_test"
if _PKG not in sys.modules:
    _package = types.ModuleType(_PKG)
    _package.__path__ = [ROOT]
    sys.modules[_PKG] = _package
    _spec = importlib.util.spec_from_file_location(
        _PKG + ".recorder", os.path.join(ROOT, "recorder.py")
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)

recorder = sys.modules[_PKG + ".recorder"]


def _settings(**overrides):
    values = dict(
        websocket_url="ws://localhost:0",
        jiten_api_key="",
        jiten_base_url="https://example.invalid",
        jiten_timeout_ms=1000,
        flush_every_lines=50,
        idle_flush_seconds=30,
        dedupe_window_lines=500,
        max_line_length=150,
        day_cutoff_hour=4,
        delete_after_days=60,
        output_dir=os.path.join(ROOT, "does-not-exist"),
    )
    values.update(overrides)
    return recorder.Settings(**values)


class PauseGate(unittest.TestCase):
    """Constructing a Recorder starts no threads and touches no disk or network,
    so ``_ingest`` — the path ``on_message`` drives — can be exercised directly.

    Test lines are Japanese on purpose: ``extract_lines`` tries ``json.loads``
    first, so ASCII words like "null" or "1" would parse as JSON and yield
    nothing.
    """

    def setUp(self):
        self.rec = recorder.Recorder(_settings())

    def _queued(self):
        out = []
        while not self.rec._queue.empty():
            out.append(self.rec._queue.get_nowait())
        return out

    def test_queues_while_recording(self):
        self.rec._ingest("あいうえお")
        self.assertEqual(self._queued(), ["あいうえお"])

    def test_paused_discards_every_line(self):
        self.rec.set_paused(True)
        for line in ("一行目", "二行目", "三行目"):
            self.rec._ingest(line)
        # Nothing is held back for later: paused lines are dropped, not deferred.
        self.assertEqual(self._queued(), [])

    def test_resume_queues_again(self):
        self.rec.set_paused(True)
        self.rec._ingest("捨てる行")
        self.rec.set_paused(False)
        self.rec._ingest("残る行")
        self.assertEqual(self._queued(), ["残る行"])

    def test_paused_lines_never_enter_the_dedup_window(self):
        # Pins the gate *above* _dedup.observe: a line discarded while paused must
        # not shadow the same line arriving after the resume.
        self.rec.set_paused(True)
        self.rec._ingest("同じ行")
        self.rec.set_paused(False)
        self.rec._ingest("同じ行")
        self.assertEqual(self._queued(), ["同じ行"])

    def test_set_paused_before_start(self):
        # _start() applies the flag between construction and start(), so this must
        # be safe on a Recorder whose threads don't exist yet.
        rec = recorder.Recorder(_settings())
        rec.set_paused(True)
        rec._ingest("あいうえお")
        self.assertTrue(rec._queue.empty())

    def test_negative_dedupe_window_still_ingests(self):
        # An unclamped negative window raised on every line, and websocket-client
        # swallows callback exceptions — so recording died while the summary
        # window still showed a healthy connection.
        rec = recorder.Recorder(_settings(dedupe_window_lines=-1))
        rec._ingest("一行目")
        rec._ingest("二行目")
        self.assertEqual(self._drain(rec), ["一行目", "二行目"])

    def test_negative_max_line_length_means_no_limit(self):
        # Truthiness-tested, a negative limit discarded every line instead.
        rec = recorder.Recorder(_settings(max_line_length=-1))
        rec._ingest("あ" * 500)
        self.assertEqual(self._drain(rec), ["あ" * 500])

    def test_max_line_length_still_drops_long_lines(self):
        rec = recorder.Recorder(_settings(max_line_length=10))
        rec._ingest("あ" * 11)
        rec._ingest("短い行")
        self.assertEqual(self._drain(rec), ["短い行"])

    def _drain(self, rec):
        out = []
        while not rec._queue.empty():
            out.append(rec._queue.get_nowait())
        return out


class DayAdoption(unittest.TestCase):
    """``output_dir`` is documented as shareable with Priority Reorder's
    ``_seen``, so a day folder written by anything else must be left completely
    alone — including on the write side, or the next save clobbers it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="daily-occ-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recorder(self):
        return recorder.Recorder(_settings(output_dir=self.tmp))

    def _write_foreign(self, day):
        directory = os.path.join(self.tmp, day)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "index.json"), "w", encoding="utf-8") as handle:
            json.dump({"title": "Some Other Dictionary", "format": 3}, handle)

    def test_adopts_a_free_day(self):
        rec = self._recorder()
        rec._adopt_day("2026-05-21")
        self.assertFalse(rec._day_blocked)
        self.assertIsNone(rec.state().snapshot()["error"])

    def test_blocks_and_reports_a_foreign_day(self):
        self._write_foreign("2026-05-21")
        rec = self._recorder()
        rec._adopt_day("2026-05-21")
        self.assertTrue(rec._day_blocked)
        self.assertIn("didn't write", rec.state().snapshot()["error"])

    def test_a_blocked_day_is_never_written(self):
        self._write_foreign("2026-05-21")
        rec = self._recorder()
        rec._adopt_day("2026-05-21")
        rec.state().current.record("本", "ほん")
        rec._persist()

        # The other tool's index.json is untouched and no banks appeared.
        directory = os.path.join(self.tmp, "2026-05-21")
        with open(os.path.join(directory, "index.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["title"], "Some Other Dictionary")
        self.assertEqual(os.listdir(directory), ["index.json"])

    def test_rolling_onto_a_free_day_clears_the_block(self):
        self._write_foreign("2026-05-21")
        rec = self._recorder()
        rec._adopt_day("2026-05-21")
        rec._adopt_day("2026-05-22")
        self.assertFalse(rec._day_blocked)
        # The stale message named a folder that is no longer being recorded to.
        self.assertIsNone(rec.state().snapshot()["error"])
        rec.state().current.record("本", "ほん")
        rec._persist()
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "2026-05-22", "term_meta_bank_1.json")
        ))


class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="daily-occ-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recorder(self):
        rec = recorder.Recorder(_settings(output_dir=self.tmp))
        rec._adopt_day("2026-05-21")
        return rec

    def test_maybe_persist_waits_for_the_interval(self):
        rec = self._recorder()
        rec.state().current.record("本", "ほん")
        rec._dirty = True
        rec._last_persist = float("inf")  # "just saved"
        rec._maybe_persist()
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "2026-05-21")))

        rec._last_persist = 0.0
        rec._maybe_persist()
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "2026-05-21", "term_meta_bank_1.json")
        ))
        # A save with nothing new behind it rewrites the whole day for nothing.
        self.assertFalse(rec._dirty)

    def test_concurrent_persists_are_serialized(self):
        rec = self._recorder()
        for i in range(300):
            rec.state().current.record(f"語{i}", f"ご{i}")

        errors = []

        def persist():
            try:
                for _ in range(5):
                    rec._persist()
            except Exception as err:  # noqa: BLE001 - asserted below
                errors.append(err)

        threads = [threading.Thread(target=persist) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        loaded = recorder.DayDict.load_from_dir(self.tmp, "2026-05-21")
        self.assertEqual(loaded.unique_words(), 300)


class _FakeJiten:
    """Every line parses to one 本; ``error`` makes every request fail instead."""

    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def parse_lines(self, lines):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [[("本", "ほん")] for _ in lines]


class HoshiChunks(unittest.TestCase):
    """The worker's Hoshi path: a chunk lands in its own day (today's dictionary or
    a past day's folder), is answered exactly once, and a cancelled or failed one
    leaves no words behind."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="daily-occ-test-")
        self.rec = recorder.Recorder(_settings(output_dir=self.tmp))
        self.rec._jiten = _FakeJiten()
        self.today = recorder.now_day_key(4)
        self.rec._adopt_day(self.today)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _count(self, day):
        loaded = recorder.DayDict.load_from_dir(self.tmp, day)
        return loaded.counts.get(("本", "ほん"), 0) if loaded else 0

    def test_a_chunk_for_today_is_recorded_and_saved_at_once(self):
        chunk = recorder.HoshiChunk(self.today, ["一行目。", "二行目。"])
        self.rec._record_hoshi(chunk)
        self.assertEqual(chunk.status, "recorded")
        self.assertTrue(chunk.done.is_set())
        self.assertEqual(self._count(self.today), 2)

    def test_a_chunk_for_a_past_day_merges_into_that_folder(self):
        earlier = recorder.DayDict("2026-01-01", {("本", "ほん"): 2})
        earlier.write_to_dir(self.tmp)
        chunk = recorder.HoshiChunk("2026-01-01", ["あ。", "い。", "う。"])
        self.rec._record_hoshi(chunk)
        self.assertEqual(chunk.status, "recorded")
        self.assertEqual(self._count("2026-01-01"), 5)
        # Today's dictionary is untouched.
        self.assertEqual(self.rec.state().snapshot()["total"], 0)

    def test_a_past_day_that_cant_be_read_is_retried_not_overwritten(self):
        recorder.DayDict("2026-01-01", {("本", "ほん"): 2}).write_to_dir(self.tmp)
        bank = os.path.join(self.tmp, "2026-01-01", "term_meta_bank_1.json")
        with open(bank, "w", encoding="utf-8") as handle:
            handle.write("[[\"本\", \"freq")  # truncated mid-write
        chunk = recorder.HoshiChunk("2026-01-01", ["あ。"])
        self.rec._record_hoshi(chunk)
        self.assertEqual(chunk.status, "transient")
        with open(bank, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "[[\"本\", \"freq")

    def test_a_foreign_past_day_is_refused_and_left_alone(self):
        directory = os.path.join(self.tmp, "2026-01-01")
        os.makedirs(directory)
        with open(os.path.join(directory, "index.json"), "w", encoding="utf-8") as handle:
            json.dump({"title": "Some Other Dictionary"}, handle)
        chunk = recorder.HoshiChunk("2026-01-01", ["あ。"])
        self.rec._record_hoshi(chunk)
        self.assertEqual(chunk.status, "permanent")
        self.assertEqual(os.listdir(directory), ["index.json"])

    def test_rate_limits_are_retried_and_bad_requests_dropped(self):
        for status, expected in ((429, "transient"), (503, "transient"), (401, "transient"),
                                 (400, "permanent"), (None, "transient")):
            self.rec._jiten = _FakeJiten(recorder.JitenError("no", status=status))
            chunk = recorder.HoshiChunk(self.today, ["あ。"])
            self.rec._record_hoshi(chunk)
            self.assertEqual(chunk.status, expected, status)
        self.assertEqual(self._count(self.today), 0)

    def test_a_chunk_cancelled_before_it_is_recorded_leaves_no_words(self):
        chunk = recorder.HoshiChunk(self.today, ["あ。"])
        self.assertEqual(chunk.cancel(), "cancelled")
        self.rec._record_hoshi(chunk)
        self.assertEqual(chunk.status, "cancelled")
        self.assertEqual(self.rec.state().snapshot()["total"], 0)

    def test_a_chunk_is_recorded_while_paused(self):
        # Pause is for the websocket. A Hoshi chunk can carry reading done long
        # before (a phone session syncing in); discarding it would lose it for good.
        self.rec.set_paused(True)
        chunk = recorder.HoshiChunk(self.today, ["あ。"])
        self.rec._record_hoshi(chunk)
        self.assertEqual(chunk.status, "recorded")
        self.assertEqual(self._count(self.today), 1)

    def test_a_chunk_answered_first_cannot_be_cancelled(self):
        chunk = recorder.HoshiChunk(self.today, ["あ。"])
        self.rec._record_hoshi(chunk)
        self.assertEqual(chunk.cancel(), "recorded")

    def test_a_stopped_engine_answers_at_once(self):
        self.rec._stop.set()
        chunk = recorder.HoshiChunk(self.today, ["あ。"])
        self.rec.submit_hoshi(chunk)
        self.assertEqual(chunk.status, "transient")
        self.assertTrue(self.rec._queue.empty())

    def test_a_blank_websocket_url_starts_no_websocket(self):
        rec = recorder.Recorder(_settings(output_dir=self.tmp, websocket_url=""))
        rec.start()
        try:
            self.assertIsNone(rec._ws_thread)
            self.assertFalse(rec.state().snapshot()["websocket"])
            chunk = recorder.HoshiChunk(rec.state().snapshot()["day"], ["あ。"])
            rec._jiten = _FakeJiten()
            rec.submit_hoshi(chunk)
            self.assertTrue(chunk.done.wait(5))
            self.assertEqual(chunk.status, "recorded")
        finally:
            rec.stop()


if __name__ == "__main__":
    unittest.main()
