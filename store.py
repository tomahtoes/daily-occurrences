"""The per-day occurrence store: an in-memory ``(headword, reading) -> count``
map persisted as an unpacked Yomitan dictionary folder named by day.

The folder's own files are the source of truth — on restart we reload by parsing
``term_meta_bank_*.json`` back into the map, so a day is never split across two
dictionaries.
"""

import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime

log = logging.getLogger("daily_occurrences")

# Max entries per term_meta_bank_N.json file.
MAX_ENTRIES_PER_BANK = 10_000

# Yomitan dictionary title prefix (shown in Yomitan's dictionary list).
TITLE_PREFIX = "Daily Occurrences"

# A day folder is named exactly YYYY-MM-DD. Only names matching this are ever
# considered by the purge.
_DAY_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DayDict:
    """One day's word counts, keyed by ``(headword, reading)``."""

    def __init__(self, day, counts=None):
        self.day = day
        self.counts = counts if counts is not None else {}
        # Running total, so total_occurrences() is O(1): the summary window polls
        # it every 1.5s while holding the lock the worker also needs.
        self._total = sum(self.counts.values())

    def record(self, headword, reading):
        """Increment the count for one observed word."""
        # An empty reading is normalized to the headword: both serialize to the
        # same compact entry, so keeping them as separate keys would write two
        # entries for one headword and silently lose a count on the next load.
        key = (headword, reading or headword)
        self.counts[key] = self.counts.get(key, 0) + 1
        self._total += 1

    def is_empty(self):
        return not self.counts

    def unique_words(self):
        return len(self.counts)

    def total_occurrences(self):
        return self._total

    def copy(self):
        return DayDict(self.day, dict(self.counts))

    @staticmethod
    def day_dir(output_dir, day):
        return os.path.join(output_dir, day)

    @classmethod
    def load_from_dir(cls, output_dir, day):
        """Load an existing day's dictionary. Returns None if the folder is
        absent, or if it holds a dictionary this add-on didn't write — see
        ``day_is_foreign``."""
        directory = cls.day_dir(output_dir, day)
        if not os.path.isdir(directory) or day_is_foreign(output_dir, day):
            return None
        counts = {}
        index = 1
        while True:
            bank = os.path.join(directory, f"term_meta_bank_{index}.json")
            if not os.path.isfile(bank):
                break
            try:
                with open(bank, "r", encoding="utf-8") as handle:
                    entries = json.load(handle)
            except (OSError, ValueError) as err:
                # A truncated bank (interrupted write, power loss) must not abort
                # the load: this also runs on the worker thread, where raising
                # would end recording for the rest of the session.
                log.warning("skipping unreadable %s: %s", bank, err)
                entries = None
            if isinstance(entries, list):
                for entry in entries:
                    parsed = _parse_entry(entry)
                    if parsed is not None:
                        headword, reading, count = parsed
                        key = (headword, reading)
                        # Summed rather than assigned, so a file written before
                        # readings were normalized (which could hold two entries
                        # for one headword) is repaired instead of halved.
                        counts[key] = counts.get(key, 0) + count
            index += 1
        return cls(day, counts)

    def write_to_dir(self, output_dir):
        """Write the day's Yomitan dictionary folder. Regenerates index.json and
        term_meta_bank_*.json from the current map and prunes now-unused banks.

        Raises OSError if ``output_dir`` itself is gone — see below."""
        # Only the day folder is created here, never the tree above it. Anki
        # deletes an add-on by removing its folder while Anki is running, and the
        # default output_dir lives inside it: a makedirs() of the full path would
        # resurrect user_files/ moments after the delete, leaving a stray add-on
        # folder behind. The parent is created once, by ensure_output_dir().
        if not os.path.isdir(output_dir):
            raise OSError(f"output directory no longer exists: {output_dir}")
        directory = self.day_dir(output_dir, self.day)
        os.makedirs(directory, exist_ok=True)

        index = {
            "title": f"{TITLE_PREFIX} {self.day}",
            "format": 3,
            "revision": str(int(time.time() * 1000)),
            "frequencyMode": "occurrence-based",
            "sequenced": False,
        }
        _write_atomic(
            os.path.join(directory, "index.json"),
            json.dumps(index, ensure_ascii=False, indent=2),
        )

        # Sorted keys keep bank output byte-stable across runs.
        entries = [
            _term_meta_entry(headword, reading, count)
            for (headword, reading), count in sorted(self.counts.items())
        ]

        chunks = [
            entries[i : i + MAX_ENTRIES_PER_BANK]
            for i in range(0, len(entries), MAX_ENTRIES_PER_BANK)
        ] or [[]]
        for idx, chunk in enumerate(chunks, start=1):
            _write_atomic(
                os.path.join(directory, f"term_meta_bank_{idx}.json"),
                json.dumps(chunk, ensure_ascii=False, separators=(",", ":")),
            )

        # Prune stale bank files from a previous, larger flush.
        stale = len(chunks) + 1
        while True:
            bank = os.path.join(directory, f"term_meta_bank_{stale}.json")
            if not os.path.isfile(bank):
                break
            try:
                os.remove(bank)
            except OSError:
                pass
            stale += 1


def purge_old_days(output_dir, keep_days, today):
    """Delete day folders more than ``keep_days`` days older than ``today``.

    ``keep_days`` of 0 or less disables the purge. Two guards keep this safe when
    ``output_dir`` is shared with another add-on (Priority Reorder's ``_seen``):
    only folders named exactly ``YYYY-MM-DD`` are considered, and only those whose
    ``index.json`` carries our title prefix — a dictionary some other tool put
    there is never touched. Returns the day keys removed.
    """
    if keep_days <= 0:
        return []
    try:
        today_date = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return []
    try:
        names = os.listdir(output_dir)
    except OSError:
        return []

    removed = []
    for name in names:
        if not _DAY_NAME.match(name):
            continue
        try:
            day = datetime.strptime(name, "%Y-%m-%d").date()
        except ValueError:  # well-formed but not a real date (2026-13-40)
            continue
        # Negative ages (a folder dated in the future) are always kept.
        if (today_date - day).days <= keep_days:
            continue
        directory = os.path.join(output_dir, name)
        if not os.path.isdir(directory) or not _is_ours(directory):
            continue
        try:
            shutil.rmtree(directory)
        except OSError:
            continue
        removed.append(name)
    return removed


def ensure_output_dir(output_dir):
    """Create the output directory. Called once at startup rather than on every
    write, so a later save can't rebuild a tree that Anki has since deleted."""
    os.makedirs(output_dir, exist_ok=True)


def day_is_foreign(output_dir, day):
    """Whether ``day``'s folder already holds a dictionary this add-on didn't
    write. Such a folder is neither read nor written: ``output_dir`` is meant to
    be shareable (Priority Reorder's ``_seen``), so absorbing someone else's
    counts — and re-titling their folder as ours, which would also make it
    eligible for the purge — is exactly what must not happen.

    A missing folder, or one with no ``index.json`` yet, is not foreign: there is
    nothing of anyone else's to protect, and the latter is what a half-finished
    write of our own leaves behind."""
    index_path = os.path.join(DayDict.day_dir(output_dir, day), "index.json")
    if not os.path.isfile(index_path):
        return False
    return not _is_ours(os.path.dirname(index_path))


def _is_ours(directory):
    """Whether a day folder was written by this add-on, judged by the title in
    its index.json. Unreadable or foreign folders report False so the purge
    leaves them alone."""
    try:
        with open(os.path.join(directory, "index.json"), "r", encoding="utf-8") as handle:
            index = json.load(handle)
    except (OSError, ValueError):
        return False
    title = index.get("title") if isinstance(index, dict) else None
    return isinstance(title, str) and title.startswith(TITLE_PREFIX)


def _term_meta_entry(headword, reading, count):
    """One Yomitan frequency entry. Kana-only words (reading == headword) use the
    compact value form; others carry the reading."""
    if not reading or reading == headword:
        return [headword, "freq", {"value": count, "displayValue": str(count)}]
    return [
        headword,
        "freq",
        {"reading": reading, "frequency": {"value": count, "displayValue": str(count)}},
    ]


def _freq_value(value):
    """A frequency value is either a bare number or ``{"value": N, ...}``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        inner = value.get("value")
        return int(inner) if isinstance(inner, (int, float)) and not isinstance(inner, bool) else None
    return None


def _parse_entry(entry):
    """Parse one Yomitan frequency entry into ``(headword, reading, count)``."""
    if not isinstance(entry, list) or len(entry) != 3 or entry[1] != "freq":
        return None
    headword = entry[0]
    data = entry[2]
    if not isinstance(headword, str):
        return None

    if isinstance(data, bool):
        return None
    if isinstance(data, (int, float)):
        return (headword, headword, int(data))
    if isinstance(data, dict):
        reading = data.get("reading", headword)
        if not isinstance(reading, str):
            reading = headword
        source = data["frequency"] if "frequency" in data else data
        count = _freq_value(source)
        if count is None:
            return None
        return (headword, reading, count)
    return None


def _write_atomic(path, text):
    # A unique temp name per writer, not a shared "<path>.tmp": stop() persists
    # from the calling thread while the worker may be persisting too, and a
    # shared name let the two truncate each other's file mid-write.
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path), prefix=os.path.basename(path) + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            # fsync before the rename, so a power loss can't leave behind a
            # zero-length file that a later load would have to recover from.
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
