"""Which stretches of a Hoshi book count as read, and on which day.

Hoshi keeps only the *latest* position per book (``bookmark.json``) plus reading
sessions (``statistics.json``: start, end, characters, seconds). Each observation
of a book is compared with the furthest position already credited (the book's
``high_water``), and the forward movement is split three ways:

* **explained** — movement Hoshi's sessions account for. Session character
  counts include jumps (the tracker adds every forward position change), so
  only the part the session *time* can back is credited: at most
  ``max_cpm`` characters per minute read. A TOC jump adds characters but almost
  no time, so it credits almost nothing.
* **unexplained** — movement with no session behind it. Stats were off or
  forgotten; it is assumed read and credited in full.
* the rest of the explained movement is the time-proven skip, and is not
  credited.

The credited amount is the *tail* of the movement — what was on screen most
recently — so the skipped part comes off the head.

Invariants a caller must not break:

* every position of a book is credited at most once, ever: ``high_water`` only
  moves forward, and the ranges planned from an observation must be persisted in
  the same write as the advanced ``high_water``;
* a book is identified by ``(folder, total characters)``, never Hoshi's id, so
  re-importing or re-downloading the same book can't re-credit it;
* nothing read before the state was first created is credited (``installed_at``).

Pure: no I/O, no clock — ``now`` and every input are passed in.
"""

from dataclasses import dataclass, field
from datetime import datetime

from .textutil import day_key

APPLE_EPOCH = 978307200  # seconds from the Unix epoch to 2001-01-01 (Hoshi's bookmarks)
STATE_VERSION = 1


@dataclass
class Session:
    started_ms: int
    chars: int
    seconds: float


@dataclass
class Observation:
    """One read of a book folder."""

    folder: str
    total: int               # metadata.characterCount (or bookinfo's)
    position: int            # bookmark.characterCount
    mark_ms: int             # bookmark.lastModified, Unix ms
    imported_ms: int         # metadata.modified; 0 for books synced from elsewhere
    sessions: dict = field(default_factory=dict)  # uuid -> Session


@dataclass
class Range:
    """Book positions ``[start, end)`` credited to ``day``."""

    folder: str
    total: int
    day: str
    start: int
    end: int

    def to_json(self):
        return {"folder": self.folder, "total": self.total, "day": self.day,
                "start": self.start, "end": self.end}

    @classmethod
    def from_json(cls, raw):
        return cls(str(raw["folder"]), int(raw["total"]), str(raw["day"]),
                   int(raw["start"]), int(raw["end"]))


@dataclass
class BookState:
    total: int
    high_water: int
    last_mark_ms: int
    seen: dict = field(default_factory=dict)  # uuid -> [chars, seconds] already accounted

    def to_json(self):
        return {"total": self.total, "high_water": self.high_water,
                "last_mark_ms": self.last_mark_ms, "seen": self.seen}

    @classmethod
    def from_json(cls, raw):
        seen = {str(k): [int(v[0]), float(v[1])] for k, v in (raw.get("seen") or {}).items()}
        return cls(int(raw["total"]), int(raw["high_water"]), int(raw.get("last_mark_ms") or 0), seen)


@dataclass
class State:
    installed_at: int
    books: dict = field(default_factory=dict)    # folder -> BookState
    pending: list = field(default_factory=list)  # Range, oldest first

    def to_json(self):
        return {
            "version": STATE_VERSION,
            "installed_at": self.installed_at,
            "books": {k: v.to_json() for k, v in self.books.items()},
            "pending": [r.to_json() for r in self.pending],
        }

    @classmethod
    def from_json(cls, raw):
        return cls(
            int(raw["installed_at"]),
            {str(k): BookState.from_json(v) for k, v in (raw.get("books") or {}).items()},
            [Range.from_json(r) for r in (raw.get("pending") or [])],
        )


def ms_day(ms, cutoff_hour):
    """The day key of a Unix-ms timestamp in local time — what ``plan``'s
    ``day_of`` is in production. Tests pass a fixed-offset one instead."""
    return day_key(datetime.fromtimestamp(ms / 1000.0), cutoff_hour)


def _new_since_install(obs, installed_at):
    """Whether a book seen for the first time was entirely read after install, so
    its whole position can be credited. Anything ambiguous is treated as old:
    crediting nothing for a first sight is recoverable (it's just a baseline),
    crediting a whole earlier read-through is not."""
    if obs.mark_ms <= installed_at:
        return False
    if obs.imported_ms and obs.imported_ms <= installed_at:
        return False
    return all(s.started_ms > installed_at for s in obs.sessions.values())


def _absorb_sessions(book, sessions):
    """Growth of each session since it was last accounted, chronological, as
    ``[(started_ms, new_chars, new_seconds)]``, and mark it accounted. The seen
    totals only ratchet up, so a session whose count dips (Hoshi clamps back
    movement into it) can't later re-credit the same characters."""
    grown = []
    for uuid, s in sessions.items():
        prev_chars, prev_seconds = book.seen.get(uuid, (0, 0.0))
        new_chars = max(0, s.chars - prev_chars)
        new_seconds = max(0.0, s.seconds - prev_seconds)
        if new_chars or new_seconds:
            grown.append((s.started_ms, new_chars, new_seconds))
        book.seen[uuid] = [max(prev_chars, s.chars), max(prev_seconds, s.seconds)]
    grown.sort()
    return grown


def plan(state, obs, day_of, max_cpm, keep_days, today):
    """Account one observation. Mutates ``state.books`` and returns the ranges to
    credit (also the caller's to persist). ``day_of`` maps a Unix-ms timestamp to
    its day key.

    Pause plays no part here. It exists for the websocket (stop recording what's
    on screen now); Hoshi reading is planned whenever it is observed, which can be
    long after it happened — a day of phone reading arriving through sync while
    recording is paused is still that day's reading."""
    book = state.books.get(obs.folder)
    if book is None or book.total != obs.total:
        if _new_since_install(obs, state.installed_at):
            book = BookState(obs.total, 0, state.installed_at)
        else:
            book = BookState(obs.total, obs.position, obs.mark_ms)
            _absorb_sessions(book, obs.sessions)
            state.books[obs.folder] = book
            return []
        state.books[obs.folder] = book

    grown = _absorb_sessions(book, obs.sessions)
    position = min(obs.position, obs.total) if obs.total else obs.position
    if position <= book.high_water:
        book.last_mark_ms = max(book.last_mark_ms, obs.mark_ms)
        return []

    delta = position - book.high_water
    explained = min(delta, sum(c for _, c, _ in grown))
    backed = [(started, min(c, seconds * max_cpm / 60.0)) for started, c, seconds in grown]
    tracked = int(min(explained, sum(b for _, b in backed)))
    unexplained = delta - explained

    book.high_water = position
    book.last_mark_ms = max(book.last_mark_ms, obs.mark_ms)

    # Lay the credit out as the tail [position - credit, position): the
    # unexplained part first, then the tracked parts in session order, the latest
    # session ending at the current position. Built backwards from the end so a
    # tracked total smaller than the sessions' backing trims the earliest ones.
    pieces = []
    cursor = position
    remaining = tracked
    for started, amount in reversed(backed):
        take = min(remaining, int(amount))
        if take <= 0:
            continue
        pieces.append((day_of(started), cursor - take, cursor))
        cursor -= take
        remaining -= take
    if remaining > 0:  # rounding left-over; give it to the latest session's day
        day = day_of(backed[-1][0])
        pieces.append((day, cursor - remaining, cursor))
        cursor -= remaining
    if unexplained > 0:
        pieces.append((day_of(obs.mark_ms), cursor - unexplained, cursor))
    pieces.reverse()

    ranges = []
    for day, start, end in pieces:
        if not within_retention(day, today, keep_days):
            continue
        last = ranges[-1] if ranges else None
        if last is not None and last.day == day and last.end == start:
            last.end = end
        else:
            ranges.append(Range(obs.folder, obs.total, day, start, end))
    return ranges


def within_retention(day, today, keep_days):
    """Whether ``day`` is still inside the retention window (and so worth
    recording). ``keep_days`` of 0 or less keeps everything."""
    if keep_days <= 0:
        return True
    try:
        age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(day, "%Y-%m-%d")).days
    except ValueError:
        return False
    return age <= keep_days
