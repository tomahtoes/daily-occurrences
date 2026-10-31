"""The Hoshi source end to end over a synthetic library: what reaches the
recorder, in what sizes, and that failures, restarts and a second program never
make text go missing or get sent twice."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hoshi_helpers import apple, chapter_html, load, session, set_progress, write_book

source_mod = load("hoshi_source")
text = load("hoshi_text")

NOW = 1_800_000_000_000
SENTENCE = "これは試験の文です。"  # 9 counted characters, 10 long


def sentences(n, start=0):
    return [f"{SENTENCE[:-1]}{i:05d}。" for i in range(start, start + n)]  # unique lines


class FakeRecorder:
    """Answers chunks with scripted statuses (``recorded`` once the script runs
    out), keeping every line it recorded."""

    def __init__(self, script=()):
        self.script = list(script)
        self.recorded = []
        self.chunks = []

    def submit(self, chunk):
        self.chunks.append(chunk)
        status = self.script.pop(0) if self.script else "recorded"
        if status == "recorded":
            self.recorded.extend(chunk.lines)
        chunk.finish(status, None if status == "recorded" else "scripted")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hoshi-source-")
        self.books = os.path.join(self.tmp, "Books")
        self.out = os.path.join(self.tmp, "_seen")
        os.makedirs(self.books)
        os.makedirs(self.out)
        self.state_path = os.path.join(self.tmp, "hoshi-state.json")
        self.clock = NOW

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def source(self, recorder, owner="anki"):
        settings = source_mod.HoshiSettings(
            books_dir=self.books, poll_minutes=1, max_cpm=1000, state_path=self.state_path,
            output_dir=self.out, cutoff_hour=4, keep_days=0, owner=owner,
        )
        src = source_mod.HoshiSource(settings, recorder.submit, now_ms=lambda: self.clock, spacing=0)
        src.state = src._load_state()
        return src

    def new_book(self, name, chapters, **kw):
        """A book imported after the source started, so its whole position counts."""
        return write_book(os.path.join(self.books, name), chapters, imported_ms=self.clock + 1, **kw)

    def book_text(self, name, start, end):
        book = text.Book.open(os.path.join(self.books, name))
        return [line for *_s, line in book.slice(start, end)]


class Draining(Base):
    def test_a_finished_book_reaches_the_recorder_whole_in_bounded_chunks(self):
        rec = FakeRecorder()
        src = self.source(rec)
        total = self.new_book("本", [chapter_html(*sentences(600)), chapter_html(*sentences(300, 600))])
        set_progress(os.path.join(self.books, "本"), total, apple(NOW + 60_000))
        src.cycle()
        self.assertEqual(rec.recorded, self.book_text("本", 0, total))
        for chunk in rec.chunks:
            self.assertLessEqual(len(chunk.lines), source_mod.CHUNK_LINES)
            self.assertLessEqual(sum(map(len, chunk.lines)), source_mod.CHUNK_CHARS)
        self.assertEqual(src.state.pending, [])

    def test_a_large_backlog_is_spread_over_cycles(self):
        rec = FakeRecorder()
        src = self.source(rec)
        total = self.new_book("本", [chapter_html(*sentences(2000, i * 2000)) for i in range(5)])
        set_progress(os.path.join(self.books, "本"), total, apple(NOW + 60_000))
        src.cycle()
        first = sum(map(len, rec.recorded))
        self.assertLessEqual(first, source_mod.CYCLE_CHAR_CAP + source_mod.CHUNK_CHARS)
        self.assertTrue(src.state.pending)
        for _ in range(10):
            src.cycle()
        self.assertEqual(rec.recorded, self.book_text("本", 0, total))

    def test_a_transient_failure_keeps_the_queue_and_backs_off(self):
        rec = FakeRecorder(["transient"])
        src = self.source(rec)
        total = self.new_book("本", [chapter_html(*sentences(100))])
        set_progress(os.path.join(self.books, "本"), total, apple(NOW + 60_000))
        src.cycle()
        self.assertEqual(rec.recorded, [])
        self.assertEqual(src.state.pending[0].start, 0)
        self.assertGreater(src._retry_at, time.monotonic())
        self.assertIsNotNone(src.snapshot()["error"])
        src.cycle()  # still backing off: nothing sent
        self.assertEqual(len(rec.chunks), 1)
        src._retry_at = 0.0
        src.cycle()
        self.assertEqual(rec.recorded, self.book_text("本", 0, total))
        self.assertIsNone(src.snapshot()["error"])

    def test_a_rejected_chunk_is_dropped_and_the_rest_continues(self):
        rec = FakeRecorder(["permanent"])
        src = self.source(rec)
        total = self.new_book("本", [chapter_html(*sentences(200))])
        set_progress(os.path.join(self.books, "本"), total, apple(NOW + 60_000))
        src.cycle()
        everything = self.book_text("本", 0, total)
        self.assertEqual(rec.recorded, everything[len(rec.chunks[0].lines):])
        self.assertEqual(src.state.pending, [])

    def test_a_restart_mid_queue_resumes_without_resending(self):
        rec = FakeRecorder()
        src = self.source(rec)
        total = self.new_book("本", [chapter_html(*sentences(2000, i * 2000)) for i in range(3)])
        set_progress(os.path.join(self.books, "本"), total, apple(NOW + 60_000))
        src.cycle()
        self.assertTrue(src.state.pending)
        # A new process: state comes back from disk only.
        again = self.source(rec)
        for _ in range(10):
            again.cycle()
        self.assertEqual(rec.recorded, self.book_text("本", 0, total))

    def test_a_cancelled_chunk_is_not_committed(self):
        class Stopping(FakeRecorder):
            def submit(inner, chunk):
                inner.chunks.append(chunk)
                src._stop.set()  # stop arrives while the chunk is in flight

        rec = Stopping()
        src = self.source(rec)
        total = self.new_book("本", [chapter_html(*sentences(100))])
        set_progress(os.path.join(self.books, "本"), total, apple(NOW + 60_000))
        src.cycle()
        self.assertEqual(rec.chunks[0].status, "cancelled")
        self.assertEqual(src.state.pending[0].start, 0)


class Library(Base):
    def test_books_present_at_first_start_are_only_baselined(self):
        write_book(os.path.join(self.books, "既読"), [chapter_html(*sentences(50))],
                   position=200, mark_apple=apple(NOW - 86_400_000))
        rec = FakeRecorder()
        src = self.source(rec)
        src.cycle()
        self.assertEqual(rec.chunks, [])
        self.assertEqual(src.state.books["既読"].high_water, 200)

    def test_reading_after_the_baseline_is_credited(self):
        folder = os.path.join(self.books, "本")
        write_book(folder, [chapter_html(*sentences(50))], position=100, mark_apple=apple(NOW - 1000))
        rec = FakeRecorder()
        src = self.source(rec)
        src.cycle()
        set_progress(folder, 190, apple(NOW + 60_000), {"S": session(NOW, 90, 60)})
        src.cycle()
        self.assertEqual("".join(rec.recorded), "".join(self.book_text("本", 100, 190)))

    def test_a_remote_only_book_waits_for_its_download(self):
        folder = os.path.join(self.books, "本")
        chapters = [chapter_html(*sentences(30))]
        rec = FakeRecorder()
        src = self.source(rec)
        total = write_book(folder, chapters, imported_ms=0, with_epub=False,
                           position=150, mark_apple=apple(NOW + 60_000))
        src.cycle()
        self.assertEqual(rec.chunks, [])
        self.assertEqual(src.snapshot()["awaiting"], 1)
        self.assertEqual(src.snapshot()["pending_chars"], 150)
        write_book(folder, chapters, imported_ms=0, position=150, mark_apple=apple(NOW + 60_000))
        src.cycle()
        self.assertEqual(rec.recorded, self.book_text("本", 0, 150))
        self.assertEqual(src.snapshot()["awaiting"], 0)
        self.assertLess(150, total)

    def test_an_unreadable_book_does_not_hold_up_the_queue(self):
        rec = FakeRecorder()
        src = self.source(rec)
        broken = self.new_book("a", [chapter_html(*sentences(20))])
        set_progress(os.path.join(self.books, "a"), broken, apple(NOW + 60_000))
        with open(os.path.join(self.books, "a", "book.epub"), "wb") as handle:
            handle.write(b"not a zip")
        total = self.new_book("b", [chapter_html(*sentences(20, 20))])
        set_progress(os.path.join(self.books, "b"), total, apple(NOW + 60_000))
        src.cycle()
        self.assertEqual(rec.recorded, self.book_text("b", 0, total))
        self.assertEqual([r.folder for r in src.state.pending], ["a"])

    def test_an_unreadable_statistics_file_is_retried_not_treated_as_empty(self):
        folder = os.path.join(self.books, "本")
        write_book(folder, [chapter_html(*sentences(50))], position=0, mark_apple=apple(NOW - 1000))
        rec = FakeRecorder()
        src = self.source(rec)
        src.cycle()
        set_progress(folder, 300, apple(NOW + 60_000))
        with open(os.path.join(folder, "statistics.json"), "w", encoding="utf-8") as handle:
            handle.write('{"half": ')
        src.cycle()
        self.assertEqual(src.state.books["本"].high_water, 0)
        set_progress(folder, 300, apple(NOW + 60_000), {"S": session(NOW, 300, 30)})
        src.cycle()
        self.assertEqual(src.state.books["本"].high_water, 300)

    def test_deleted_sessions_are_ignored(self):
        folder = os.path.join(self.books, "本")
        write_book(folder, [chapter_html(*sentences(5))])
        with open(os.path.join(folder, "statistics.json"), "w", encoding="utf-8") as handle:
            json.dump({"A": {"modified": 1, "value": None},
                       "B": {"modified": 1, "value": session(NOW, 10, 5)}}, handle)
        observation = source_mod.read_observation(folder, "本")
        self.assertEqual(list(observation.sessions), ["B"])



class Heartbeat(Base):
    def _beat(self, owner, ts):
        with open(os.path.join(self.out, source_mod.HEARTBEAT_NAME), "w", encoding="utf-8") as handle:
            json.dump({"owner": owner, "ts": ts, "interval_s": 60}, handle)

    def _owner(self):
        with open(os.path.join(self.out, source_mod.HEARTBEAT_NAME), encoding="utf-8") as handle:
            return json.load(handle)["owner"]

    def test_a_fresh_heartbeat_from_another_program_blocks_crediting(self):
        self._beat("other-app", NOW - 30_000)
        rec = FakeRecorder()
        src = self.source(rec)
        total = self.new_book("本", [chapter_html(*sentences(20))])
        set_progress(os.path.join(self.books, "本"), total, apple(NOW + 60_000))
        src.cycle()
        self.assertEqual(rec.chunks, [])
        self.assertEqual(src.state.books, {})
        self.assertIn("other-app", src.snapshot()["error"])
        self.assertEqual(self._owner(), "other-app")

    def test_a_stale_heartbeat_is_taken_over(self):
        self._beat("other-app", NOW - 10 * 60_000)
        rec = FakeRecorder()
        src = self.source(rec)
        src.cycle()
        self.assertEqual(self._owner(), "anki")
        self.assertIsNone(src.snapshot()["error"])


if __name__ == "__main__":
    unittest.main()
