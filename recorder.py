"""The recording engine.

Two background threads cooperate through a queue:

* the **websocket thread** keeps a connection to the text source alive (with
  exponential reconnect backoff and a keepalive ping that forces a reconnect when
  the peer goes silent), and pushes deduplicated lines onto a queue;
* the **worker thread** batches those lines, sends each batch to the reader API,
  records the resulting words into the current day's dictionary, and persists it
  to disk.

The engine can also be *paused*, in which case every arriving line is discarded in
``_ingest`` — nothing is buffered for later. The socket and its reconnect backoff
are left alone, so resuming costs nothing, and the worker keeps running, so lines
that arrived before the pause still flush and the day still rolls over.

Nothing here imports Anki — the engine takes a plain settings object and an
already-resolved output directory / cutoff hour, so it can be exercised on its
own.
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass

from .jiten import JitenClient, JitenError
from .store import DayDict, day_is_foreign, ensure_output_dir, purge_old_days
from .textutil import DedupWindow, extract_lines, flatten_text, now_day_key
from .vendor import websocket

log = logging.getLogger("daily_occurrences")

# Mirrors of the hardcoded engine limits (not user settings).
BATCH_CHAR_CAP = 4000          # hard cap on characters per reader request
RECONNECT_INITIAL = 1.0        # seconds; first wait after a drop
RECONNECT_MAX = 30.0           # seconds; backoff ceiling (doubles each failure)
KEEPALIVE_PING = 20.0          # seconds between keepalive pings on a live socket
KEEPALIVE_PONG_TIMEOUT = 10.0  # seconds to wait for a pong before reconnecting
_WORKER_TICK = 1.0             # seconds; worker wake cadence for idle/rollover
_STABLE_CONNECTION = 60.0      # seconds a connection must last to reset the backoff
_PERSIST_MIN_INTERVAL = 5.0    # seconds; floor between saves of the same day


@dataclass
class Settings:
    """Fully-resolved engine settings (cutoff hour and output_dir already
    computed by the caller)."""

    websocket_url: str
    jiten_api_key: str
    jiten_base_url: str
    jiten_timeout_ms: int
    flush_every_lines: int
    idle_flush_seconds: int
    dedupe_window_lines: int
    max_line_length: int
    day_cutoff_hour: int
    delete_after_days: int
    output_dir: str


class _State:
    """Shared state read by the summary window; guarded by a single lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False
        self.current = None  # DayDict
        self.error = None    # last user-facing problem, or None

    def snapshot(self):
        with self.lock:
            day = self.current.day if self.current else "—"
            distinct = self.current.unique_words() if self.current else 0
            total = self.current.total_occurrences() if self.current else 0
            return {
                "connected": self.connected,
                "day": day,
                "distinct": distinct,
                "total": total,
                "error": self.error,
            }


class Recorder:
    def __init__(self, settings):
        self.settings = settings
        self._state = _State()
        self._stop = threading.Event()
        self._queue = queue.Queue()
        self._dedup = DedupWindow(settings.dedupe_window_lines)
        self._paused = threading.Event()  # set = drop arriving lines at ingest
        self._jiten = JitenClient(
            settings.jiten_base_url, settings.jiten_api_key, settings.jiten_timeout_ms
        )
        self._app = None
        self._backoff = RECONNECT_INITIAL
        self._connected_at = None  # websocket thread only; no lock needed
        self._ws_thread = None
        self._worker_thread = None
        # Serializes writes: stop() persists from the calling thread while the
        # worker may be persisting too.
        self._write_lock = threading.Lock()
        self._dirty = False
        self._last_persist = 0.0
        self._day_blocked = False  # today's folder belongs to someone else

    # --- lifecycle ----------------------------------------------------------

    def start(self):
        # The one place the output directory is created. Every later write
        # requires it to already exist, so a save can't rebuild a folder the user
        # (or Anki, deleting the add-on) has removed.
        try:
            ensure_output_dir(self.settings.output_dir)
        except OSError as err:
            self._set_error(f"Can't create {self.settings.output_dir}: {err}")

        # Load (or create) today's dictionary up front so the summary window has
        # real numbers immediately and counts resume across restarts.
        day = now_day_key(self.settings.day_cutoff_hour)
        loaded = self._adopt_day(day)
        if loaded is not None and not loaded.is_empty():
            log.info(
                "resumed %s (%d words, %d occurrences)",
                day, loaded.unique_words(), loaded.total_occurrences(),
            )

        if not self.settings.jiten_api_key:
            log.warning("no Jiten API key set; recording will not work until one is configured")

        self._ws_thread = threading.Thread(
            target=self._ws_loop, name="daily-occ-ws", daemon=True
        )
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="daily-occ-worker", daemon=True
        )
        self._ws_thread.start()
        self._worker_thread.start()
        log.info("started; reading %s", self.settings.websocket_url)

    def stop(self):
        self._stop.set()
        app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        # Save immediately from the caller so a close/restart never loses
        # recorded data even if a worker API request is still in flight.
        self._persist()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=3.0)
        if self._ws_thread is not None:
            self._ws_thread.join(timeout=2.0)

    def state(self):
        return self._state

    def set_paused(self, value):
        """Pause or resume ingestion. While paused, arriving lines are discarded
        in ``_ingest``: the socket stays up and the worker keeps flushing whatever
        was already queued. Safe from any thread, and before ``start()``.

        Deliberately silent — the caller owns the pause and logs it, so an engine
        rebuild re-applying the flag doesn't look like a user action."""
        if value:
            self._paused.set()
        else:
            self._paused.clear()

    # --- error reporting ----------------------------------------------------

    def _set_error(self, message):
        """Record the problem the summary window shows, or clear it with None.

        Without this the three ways recording can fail — a rejected key, an
        unreachable API, a response we can't read — all look identical from the
        outside: connected, zero words, no explanation. Logs only on change, so a
        long outage doesn't fill the log file."""
        with self._state.lock:
            changed = self._state.error != message
            self._state.error = message
        if changed and message:
            log.warning("%s", message)

    def _adopt_day(self, day):
        """Make ``day`` the current dictionary, loading whatever was already
        recorded for it. Returns what was loaded, or None.

        If the folder holds a dictionary this add-on didn't write, the day is
        *blocked*: nothing is loaded from it and ``_persist`` won't write to it.
        Words still accumulate in memory, so clearing the conflict loses
        nothing — and leaving it in place destroys nothing."""
        blocked = day_is_foreign(self.settings.output_dir, day)
        self._day_blocked = blocked
        if blocked:
            self._set_error(
                f"{DayDict.day_dir(self.settings.output_dir, day)} holds a "
                "dictionary this add-on didn't write — not recording to it."
            )
            loaded = None
        else:
            # Clears a previous day's blocked-folder message, which names a path
            # that is no longer the one being recorded to.
            self._set_error(None)
            loaded = DayDict.load_from_dir(self.settings.output_dir, day)
        with self._state.lock:
            self._state.current = loaded if loaded is not None else DayDict(day)
        return loaded

    # --- websocket thread ---------------------------------------------------

    def _ws_loop(self):
        self._backoff = RECONNECT_INITIAL
        while not self._stop.is_set():
            app = websocket.WebSocketApp(
                self.settings.websocket_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._app = app
            try:
                app.run_forever(
                    ping_interval=KEEPALIVE_PING,
                    ping_timeout=KEEPALIVE_PONG_TIMEOUT,
                    skip_utf8_validation=True,
                )
            except Exception as err:  # never let the supervisor thread die
                log.warning("websocket loop error: %s", err)
            self._app = None
            self._set_connected(False)

            if self._stop.is_set():
                break
            # A connection that lasted resets the ramp; one that dropped straight
            # away does not. Resetting on connect instead would mean a server
            # that accepts and immediately hangs up gets retried once a second
            # forever, because the backoff could never grow.
            opened_at, self._connected_at = self._connected_at, None
            if opened_at is not None and (time.monotonic() - opened_at) >= _STABLE_CONNECTION:
                self._backoff = RECONNECT_INITIAL
            # Disconnected: wait (interruptibly), then grow the backoff.
            if self._stop.wait(self._backoff):
                break
            self._backoff = min(self._backoff * 2.0, RECONNECT_MAX)

    def _on_open(self, _ws):
        # Same thread as _ws_loop, which reads it after run_forever returns.
        self._connected_at = time.monotonic()
        self._set_connected(True)
        log.info("connected to %s", self.settings.websocket_url)

    def _on_close(self, _ws, status_code, _msg):
        self._set_connected(False)
        if not self._stop.is_set():
            log.info("connection closed (%s)", status_code)

    def _on_error(self, _ws, err):
        self._set_connected(False)
        if not self._stop.is_set():
            log.warning("connection error: %s", err)

    def _on_message(self, _ws, message):
        if isinstance(message, (bytes, bytearray)):
            try:
                message = bytes(message).decode("utf-8")
            except UnicodeDecodeError:
                return
        self._ingest(message)

    def _ingest(self, text):
        # Paused: discard on arrival, above every other guard. Nothing is kept for
        # later, and the line never reaches the dedup window — so an identical line
        # arriving after the resume isn't shadowed by one we threw away.
        if self._paused.is_set():
            return
        max_len = self.settings.max_line_length
        for raw in extract_lines(text):
            line = flatten_text(raw)
            if not line:
                continue
            # Length-first guard: an over-long line (backlog dump / line-skip
            # blob) never enters the dedup window or a batch. Compared against
            # zero rather than tested for truthiness, so a negative limit means
            # "no limit" instead of silently discarding every line.
            if max_len > 0 and len(line) > max_len:
                continue
            # Dedup before batching: repeats are never queued or sent.
            if not self._dedup.observe(line):
                continue
            self._queue.put(line)

    def _set_connected(self, value):
        with self._state.lock:
            self._state.connected = value

    # --- worker thread ------------------------------------------------------

    def _worker_loop(self):
        s = self.settings
        # Retention is enforced here rather than in start() so the disk walk
        # never runs on the UI thread.
        self._purge()
        batch = []
        batch_chars = 0
        last_recv = time.monotonic()

        while not self._stop.is_set():
            try:
                timeout = _WORKER_TICK
                if s.idle_flush_seconds > 0:
                    remaining = (last_recv + s.idle_flush_seconds) - time.monotonic()
                    timeout = max(0.0, min(remaining, _WORKER_TICK))

                try:
                    line = self._queue.get(timeout=timeout)
                except queue.Empty:
                    line = None

                if line is not None:
                    last_recv = time.monotonic()
                    batch.append(line)
                    batch_chars += len(line)
                    full = (
                        (s.flush_every_lines > 0 and len(batch) >= s.flush_every_lines)
                        or batch_chars >= BATCH_CHAR_CAP
                    )
                    if full:
                        self._flush_batch(batch)
                        batch, batch_chars = [], 0
                    continue

                # Idle tick: process whatever is batched if we've been quiet
                # long enough.
                now = time.monotonic()
                idle = s.idle_flush_seconds
                if idle > 0 and (now - last_recv) >= idle:
                    if batch:
                        self._flush_batch(batch)
                        batch, batch_chars = [], 0
                    last_recv = now
                # Keep the day current, and get recent words to disk, even with
                # no traffic at all.
                self._ensure_day()
                self._maybe_persist()
            except Exception:
                # One bad iteration must not end recording for the session. This
                # thread has no supervisor, and from the summary window a dead
                # worker looks exactly like having nothing to read.
                log.exception("worker iteration failed; continuing")
                batch, batch_chars = [], 0

        # Nothing to persist here: stop() already saved on the calling thread,
        # and that call is the one guaranteed to run. The small unsent tail
        # (lines queued or batched but never sent to the API) is dropped on
        # purpose — processing it would mean a network round-trip on the
        # close/restart path, risking a UI stall and a late write from an
        # instance that has already been superseded.

    def _flush_batch(self, batch):
        """Send a batch to the reader API and record its words."""
        try:
            parsed = self._jiten.parse_lines(batch)
        except JitenError as err:
            log.warning("reader API error, dropping %d line(s): %s", len(batch), err)
            self._set_error(err.user_message())
            return
        except Exception as err:
            log.warning("reader request failed, dropping %d line(s): %s", len(batch), err)
            self._set_error(f"Can't reach the Jiten API: {err}")
            return

        # Stopped while the request was in flight. Drop the result: stop() has
        # already persisted, and a replacement Recorder may already be recording
        # the same day — writing now would overwrite its counts with ours.
        if self._stop.is_set():
            return

        # Roll the day if the cutoff boundary passed, then record.
        self._ensure_day()
        recorded = False
        with self._state.lock:
            current = self._state.current
            for words in parsed:
                for headword, reading in words:
                    current.record(headword, reading)
                    recorded = True

        if recorded:
            self._dirty = True
            self._set_error(None)
            self._maybe_persist()
        else:
            # A whole batch yielding nothing is unremarkable once (pure
            # punctuation), but it is also exactly what a changed response shape
            # looks like: tokens that no longer line up with the vocabulary list
            # produce zero words for every line, forever, with nothing logged.
            self._set_error(
                "The reader API returned no usable words — recording may be broken."
            )

    def _ensure_day(self):
        """Roll over to a new day's dictionary if the cutoff boundary passed,
        finalizing the previous day first."""
        today = now_day_key(self.settings.day_cutoff_hour)
        with self._state.lock:
            current = self._state.current
            needs_roll = current is None or current.day != today
        if not needs_roll:
            return
        self._persist()  # finalize the previous day
        self._adopt_day(today)
        log.info("rolled over to %s", today)
        self._purge()

    def _purge(self):
        """Drop day folders past the retention window."""
        days = self.settings.delete_after_days
        if days <= 0:
            return
        try:
            removed = purge_old_days(
                self.settings.output_dir,
                days,
                now_day_key(self.settings.day_cutoff_hour),
            )
        except OSError as err:
            log.warning("purge failed: %s", err)
            return
        if removed:
            log.info("purged %d day(s) older than %d days", len(removed), days)

    def _maybe_persist(self):
        """Save if there is something new and enough time has passed.

        Every save rewrites the whole day, so saving after each batch got
        steadily more expensive as the day grew. Rate-limiting here also means a
        save still happens with ``flush_every_lines`` and ``idle_flush_seconds``
        both disabled, which used to defer every write to shutdown."""
        if not self._dirty:
            return
        if (time.monotonic() - self._last_persist) < _PERSIST_MIN_INTERVAL:
            return
        self._persist()

    def _persist(self):
        """Snapshot the in-memory dictionary under the lock, then write it off
        the lock. The write itself is serialized separately, because stop() calls
        this from the caller's thread while the worker may be calling it too."""
        if self._day_blocked:
            return
        with self._state.lock:
            snapshot = self._state.current.copy() if self._state.current else None
        if snapshot is None or snapshot.is_empty():
            return
        with self._write_lock:
            try:
                snapshot.write_to_dir(self.settings.output_dir)
            except OSError as err:
                log.error("flush failed for %s: %s", snapshot.day, err)
                return
        self._dirty = False
        self._last_persist = time.monotonic()
