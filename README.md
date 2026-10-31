# Daily Occurrences

An Anki add-on that builds a **daily occurrence dictionary** from your immersion. It connects to a websocket that streams the Japanese text you're reading (and can also follow the ebooks you read in [Hoshi Reader](https://github.com/Manhhao/Hoshi-Reader-Desktop)), parses each line with the **Jiten reader API**, and writes a per-day frequency list of every word that appeared.

It's built to feed the **Priority Reorder** add-on: point its output at Priority Reorder's `_seen` folder and the words you read each day automatically become available to that add-on's `seen:` prioritization. See [Pointing it at Priority Reorder](#pointing-it-at-priority-reorder).

It runs quietly in the background whenever Anki is open, reconnecting on its own if the text source goes away and comes back. You can pause it at any time from [the summary window](#the-summary-window).

## Requirements

- A **Jiten account and API key**. Parsing runs on Jiten's servers, so each recorded line is sent to `api.jiten.moe` under your key.
- Something to read from: a websocket text source (a texthooker, clipboard inserter, OCR feed, or anything else that emits the sentences you're reading as websocket messages), and/or ebooks in [Hoshi Reader Desktop](https://github.com/Manhhao/Hoshi-Reader-Desktop) — see [Reading ebooks in Hoshi Reader](#reading-ebooks-in-hoshi-reader).

## Install

Install from [AnkiWeb](https://ankiweb.net/shared/info/294730816).

## Setup

**The only thing you have to configure is your Jiten API key.** Everything else has a working default; leave it alone unless you have a specific reason not to.

1. Generate a Jiten API key: log in at [jiten.moe](https://jiten.moe), open your [settings page](https://jiten.moe/settings), and find the **API Key** section.
2. In Anki, open *Tools → Add-ons*, select **Daily Occurrences**, and click **Config**.
3. Paste the key into `jiten_api_key` and save.

The add-on starts recording immediately, as soon as it finds something on the websocket. Settings apply as soon as you close the config window — no restart needed.

Two optional settings are worth knowing about, but neither is required:

- `output_dir` — set this if you want the dictionaries to feed Priority Reorder. See [below](#pointing-it-at-priority-reorder).
- `websocket_url` — set this only if your text source isn't on the default `ws://localhost:6677`.

## What it produces

Daily dictionaries are written under the add-on's `user_files/occurrence-dicts/` folder (or wherever you point `output_dir`), one folder per day:

```
occurrence-dicts/
  2026-06-20/
    index.json
    term_meta_bank_1.json
```

Each `YYYY-MM-DD` folder is a complete Yomitan-format occurrence dictionary; words are stored against their occurrence counts. A day runs from your configured cutoff hour to the next (by default it follows Anki's "next day starts at" preference), so late-night sessions stay in one file.

Old day folders are deleted automatically after 60 days — see [`delete_after_days`](#delete_after_days).

## Pointing it at Priority Reorder

The primary intended use case for this add-on is to feed Priority Reorder's `seen:` search. That add-on reads daily occurrence dictionaries from a reserved `_seen` folder inside its own `user_files`, one subfolder per day named `YYYY-MM-DD` — exactly the layout this add-on writes. So rather than leaving the dictionaries in this add-on's folder, point `output_dir` straight at that `_seen` folder and every day's words land where Priority Reorder expects them.

To find the path, open *Tools → Add-ons* in Anki, select **Priority Reorder**, and click **View Files**. The target is the `user_files\_seen` subfolder of the folder that opens (it doesn't need to exist yet — it's created on the first write).

`config.json` is JSON, so on Windows you must either escape each backslash (`\\`) or use forward slashes:

```jsonc
"output_dir": "C:\\Users\\you\\AppData\\Roaming\\Anki2\\addons21\\<priority-reorder-id>\\user_files\\_seen"
// — or —
"output_dir": "C:/Users/you/AppData/Roaming/Anki2/addons21/<priority-reorder-id>/user_files/_seen"
```

Once set, `seen:1` in Priority Reorder matches today's words, `seen:7` the last week, and so on.

Sharing that folder is safe. A day folder that this add-on didn't write is never read, never overwritten, and never deleted — it's identified by the title inside its `index.json`. If something else already owns the folder for today's date, recording for that day is skipped rather than merged into it, and the summary window tells you so.

## Reading ebooks in Hoshi Reader

Set `hoshi_enabled` to `true` and ebooks you read in **[Hoshi Reader Desktop](https://github.com/Manhhao/Hoshi-Reader-Desktop)** are recorded alongside your websocket text, into the same daily dictionaries. Reading done in Hoshi on your phone counts too, once Hoshi Desktop has synced it.

Hoshi keeps each book's EPUB, your current position, and (if you let it) your reading sessions in its library folder. Every `hoshi_poll_minutes` the add-on looks at how far each book has moved since it last looked, cuts that stretch of text out of the EPUB, and records its words — into the day you read them, which for synced phone reading may be an earlier day.

- **Turn on statistics autostart in Hoshi's settings.** Hoshi counts a jump through the table of contents as "characters read", so the add-on uses each session's *reading time* to tell reading from skipping: movement faster than [`hoshi_max_chars_per_minute`](#hoshi_max_chars_per_minute) while stats were running is treated as a skip and not recorded.
- **Forgetting the stats costs you nothing.** Movement with no session behind it is assumed to have been read and is recorded in full, to the day of your last page turn.
- **Nothing is recorded twice.** Each stretch of a book is recorded once, ever: going back, re-reading, re-importing the book or Hoshi syncing old sessions later never adds the same text again.
- **Books you'd already read aren't backfilled.** When you turn this on, recording starts from wherever each book is at that moment.
- **Synced books need downloading.** A book you've only read on another device is waiting in the queue until Hoshi Desktop downloads it — open it there once.
- **Big backlogs trickle in.** A day of phone reading is sent to Jiten a few thousand characters at a time, a few seconds apart, and at most 40,000 characters a minute. The summary window shows what's still queued.
- **Run it in one program only.** If another program is also reading your Hoshi library into the same folder, the add-on notices and stays out of the way rather than count everything twice.

Set `websocket_url` to `""` if Hoshi is your only source.

## The summary window

*Tools → Daily Occurrences Summary* opens a small panel showing:

- a status dot — **solid green** when it's connected to the websocket, **solid red** when it isn't, and **split diagonally green/red** while recording is paused,
- the date it's recording for,
- how many distinct terms and total terms you've recorded today,
- with Hoshi enabled, how many books it's watching, how much ebook text it has recorded today, and anything still queued or waiting for a download,
- a **Pause recording** button,
- a red line describing the problem, if anything is stopping words from being recorded — a missing or rejected API key, an unreachable server, or a day folder in `output_dir` that belongs to something else. If words aren't appearing, this is the first place to look; the full detail goes to `user_files/daily-occurrences.log`.

### Pausing

Pause when you're about to read something you don't want in today's dictionary — re-reading a passage, testing your text hook, or just handing the machine to someone else.

While paused, text arriving on the websocket is thrown away as it comes in. Nothing is saved up and recorded later, so anything that arrives while paused simply never happened as far as the dictionary is concerned. The connection itself stays open, so resuming is instant.

Pausing doesn't affect Hoshi Reader. Ebook reading is still recorded while paused — including reading from your phone that happens to sync in while you're paused.

Text you read *before* pausing is still saved, so the counts may keep climbing for a few seconds after you click Pause before they settle.

**A pause isn't remembered across restarts.** Closing your profile or quitting Anki always resumes recording, so a pause you forget about can't cost you more than the one session. It *does* survive saving the config screen.

## Settings

**You (probably) should not need to change any of these, and I'd suggest not touching them unless you genuinely know what a given value does.**

The defaults aren't the product of careful measurement — they're values I picked because they're conservative. Parsing happens on Jiten's servers, and a text source can misbehave in ways that have nothing to do with how much you're actually reading: a badly configured texthooker, a page that dumps an entire backlog at once. The batching, dedup, and line-length limits exist so that garbage like that gets dropped locally instead of turning into a flood of pointless requests against Jiten.

Every setting is also documented inline on the config screen. A value that isn't a number, or is below the minimum a setting allows, is replaced with a sane one rather than accepted — the substitution is noted in `user_files/daily-occurrences.log`.

### `jiten_api_key`
**Required.** Your Jiten API key. Empty means recording is disabled.

### `websocket_url`
The websocket to read text from. Default `ws://localhost:6677`. Point this at whatever your text source exposes. `""` turns the websocket off.

### `output_dir`
Where daily dictionary folders are written. Empty (the default) uses `user_files/occurrence-dicts/` inside this add-on's folder. Set an absolute path to write elsewhere — typically Priority Reorder's `_seen` folder, see [Pointing it at Priority Reorder](#pointing-it-at-priority-reorder).

### `delete_after_days`
Delete a day's dictionary once it's more than this many days old, so the folder doesn't grow forever. Default `60`. `0` = keep everything.

Only folders named `YYYY-MM-DD` that this add-on wrote are ever removed, so it's safe to point `output_dir` at a folder you share with something else. Keep this at or above the largest `seen:N` you actually search with in Priority Reorder — a day that's been deleted can't be matched.

### `jiten_base_url`
Jiten API base URL. Leave as `https://api.jiten.moe` unless you self-host.

### `jiten_timeout_ms`
How long to wait for a Jiten response before giving up on that batch, in milliseconds. Default `10000`.

### `flush_every_lines`
Process after this many new lines — also the batch size sent per API request. `0` = only process on the idle timer below. Default `50`. (Regardless of this value, a batch is capped at ~4000 characters per request.)

Lowering it means more, smaller requests for the same amount of reading.

### `idle_flush_seconds`
Process after this many seconds with no new line. `0` = disable. Default `30`. This is what gets the tail of a reading session recorded once you stop.

Saving to disk is separate from either of these, and happens at most every few seconds while there's something new — so setting both to `0` still won't lose your day's words.

### `dedupe_window_lines`
Drop exact-repeat lines seen within this rolling window before parsing, so re-sent clipboard/OCR frames aren't counted twice or sent to the API twice. `0` = disable. Default `500`.

### `max_line_length`
Ignore any incoming line longer than this many characters, before it's deduped or parsed. This is the main guard against a bad hook or a backlog dump flooding the dictionary with text you never actually read. `0` = no limit. Default `150`.

### `day_cutoff_hour`
The hour (0–23) at which a new "day" starts, so a late-night session stays in one dictionary. `null` (the default) follows your Anki "next day starts at" preference. Set a number 0–23 to override it.

### `hoshi_enabled`
`true` to record ebooks read in Hoshi Reader. Default `false`. See [Reading ebooks in Hoshi Reader](#reading-ebooks-in-hoshi-reader).

### `hoshi_books_dir`
Hoshi's library folder. Empty (the default) finds it automatically: `%APPDATA%\hoshi-reader\Books` on Windows, `~/Library/Application Support/de.manhhao.hoshi/Books` on macOS.

### `hoshi_poll_minutes`
How often to check Hoshi, in minutes. Default `1`, minimum `1`.

### `hoshi_max_chars_per_minute`
The fastest reading speed the add-on will believe, in characters per minute. Default `1000`, minimum `100`. Movement faster than this while Hoshi's stats were running is treated as skipping ahead. It's a ceiling rather than an estimate, so it only needs raising if you genuinely read faster.

## Third-party code

The websocket client under `vendor/websocket/` is [websocket-client](https://github.com/websocket-client/websocket-client), bundled unmodified under the Apache License 2.0 (see `vendor/websocket/LICENSE`).
