# Daily Occurrences — settings

This add-on connects to a websocket that emits the Japanese text you're reading
(a texthooker, clipboard inserter, OCR feed, etc.), parses each line with the
**Jiten reader API**, and builds a daily **occurrence dictionary** on disk — a
frequency list of how often each word showed up in your immersion that day. Point
`output_dir` at the **Priority Reorder** add-on's `_seen` folder to feed that
add-on's `seen:` prioritization.

> **The only setting you have to fill in is `jiten_api_key`.** Everything else
> already has a working default.

Parsing happens on Jiten's servers, so every line you record is sent to
`api.jiten.moe` under your key, and the add-on does nothing until a key is set.

To generate one, log in at https://jiten.moe, open your settings page
(https://jiten.moe/settings), and find the **API Key** section.

After editing, settings apply as soon as you close this window (the engine
restarts automatically).

---

## A note on the defaults

**You should not need to change anything below `jiten_api_key`, and I'd suggest
leaving it all alone unless you genuinely know what a given value does.**

These numbers aren't the result of careful measurement — they're values I picked
because they're conservative. A text source can misbehave in ways that have
nothing to do with how much you're actually reading: a badly configured
texthooker, a clipboard inserter re-sending the same frame, a page that dumps a
whole backlog at once. The batching, dedup, and line-length limits exist so that
garbage like that is dropped locally instead of becoming a flood of pointless
requests against Jiten. Loosening them mostly buys you more requests, not more
words.

A value that isn't a number, or is below the minimum a setting allows, is
replaced with a sane one rather than accepted — the substitution is noted in
`user_files/daily-occurrences.log`.

---

### `websocket_url`
The websocket to read text from. Default `ws://localhost:6677`. Point this at
whatever your text source exposes.

### `jiten_api_key`
**Required.** Your Jiten API key. Empty means recording is disabled.

### `jiten_base_url`
Jiten API base URL. Leave as `https://api.jiten.moe` unless you self-host.

### `jiten_timeout_ms`
How long to wait for a Jiten response before giving up on that batch, in
milliseconds. Default `10000`.

### `flush_every_lines`
Process after this many new lines — also the batch size sent per API request.
`0` = only process on the idle timer below. Default `50`. (Regardless of this
value, a batch is capped at ~4000 characters per request.) Lowering it means
more, smaller requests for the same amount of reading.

### `idle_flush_seconds`
Process after this many seconds with no new line. `0` = disable. Default `30`.
This is what records the tail of a session once you stop reading.

Saving to disk is separate from both of these and happens at most every few
seconds while there's something new, so setting both to `0` still won't lose
your day's words.

### `dedupe_window_lines`
Drop exact-repeat lines seen within this rolling window before parsing, so re-sent
clipboard/OCR frames aren't counted twice or sent to the API twice. `0` = disable.
Default `500`.

### `max_line_length`
Ignore any incoming line longer than this many characters, before it's deduped or
parsed. This is the main guard against a bad hook or a backlog dump flooding the
dictionary with text you never actually read. `0` = no limit. Default `150`.

### `day_cutoff_hour`
The hour (0–23) at which a new "day" starts, so a late-night session stays in one
dictionary. **`null` (the default) follows your Anki "next day starts at"
preference.** Set a number 0–23 to override it.

### `delete_after_days`
Delete a day's dictionary once it's more than this many days old, so the folder
doesn't grow forever. Default `60`. `0` = keep everything. Only `YYYY-MM-DD`
folders this add-on wrote are ever removed, so a shared `output_dir` is safe.
Keep this at or above the largest `seen:N` you actually search with — a deleted
day can't be matched.

### `output_dir`
Where daily dictionary folders are written. Empty (the default) uses
`user_files/occurrence-dicts/` inside this add-on's folder. Set an absolute path
to write elsewhere — typically the **Priority Reorder** add-on's `user_files/_seen`
folder, so each day's words feed its `seen:` search. This is JSON, so on Windows
escape each backslash (`\\`) or use forward slashes.

---

Each day becomes a folder named `YYYY-MM-DD` containing that day's occurrence
dictionary. Open **Tools → Daily Occurrences Summary** to see whether you're
connected and how many words you've recorded today, or to pause recording for a
while — the dot there goes green/red split while you're paused.
