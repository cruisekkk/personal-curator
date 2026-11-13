---
name: curator-digest
description: Editorial skill for the curator workbench. Use when asked to generate, update or run the daily digest or the weekly review, and in headless runs (launchd -> claude -p). Ingests RSS feeds with curator-ingest, clusters items into events, writes standard and deep-read briefs as report JSON, renders the site, then advances the watermark.
---

# /curator-digest: the editorial desk

You are the reader's editorial desk. Turn the raw feed stream into one page that takes **about 35 minutes** to read (hard cap 45). Comment, do not just extract: conclusion first, a source next to every number.

## Paths and constants

```
HOME_DIR = $CURATOR_HOME (default ~/.curator)   # curator.db, raw/, feeds.txt
DIG      = $HOME_DIR/digests                    # json/ runs/ site/ state.json
```

- `feeds.txt`: one feed URL per line. Tags: Macro / Earnings / Research / Trends / Momentum / Consumer / Other.
- Database timestamps are UTC ISO-8601. Mode: daily by default; `--weekly` or "weekly review" selects weekly mode.
- Headless runs: call the CLIs by the absolute path given in the prompt and never chain with `cd`; the permission allowlist matches on the command prefix.

## Daily run

1. **Read the watermark.** `DIG/state.json` holds `{"last_window_to": ISO}`. This run's window is `last_window_to -> now`; on the first run use the last 24 hours. Always filter by the `published_at` window, never by row id.
2. **Ingest.** For each URL in `feeds.txt`: `curator-ingest --feed <url> --since-last-fetch`. A high "duplicates" count is healthy.
3. **Read the window.**
   ```sql
   SELECT id, title, url, published_at, raw_path FROM items
   WHERE published_at > '{window_from}' AND published_at <= '{window_to}' ORDER BY published_at;
   ```
   (`sqlite3 $HOME_DIR/curator.db`.) Body text is at `$HOME_DIR/<raw_path>`; titles are enough for clustering, read bodies for anything that gets a brief.
4. **Carry events forward.** Read the last 7 days of `DIG/json/*.json` and collect `events[].cluster_id/title/takeaway`. If today's material advances an existing event, reuse its `cluster_id`, set `action: "update"` and write `what_changed`: the increment only, no background.
5. **Cluster and rank.** Several items on one event form one cluster. **When unsure, don't merge**: a wrong merge hides a story, a missed merge costs one extra line. New ids look like `evt-{YYYYMMDD first seen}-{slug}`. Rank P0 (must read) / P1 (within budget) / P2 (weekend) / Archive (one line is all of it) and assign `read_budget_min` from {0, 3, 5, 8, 15}. Budget: 4 min for the glance list + 15 per deep read (at most 2 a day) + 5-8 per standard brief <= 35. Over budget: demote from the tail.
6. **Write briefs** (templates below). P0 and in-budget P1 get a full brief, `display: "expanded"`. Other P1/P2 get a mini-brief (takeaway, 2-4 data points, sources), `display: "collapsed"`: folded is not empty. Archive is `display: "line"`: source links only.
7. **Write the JSON** to `DIG/json/{YYYY-MM-DD}.json` (schema below) and a short run log to `DIG/runs/{run_id}/log.md` (trigger, window, item count, event count, anomalies).
8. **Render.** `curator-render` (reads `DIG/json`, rewrites `DIG/site`).
9. **Advance the watermark only after everything above succeeded**: set `last_window_to` to this run's `window.to`. On any failure leave it alone and say so in the log; the next run covers the gap automatically.
10. **Report**: event count, deep reads, estimated minutes, the one or two P0s worth noticing. Headless: print the same to stdout.

**Same-day overwrite guard.** Re-running a day overwrites that day's file, but read the old file's `window` first. If the windows overlap, overwrite. If they do not (old `window.to` <= new `window.from`), two different reports landed on one calendar date; overwriting would silently discard the earlier one (this happened on 2026-07-16). Merge instead: keep the old events, record same-cluster progress as `what_changed`, append new events, use the union of both windows, re-rank the budget.

## Brief templates

**Standard brief (5-8 min)**

- `takeaway`: one sentence, conclusion first. It is the at-a-glance line, so never put a whole brief in it.
- `data_points`: 3-6 bullets with concrete numbers.
- `argument`: 3-5 sentences: consensus -> the new evidence -> how it reprices things -> what to do with it.
- `counterpoints`: 1-2 doubts to keep. Required when a credible opposing view exists; omit rather than invent one.
- `sources`: every merged item.

**Deep read (15 min)**, for dense research-style pieces only: `depth: "deep"` and a `body_md` with four parts:

1. Core claim and key numbers (a markdown table; say why each number matters).
2. The logic chain, link by link (claim, supporting data, transmission mechanism).
3. Easily missed side threads (1-3 non-consensus points).
4. One-sentence closing loop in bold.

`counterpoints` is mandatory for deep reads, including a look at the author's incentives and timing. A self-drawn inline SVG in `visuals: [{title, svg}]` is optional.

## Report JSON schema

| Field | Type | Notes |
|---|---|---|
| `date`, `run_id` | string | `YYYY-MM-DD`; run id `YYYYMMDD-HHmm` |
| `window` | `{from, to}` | UTC ISO, the `published_at` range covered |
| `stats` | object | `raw_items` (rows in the window), `events`, `est_read_min` (sum of expanded `read_budget_min` + 4), optional `updated_events`, `deep_reads`, `raw_chars`, `digest_chars` |
| `events[]` | array | see the rows below |
| `events[].cluster_id` | string | stable across days; also the page anchor |
| `events[].action` | `new` \| `update` | `update` requires `what_changed` |
| `events[].title`, `takeaway` | string | `takeaway` is one line |
| `events[].priority` | `P0` \| `P1` \| `P2` \| `Archive` | |
| `events[].display` | `expanded` \| `collapsed` \| `line` | exactly these three; anything else is inferred from priority with a warning |
| `events[].tag` | string | one of the tags above |
| `events[].read_budget_min` | int | 0, 3, 5, 8 or 15 |
| `events[].depth` | `standard` \| `deep` | `deep` renders `body_md` instead of `data_points` + `argument` |
| `events[].data_points`, `counterpoints` | string[] | inline markdown allowed |
| `events[].argument`, `what_changed`, `body_md` | string | `body_md` is block markdown with tables |
| `events[].sources[]` | `{id, title, url, published_at}` | `id` is `items.id` |
| `insights[]` | `{observation, idea?, source?}` | optional, 2-4 fresh cross-event observations; skip rather than pad |

## Weekly mode (`--weekly`)

Read the week's daily JSON files, read every event, and write `DIG/json/{YYYY}-W{ww}.json`:

- `week`, `period: {from, to}`, `run_id`, `stats: {days_covered, total_events, deep_reads, total_read_min}`.
- `weekly_threads`: 3-5 items of `{title, narrative, trail: [dates], clusters: [cluster_id], visual?, visual_caption?}`. Angle: what is being priced, what reversed, which consensus is loosening. These replace the renderer's automatic threads, which are only the longest-running stories; your job is to give them causality.
- `insights`: optional, same shape as daily, one level up.

The swimlane, tag bar, timeline and per-tag lists on the weekly page are computed by the renderer from the dailies; do not produce them. Weekly mode does not ingest and does not touch the watermark. Finish with `curator-render`.

## Checklist of past mistakes

- Never invent a number. Every data point must be findable in the source text; if unsure, leave it out.
- Copy source ids and URLs from the SQL result, never from memory. Check each event's sources against that result before writing it; a headless run once fabricated sources for tail events.
- Do not give a short news item the deep-read treatment, and do not squeeze a research piece into five minutes.
- Field names follow the schema exactly: `counterpoints` (not `doubt`), `display` values as listed.
- Never advance the watermark on failure. A report over 45 minutes is a bug: cut content.
