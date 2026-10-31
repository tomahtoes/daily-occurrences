"""The Hoshi Reader source: watches Hoshi's book folders and feeds what was read
to the recorder as ``HoshiChunk``s.

One background thread runs a cycle every ``poll_minutes``:

1. **claim** — make sure no other program is crediting the same reading (see
   ``_claim``);
2. **plan** — read each changed book folder and let ``hoshi_policy.plan`` decide
   which ranges were read; the ranges join a durable pending queue in the same
   atomic write that advances each book's ``high_water``, so a range is never
   planned twice;
3. **drain** — turn the head of the queue into chunks of whole lines (at most
   ``CHUNK_LINES`` lines / ``CHUNK_CHARS`` characters), hand them to the recorder
   one at a time, and remove each chunk's span from the queue once the recorder
   says it is recorded. Requests are spaced ``REQUEST_SPACING`` apart, a cycle
   sends at most ``CYCLE_CHAR_CAP`` characters, and failures back off
   exponentially, so a large backlog (a day read on the phone, a whole book)
   reaches the reader API as a slow trickle instead of a burst.

Ranges for a book that is only on another device so far (synced but not yet
downloaded, so there is no text to cut) wait in the queue until the EPUB shows up.

The heartbeat file ``.hoshi-source.json`` lives in ``output_dir`` next to the day
folders. Priority Reorder only reads ``YYYY-MM-DD`` folders and the purge only
removes those, so it is invisible to both.
"""

import json
import logging
import os
import sys
import threading
import time
import zipfile

from . import hoshi_policy as policy
from .hoshi_text import Book
from .recorder import HoshiChunk
from .store import _write_atomic
from .textutil import now_day_key

log = logging.getLogger("daily_occurrences")

CHUNK_LINES = 50          # matches the recorder's default batch size
CHUNK_CHARS = 4000        # matches recorder.BATCH_CHAR_CAP
REQUEST_SPACING = 3.0     # seconds between Hoshi requests
CYCLE_CHAR_CAP = 40_000   # characters sent per poll cycle at most
BACKOFF_INITIAL = 60.0    # seconds; first wait after a failed request
BACKOFF_MAX = 1800.0      # seconds; ceiling (doubles each failure)
HEARTBEAT_NAME = ".hoshi-source.json"
_WINDOW_SLACK = 400       # extra positions sliced per chunk, for the line it may cut
_READ_ATTEMPTS = 3       # Hoshi writes its JSON in place, so a read can catch it mid-write
_READ_RETRY = 0.2
# What reading a damaged or incomplete EPUB can raise.
_BOOK_ERRORS = (OSError, KeyError, ValueError, zipfile.BadZipFile)


def default_books_dir():
    """Where Hoshi Reader Desktop keeps its library on this platform."""
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "hoshi-reader", "Books")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/de.manhhao.hoshi/Books")
    return os.path.expanduser("~/.local/share/de.manhhao.hoshi/Books")


class HoshiSettings:
    def __init__(self, books_dir, poll_minutes, max_cpm, state_path, output_dir,
                 cutoff_hour, keep_days, owner="anki"):
        self.books_dir = books_dir
        self.poll_minutes = poll_minutes
        self.max_cpm = max_cpm
        self.state_path = state_path
        self.output_dir = output_dir
        self.cutoff_hour = cutoff_hour
        self.keep_days = keep_days
        self.owner = owner


def _read_json(path):
    """``(value, ok)``. A missing file is ``(None, True)``; one that still won't
    parse after a few tries is ``(None, False)``, and the caller must not treat
    it as absent — an unreadable statistics file is not "no sessions"."""
    for attempt in range(_READ_ATTEMPTS):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle), True
        except FileNotFoundError:
            return None, True
        except (OSError, ValueError):
            if attempt + 1 < _READ_ATTEMPTS:
                time.sleep(_READ_RETRY)
    return None, False


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def read_observation(folder_path, folder):
    """An ``Observation`` of one book folder, or None when it can't be read
    reliably right now (or isn't a book)."""
    metadata, ok = _read_json(os.path.join(folder_path, "metadata.json"))
    if not ok or not isinstance(metadata, dict):
        return None
    total = metadata.get("characterCount")
    if not total:
        bookinfo, ok = _read_json(os.path.join(folder_path, "bookinfo.json"))
        if not ok:
            return None
        total = (bookinfo or {}).get("characterCount") if isinstance(bookinfo, dict) else None
    if not isinstance(total, int) or total <= 0:
        return None

    bookmark_path = os.path.join(folder_path, "bookmark.json")
    bookmark, ok = _read_json(bookmark_path)
    if not ok:
        return None
    bookmark = bookmark if isinstance(bookmark, dict) else {}
    position = bookmark.get("characterCount")
    position = int(position) if isinstance(position, (int, float)) else 0
    modified = bookmark.get("lastModified")
    if isinstance(modified, (int, float)) and not isinstance(modified, bool):
        mark_ms = int((modified + policy.APPLE_EPOCH) * 1000)
    else:
        mtime = _mtime(bookmark_path)
        mark_ms = mtime // 1_000_000 if mtime else 0

    stats, ok = _read_json(os.path.join(folder_path, "statistics.json"))
    if not ok:
        return None
    sessions = {}
    # Hoshi rewrites the old ttu array format into sessions when it loads a book,
    # so an array here is a file it hasn't converted yet: no sessions to use.
    if isinstance(stats, dict):
        for uuid, entry in stats.items():
            value = entry.get("value") if isinstance(entry, dict) else None
            if not isinstance(value, dict):
                continue  # null = a deleted session
            try:
                sessions[uuid] = policy.Session(
                    int(value["startedAt"]), int(value["charactersRead"]),
                    float(value["readingTime"]),
                )
            except (KeyError, TypeError, ValueError):
                continue

    imported = metadata.get("modified")
    imported = int(imported) if isinstance(imported, (int, float)) else 0
    return policy.Observation(folder, total, position, mark_ms, imported, sessions)


class HoshiSource:
    def __init__(self, settings, submit, now_ms=None, spacing=REQUEST_SPACING):
        self.settings = settings
        self._submit = submit
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._spacing = spacing
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()  # guards the fields the summary reads
        self._status = "starting"
        self._error = None
        self._books = 0
        self._awaiting = set()
        self._credited = {}            # day -> characters recorded this session
        self._signatures = {}          # folder -> file mtimes at the last plan
        self._opened = {}              # folder -> (signature, Book or None)
        self._checked = set()          # folders whose counts were verified
        self._broken = set()           # folders whose text couldn't be read, warned once
        self._backoff = 0.0
        self._retry_at = 0.0
        self._in_flight = None
        self.state = None

    # --- lifecycle ----------------------------------------------------------

    def start(self):
        self.state = self._load_state()
        self._thread = threading.Thread(target=self._loop, name="daily-occ-hoshi", daemon=True)
        self._thread.start()
        log.info("hoshi: watching %s", self.settings.books_dir)

    def stop(self):
        self._stop.set()
        chunk = self._in_flight
        if chunk is not None:
            chunk.cancel()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def snapshot(self):
        today = now_day_key(self.settings.cutoff_hour)
        with self._lock:
            pending = sum(r.end - r.start for r in self.state.pending) if self.state else 0
            return {
                "status": self._status,
                "error": self._error,
                "books": self._books,
                "awaiting": len(self._awaiting),
                "pending_chars": pending,
                "chars_today": self._credited.get(today, 0),
            }

    def _set_status(self, status, error=None):
        with self._lock:
            changed = self._error != error
            self._status, self._error = status, error
        if changed and error:
            log.warning("hoshi: %s", error)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception:
                # Same rule as the recorder's worker: one bad cycle must not end
                # the source for the session.
                log.exception("hoshi: cycle failed; continuing")
            if self._stop.wait(max(1, self.settings.poll_minutes) * 60.0):
                break

    # --- one cycle ----------------------------------------------------------

    def cycle(self):
        books_dir = self.settings.books_dir
        if not os.path.isdir(books_dir):
            self._set_status("folder missing", f"Hoshi library not found at {books_dir}")
            return
        owner = self._claim()
        if owner is not None:
            self._set_status("blocked", f"The Hoshi source is already active in {owner} — not reading Hoshi here.")
            return
        today = now_day_key(self.settings.cutoff_hour)
        self._plan_all(books_dir, today)
        self._drain(today)
        with self._lock:
            # A drain failure stays on screen until a later chunk goes through
            # (or there's nothing left that could fail).
            failing = self._status == "error" and bool(self.state.pending)
            self._awaiting = {
                r.folder for r in self.state.pending
                if r.folder in self._opened and self._opened[r.folder][1] is None
            }
        if not failing:
            self._set_status("watching")

    def _claim(self):
        """Write our heartbeat, unless another program's is still fresh — then
        return that program's name. Two sources crediting the same reading into
        the same folder would count every word twice."""
        path = os.path.join(self.settings.output_dir, HEARTBEAT_NAME)
        now = self._now_ms()
        beat, _ok = _read_json(path)
        if isinstance(beat, dict) and beat.get("owner") != self.settings.owner:
            interval = beat.get("interval_s")
            interval = interval if isinstance(interval, (int, float)) and interval > 0 else 60
            ts = beat.get("ts")
            if isinstance(ts, (int, float)) and now - ts < 3 * interval * 1000:
                return str(beat.get("owner") or "another program")
        if not os.path.isdir(self.settings.output_dir):
            return None
        try:
            _write_atomic(path, json.dumps({
                "owner": self.settings.owner,
                "ts": now,
                "interval_s": max(1, self.settings.poll_minutes) * 60,
            }))
        except OSError as err:
            log.warning("hoshi: can't write heartbeat: %s", err)
        return None

    def _day_of(self, ms):
        return policy.ms_day(ms, self.settings.cutoff_hour)

    def _signature(self, folder_path):
        return tuple(_mtime(os.path.join(folder_path, name)) for name in
                     ("metadata.json", "bookmark.json", "statistics.json", "bookinfo.json"))

    def _plan_all(self, books_dir, today):
        try:
            names = sorted(os.listdir(books_dir))
        except OSError as err:
            self._set_status("error", f"Can't read {books_dir}: {err}")
            return
        planned = []
        changed = False
        books = 0
        for name in names:
            folder_path = os.path.join(books_dir, name)
            if name.startswith(".") or name == "statistics_archive" or not os.path.isdir(folder_path):
                continue
            books += 1
            signature = self._signature(folder_path)
            if self._signatures.get(name) == signature and name in self.state.books:
                continue
            obs = read_observation(folder_path, name)
            if obs is None:
                continue  # unreadable right now; the signature stays stale, so retried next cycle
            ranges = policy.plan(
                self.state, obs, self._day_of, self.settings.max_cpm,
                self.settings.keep_days, today,
            )
            self._signatures[name] = signature
            planned.extend(ranges)
            changed = True
            for r in ranges:
                log.info("hoshi: %s +%d chars → %s", name, r.end - r.start, r.day)
        with self._lock:
            self._books = books
            self.state.pending.extend(planned)
        if changed:
            self._save_state()

    def _book(self, r):
        """The downloaded book for a range, ``"awaiting"`` while it is remote-only,
        or None when the range can never be served (folder gone, or a different
        edition now sits there)."""
        folder_path = os.path.join(self.settings.books_dir, r.folder)
        if not os.path.isdir(folder_path):
            return None
        signature = self._signature(folder_path)
        cached = self._opened.get(r.folder)
        if cached is None or cached[0] != signature:
            book = Book.open(folder_path)
            self._opened[r.folder] = cached = (signature, book)
            if book is not None and r.folder not in self._checked:
                self._checked.add(r.folder)
                try:
                    bad = book.mismatches()
                except _BOOK_ERRORS as err:
                    log.warning("hoshi: %s: can't check chapter counts: %s", r.folder, err)
                    bad = []
                if bad:
                    log.warning("hoshi: %s: %d chapter count(s) differ from Hoshi's; positions may drift: %s",
                                r.folder, len(bad), bad[:3])
        book = cached[1]
        if book is None:
            return "awaiting"
        if book.total != r.total:
            return None
        return book

    def _drain(self, today):
        if time.monotonic() < self._retry_at:
            return
        sent = 0
        index = 0
        while not self._stop.is_set() and sent < CYCLE_CHAR_CAP:
            with self._lock:
                if index >= len(self.state.pending):
                    break
                r = self.state.pending[index]
            if not policy.within_retention(r.day, today, self.settings.keep_days):
                log.info("hoshi: %s: dropping %d chars for %s (past retention)", r.folder, r.end - r.start, r.day)
                self._drop(index)
                continue
            book = self._book(r)
            if book == "awaiting":
                index += 1
                continue
            if book is None:
                log.warning("hoshi: %s: book changed or removed; dropping %d queued chars", r.folder, r.end - r.start)
                self._drop(index)
                continue

            # Slice only a window a little over one chunk (a line has at least as
            # many characters as it has counted ones), dropping its last line when
            # the window may have cut it short. A big range is then never
            # re-sliced whole for every chunk.
            window_end = min(r.end, r.start + CHUNK_CHARS + _WINDOW_SLACK)
            try:
                window = book.slice(r.start, window_end)
            except _BOOK_ERRORS as err:
                # Skipped rather than raised: one unreadable book must not hold
                # up everything queued behind it. Its ranges stay queued.
                if r.folder not in self._broken:
                    self._broken.add(r.folder)
                    log.warning("hoshi: %s: can't read the book's text: %s", r.folder, err)
                index += 1
                continue
            if window_end < r.end and len(window) > 1:
                window = window[:-1]
            lines = []
            size = 0
            for start, end, line in window:
                if lines and (len(lines) >= CHUNK_LINES or size + len(line) > CHUNK_CHARS):
                    break
                lines.append((start, end, line))
                size += len(line)
            if not lines:
                self._drop(index)  # nothing countable left in it
                continue

            chunk = HoshiChunk(r.day, [line for _s, _e, line in lines])
            status = self._send(chunk)
            if status in ("cancelled", "pending"):
                return
            if status == "transient":
                self._backoff = min(max(self._backoff * 2, BACKOFF_INITIAL), BACKOFF_MAX)
                self._retry_at = time.monotonic() + self._backoff
                self._set_status("error", f"Hoshi text couldn't be parsed ({chunk.error}); retrying in {int(self._backoff)}s.")
                break
            if status == "permanent":
                log.warning("hoshi: %s: dropping a chunk the reader API rejected: %s", r.folder, chunk.error)
            else:
                self._backoff = 0.0
                with self._lock:
                    self._credited[r.day] = self._credited.get(r.day, 0) + (lines[-1][1] - r.start)
                self._set_status("watching")
            self._advance(index, lines[-1][1])
            sent += size
            if self._stop.wait(self._spacing):
                break

    def _send(self, chunk):
        """Hand one chunk to the recorder and wait for its answer. On stop, cancel
        it: the chunk lock guarantees that a chunk is either recorded (and then
        committed here) or never recorded at all."""
        self._in_flight = chunk
        try:
            self._submit(chunk)
            while not chunk.done.wait(0.5):
                if self._stop.is_set():
                    break
            if self._stop.is_set():
                status = chunk.cancel()
                return status if status == "recorded" else "cancelled"
            return chunk.status
        finally:
            self._in_flight = None

    def _advance(self, index, new_start):
        with self._lock:
            r = self.state.pending[index]
            r.start = new_start
            if r.start >= r.end:
                del self.state.pending[index]
        self._save_state()

    def _drop(self, index):
        with self._lock:
            del self.state.pending[index]
        self._save_state()

    # --- state file ---------------------------------------------------------

    def _load_state(self):
        raw, ok = _read_json(self.settings.state_path)
        if isinstance(raw, dict):
            try:
                return policy.State.from_json(raw)
            except (KeyError, TypeError, ValueError) as err:
                log.warning("hoshi: state file unreadable (%s); starting fresh", err)
        elif not ok:
            log.warning("hoshi: state file unreadable; starting fresh")
        # Fresh state: everything already in the library is baselined on the
        # first cycle, so reading done before now is never credited.
        state = policy.State(installed_at=self._now_ms())
        self.state = state
        self._save_state()
        return state

    def _save_state(self):
        with self._lock:
            text = json.dumps(self.state.to_json(), ensure_ascii=False, separators=(",", ":"))
        try:
            _write_atomic(self.settings.state_path, text)
        except OSError as err:
            self._set_status("error", f"Can't save Hoshi progress: {err}")
