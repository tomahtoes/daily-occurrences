"""Small pure-text helpers: frame parsing, whitespace flattening, day keys, and
the rolling dedup window. No Anki or network imports here, so this module is
trivially unit-testable on its own.
"""

import json
import re
from collections import deque
from datetime import datetime, timedelta

# Incoming frames may be a bare string, a JSON object carrying the sentence under
# one of these keys, or a JSON array of either. The first key that is present
# wins.
_TEXT_KEYS = ("sentence", "text", "content", "message")

# Any run of whitespace that contains a line break is removed entirely; runs
# without a line break are left intact. Trailing/leading whitespace is then
# trimmed.
_NEWLINE_RUN = re.compile(r"\s*[\r\n]+\s*")


def _text_from_json(value):
    """Pull the text out of one decoded JSON value, or return None."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            found = value.get(key)
            if isinstance(found, str):
                return found
    return None


def extract_lines(raw):
    """Parse one raw frame into every non-empty text line it carries.

    Tries JSON first: an array yields one candidate per element, any other JSON
    value is a single candidate. Non-JSON input is treated as a single trimmed
    raw line.
    """
    try:
        parsed = json.loads(raw)
    except ValueError:
        stripped = raw.strip()
        return [stripped] if stripped else []

    sources = parsed if isinstance(parsed, list) else [parsed]
    lines = []
    for value in sources:
        text = _text_from_json(value)
        if text:
            lines.append(text)
    return lines


def flatten_text(text):
    """Collapse line breaks: drop any whitespace run containing a newline, keep
    newline-free whitespace runs, then trim the ends."""
    return _NEWLINE_RUN.sub("", text).strip()


def day_key(now, cutoff_hour):
    """The ``YYYY-MM-DD`` key for ``now`` given the cutoff hour. A day runs from
    ``cutoff_hour:00`` to the next ``cutoff_hour:00`` — anything before the
    cutoff belongs to the previous calendar day, so a late-night session stays in
    one dictionary.
    """
    return (now - timedelta(hours=cutoff_hour)).strftime("%Y-%m-%d")


def now_day_key(cutoff_hour):
    """Convenience: the day key for the current local time."""
    return day_key(datetime.now(), cutoff_hour)


class DedupWindow:
    """A rolling window of recently-seen lines, used to drop exact-repeat lines
    (re-sent clipboard / OCR frames) before they are parsed and counted."""

    def __init__(self, capacity):
        # Clamped here as well as by the caller: a negative capacity would make
        # the eviction check below always true and pop an empty deque, which
        # would raise on every single line rather than failing visibly.
        self.capacity = max(0, capacity)
        self._queue = deque()
        self._set = set()

    def observe(self, line):
        """Record ``line`` and report whether it is *new*. Returns False for an
        exact repeat still in the window. A capacity of 0 disables dedup."""
        if self.capacity == 0:
            return True
        if line in self._set:
            return False
        if len(self._queue) >= self.capacity:
            self._set.discard(self._queue.popleft())
        self._queue.append(line)
        self._set.add(line)
        return True
