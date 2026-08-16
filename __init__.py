"""Daily Occurrences — builds a daily Yomitan occurrence dictionary from a live
Japanese text stream, parsed via the Jiten reader API.

This module is the Anki glue: it wires configuration, the engine lifecycle, the
Tools-menu summary window, and config hot-reload. The engine itself lives in
``recorder.py`` and never imports Anki. It also owns the pause flag, which is user
intent rather than engine state — see ``_paused``.
"""

import logging
import os

from aqt import gui_hooks, mw
from aqt.qt import QAction

from . import summary
from .logsetup import LOG_NAME, setup_logging
from .recorder import Recorder, Settings

ADDON_DIR = os.path.dirname(__file__)

# The same logger object setup_logging() attaches the file handler to, so this is
# usable from import onward regardless of when setup runs.
log = logging.getLogger(LOG_NAME)

_recorder = None

# Whether the last _start() found no API key. Cached rather than re-read on every
# summary refresh, and used to explain the idle engine in the summary window.
_no_api_key = False

# Pause lives here rather than in the engine because _on_config_updated destroys
# and rebuilds the Recorder — an engine-owned flag would silently un-pause on every
# config save. It is deliberately not persisted anywhere: closing the profile
# resumes, so a pause you forget about can never cost more than one session.
_paused = False

_DEFAULTS = {
    "websocket_url": "ws://localhost:6677",
    "jiten_api_key": "",
    "jiten_base_url": "https://api.jiten.moe",
    "jiten_timeout_ms": 10000,
    "flush_every_lines": 50,
    "idle_flush_seconds": 30,
    "dedupe_window_lines": 500,
    "max_line_length": 150,
    "day_cutoff_hour": None,
    "delete_after_days": 60,
    "output_dir": "",
}


def _user_files():
    path = os.path.join(ADDON_DIR, "user_files")
    os.makedirs(path, exist_ok=True)
    return path


def _setup_logging():
    setup_logging(_user_files())


def _resolve_cutoff(value):
    """An explicit 0–23 wins; otherwise follow Anki's 'next day starts at'
    (rollover) preference, falling back to 4 if it can't be read."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 23:
        return value
    try:
        return int(mw.col.get_preferences().scheduling.rollover)
    except Exception:
        pass
    try:
        rollover = mw.col.get_config("rollover")
        if isinstance(rollover, int) and 0 <= rollover <= 23:
            return rollover
    except Exception:
        pass
    return 4


# Lowest sane value per numeric setting. Anything below is clamped rather than
# rejected: config.json is hand-edited, and a typo here used to break recording
# silently (a negative dedupe window raised on every line; a negative
# max_line_length dropped all of them).
_MINIMUMS = {
    "jiten_timeout_ms": 1000,
    "flush_every_lines": 0,
    "idle_flush_seconds": 0,
    "dedupe_window_lines": 0,
    "max_line_length": 0,
    "delete_after_days": 0,
}


def _int(cfg, key):
    try:
        value = int(cfg.get(key, _DEFAULTS[key]))
    except (TypeError, ValueError):
        log.warning("%s is not a number; using %s", key, _DEFAULTS[key])
        return _DEFAULTS[key]
    minimum = _MINIMUMS.get(key, 0)
    if value < minimum:
        log.warning("%s of %s is below the minimum; using %s", key, value, minimum)
        return minimum
    return value


def _build_settings():
    cfg = mw.addonManager.getConfig(__name__) or {}
    output_dir = (cfg.get("output_dir") or "").strip()
    if not output_dir:
        output_dir = os.path.join(_user_files(), "occurrence-dicts")
    return Settings(
        websocket_url=cfg.get("websocket_url") or _DEFAULTS["websocket_url"],
        jiten_api_key=cfg.get("jiten_api_key") or "",
        jiten_base_url=cfg.get("jiten_base_url") or _DEFAULTS["jiten_base_url"],
        jiten_timeout_ms=_int(cfg, "jiten_timeout_ms"),
        flush_every_lines=_int(cfg, "flush_every_lines"),
        idle_flush_seconds=_int(cfg, "idle_flush_seconds"),
        dedupe_window_lines=_int(cfg, "dedupe_window_lines"),
        max_line_length=_int(cfg, "max_line_length"),
        day_cutoff_hour=_resolve_cutoff(cfg.get("day_cutoff_hour")),
        delete_after_days=_int(cfg, "delete_after_days"),
        output_dir=output_dir,
    )


def _start():
    global _recorder, _no_api_key
    if _recorder is not None:
        return
    settings = _build_settings()
    _no_api_key = not settings.jiten_api_key
    if _no_api_key:
        # Every request would be rejected, so connecting buys nothing and costs
        # a steady trickle of doomed traffic against Jiten. _current_snapshot
        # surfaces this, so it isn't a silent no-op.
        log.warning("no Jiten API key set; not starting")
        return
    engine = None
    try:
        engine = Recorder(settings)
        # Apply the pause before start(): the threads don't exist yet, so a rebuild
        # while paused can't let lines slip through in between.
        engine.set_paused(_paused)
        engine.start()
    except Exception:
        log.exception("failed to start recorder")
        if engine is not None:
            # start() may have got one thread up before raising. stop() is safe
            # either way and joins whatever exists, so the threads don't outlive
            # the reference we're about to drop.
            try:
                engine.stop()
            except Exception:
                log.exception("error while cleaning up a failed start")
        return
    _recorder = engine


def _stop():
    global _recorder
    if _recorder is None:
        return
    try:
        _recorder.stop()
    except Exception:
        log.exception("error while stopping recorder")
    finally:
        _recorder = None


def _set_paused(value):
    """Pause or resume recording. The flag is kept here even when no recorder
    exists, so it still applies to the next one _start() builds."""
    global _paused
    value = bool(value)
    if value == _paused:
        return
    _paused = value
    if _recorder is not None:
        _recorder.set_paused(value)
    log.info("recording %s", "paused" if value else "resumed")


def _on_config_updated(_new_config):
    log.info("config updated; restarting engine")
    _stop()
    _start()


def _current_snapshot():
    if _recorder is not None:
        snap = _recorder.state().snapshot()
    else:
        snap = {
            "connected": False,
            "day": "—",
            "distinct": 0,
            "total": 0,
            "error": (
                "No Jiten API key set — recording is disabled. Add one in "
                "Tools → Add-ons → Daily Occurrences → Config."
                if _no_api_key
                else None
            ),
        }
    # Stamped on here rather than reported by the engine: this is the authority.
    snap["paused"] = _paused
    return snap


def _open_summary():
    summary.show_summary(mw, _current_snapshot, _set_paused)


def _on_profile_will_close():
    global _paused
    # Close the window before anything else: its "still paused?" question is modal,
    # and asking it mid-shutdown would strand Anki behind a dialog nobody asked for.
    summary.force_close()
    if _paused:
        _paused = False
        log.info("recording resumed (profile closing)")
    _stop()


def _install_menu():
    action = QAction("Daily Occurrences Summary", mw)
    action.triggered.connect(_open_summary)
    mw.form.menuTools.addAction(action)


_setup_logging()
_install_menu()
mw.addonManager.setConfigUpdatedAction(__name__, _on_config_updated)
gui_hooks.profile_did_open.append(_start)
gui_hooks.profile_will_close.append(_on_profile_will_close)
