"""The add-on's log file: rotating, inside ``user_files``, and never held open
between records.

Anki updates and deletes an add-on by removing its folder while Anki is running.
Windows refuses to delete a file that any process still has open, so a plain
``RotatingFileHandler`` — which keeps its handle for the life of the process —
made this add-on impossible to remove or update from the add-on manager:

    Unable to update or delete add-on. [WinError 32] The process cannot access
    the file because it is being used by another process:
    ...\\addons21\\daily_occurrences\\user_files\\daily-occurrences.log

The handler below drops the file after every record and reopens it on the next
one, so the window in which the log is open is a few microseconds per record
rather than the whole session. The extra open/close costs nothing here: this
add-on logs on start, stop, config change, rollover and errors — a handful of
records per session, not one per line of text.
"""

import logging
import logging.handlers
import os

LOG_NAME = "daily_occurrences"

_FILENAME = "daily-occurrences.log"
_MAX_BYTES = 512 * 1024
_BACKUP_COUNT = 1


class ReleasingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating file handler that holds no OS handle between records."""

    def emit(self, record):
        try:
            super().emit(record)
        finally:
            self._release()

    def _release(self):
        """Drop the open file *without* marking the handler closed, so the next
        record just reopens it — which ``delay=True`` already handles. Calling
        ``close()`` here instead would retire the handler after one record.

        Runs under the handler lock: ``Handler.handle`` holds it across ``emit``.
        """
        stream = self.stream
        if stream is None:
            return
        self.stream = None
        try:
            stream.close()
        except Exception:  # noqa: BLE001 - a log write must never raise upward
            pass


def setup_logging(user_files_dir):
    """Attach the file handler once, and return the add-on's logger."""
    log = logging.getLogger(LOG_NAME)
    if getattr(log, "_configured", False):
        return log
    log.setLevel(logging.INFO)
    handler = ReleasingRotatingFileHandler(
        os.path.join(user_files_dir, _FILENAME),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,  # nothing is opened until there is something to write
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.propagate = False
    log._configured = True
    return log
