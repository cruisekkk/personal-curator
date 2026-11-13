English | [简体中文](README.zh-CN.md)

# personal-curator

A small personal workbench that turns a stream of news feeds into one page a day and one page a week.

Python does the mechanical parts (collecting, storing, rendering). A written editorial skill, followed by [Claude Code](https://claude.com/claude-code), does the judgment part (what is one event, what matters, what to doubt). The two meet at a plain JSON file, so either side can be replaced.

## Design: four stages

```
Collect  ──►  Interpret  ──►  Keep  ──►  Present
RSS/Atom      editorial       SQLite +    static HTML
              skill (LLM)     text + JSON (Jinja2)
```

| Stage | What happens | Where |
|---|---|---|
| **Collect** | Fetch RSS/Atom feeds, convert every entry to one `RawItem` shape, dedupe on `(source, external_id)`. | `src/curator/ingest/`, `curator-ingest` |
| **Interpret** | Cluster items into events, carry stories across days with a stable `cluster_id`, rank them P0/P1/P2/Archive against a reading budget (35 minutes by default), write briefs with data points, an argument and the doubts worth keeping. | `skill/SKILL.md` |
| **Keep** | Items in SQLite, body text as plain files, reports as JSON. The report JSON is the single source of truth; the site can always be rebuilt from it. | `src/curator/store.py`, `$CURATOR_HOME` |
| **Present** | Render the JSON into a static site: daily pages, weekly pages with a swimlane of multi-day stories, and an index. No server, no JavaScript framework. | `src/curator/render.py`, `curator-render` |

The interpreter never writes HTML and the renderer never calls a model. That boundary is the point: a bad report can be fixed by editing JSON, and a design change never needs a re-run of the model.

## Quick start: render the bundled example

Requires Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/curator-render --digests-dir examples/digests
open examples/digests/site/index.html        # macOS; otherwise open the file in a browser
```

`examples/digests/json/` holds three daily reports (2026-03-02 to 2026-03-04) and one weekly report (`2026-W10`). All of it is synthetic: the companies, numbers and links are made up. Open the weekly page to see the swimlane: three stories run across several days because their events share a `cluster_id`.

Run the tests and the linter:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

## Using it on your own feeds

Data lives in `$CURATOR_HOME` (default `~/.curator`):

```
curator.db                     sources + items
raw/YYYY-MM-DD/<id>.txt        body text of each item
feeds.txt                      one feed URL per line (read by the skill)
digests/json/                  report JSON: YYYY-MM-DD.json, YYYY-Www.json
digests/site/                  rendered site (derived, safe to delete)
digests/state.json             watermark: {"last_window_to": "..."}
```

1. Collect: `.venv/bin/curator-ingest --feed https://example.com/feed.xml --since-last-fetch` (repeat `--feed` for more feeds).
2. Interpret: install the skill, then ask for a report in Claude Code.
   ```bash
   mkdir -p ~/.claude/skills/curator-digest
   cp skill/SKILL.md ~/.claude/skills/curator-digest/SKILL.md
   ```
   In a Claude Code session started in this directory: `/curator-digest` for the daily report, `/curator-digest --weekly` for the weekly review.
3. Present: `.venv/bin/curator-render` (the skill runs this as its last step).

### Report JSON in one paragraph

A daily report has `date`, `run_id`, `window`, `stats`, `events[]` and optional `insights[]`. Each event has a `cluster_id`, `action` (`new` or `update`, the latter with `what_changed`), `priority`, `tag`, `read_budget_min`, and `display`. `display` is exactly one of:

- `expanded`: a full brief, open by default, counted in the reading budget
- `collapsed`: a mini-brief in a folded card
- `line`: source links only, at the bottom of the page

A missing or unknown `display` is not an error: the renderer infers it from `priority` (Archive → `line`, P0 → `expanded`, otherwise `collapsed`) and prints a warning. The full schema is the table in `skill/SKILL.md`.

## How the scheduled run works

`scripts/` contains the wrappers that run the skill unattended on macOS with launchd. They exist because "run it every morning" fails in several quiet ways on a laptop:

- **Asleep at the scheduled time.** A fixed-time job is skipped if the lid is closed. The job therefore polls every two hours and at login, and `daily_digest_cron.sh` exits immediately when today's report already exists (idempotent guard).
- **A run that never ends.** One run hung for 69 days. Every model call now runs in its own process group under a hard timeout.
- **Two runs at once, or a dead run holding the lock.** A `mkdir` lock records its PID; a lock whose holder is gone is reclaimed.
- **Expired login.** Background runs use a long-lived token stored in the macOS keychain (service `curator-claude-oauth-token`), and a health check runs before the real job. The check looks for a line that is exactly `OK`, because the CLI may print warnings before its answer.
- **Transient errors versus real ones.** Network and overload errors are retried with backoff; an auth error notifies you at once; everything else notifies only after 12 hours, at most once per 12 hours.
- **A clean exit that produced nothing.** Success means "the watermark moved", not "exit code 0". The skill advances the watermark only after every step succeeded.

Setup:

```bash
# 1. Store a long-lived token in the keychain (interactive, once)
bash scripts/setup_cron_auth.sh

# 2. Fill in the two placeholders in the plist and load it
sed -e "s|/path/to/curator|$PWD|g" -e "s|/path/to/home|$HOME|g" \
  scripts/launchd/com.example.curator.daily-digest.plist \
  > ~/Library/LaunchAgents/com.example.curator.daily-digest.plist
launchctl load -w ~/Library/LaunchAgents/com.example.curator.daily-digest.plist
```

The plist ships with `/path/to/curator` (this repository) and `/path/to/home` (your home directory) because launchd does not expand `~` or `$HOME`. For the weekly review, copy the plist, change the label and the script to `weekly_digest_cron.sh`, and set `StartInterval` to `10800`; the script itself only acts on Sundays.

The wrappers read these environment variables, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `CURATOR_HOME` | `~/.curator` | data directory |
| `CURATOR_REPO` | the parent of `scripts/` | where `.venv/bin/curator-*` live |
| `CLAUDE_BIN` | `claude` on `PATH`, else `~/.local/bin/claude` | Claude Code CLI |
| `CURATOR_MODEL` | `opus` | model for the report run |
| `CURATOR_WEEK` | current ISO week | force a specific week in `weekly_digest_cron.sh` |

Logs go to `~/Library/Logs/curator-daily-digest.log`.

## Layout

```
src/curator/ingest/     RawItem, IngestSource, RSS collector
src/curator/store.py    SQLite + raw text files
src/curator/render.py   report JSON -> static site
src/curator/templates/  Jinja2 templates and style.css
skill/SKILL.md          the editorial skill
scripts/                unattended-run wrappers and the launchd plist
examples/digests/json/  synthetic reports
tests/                  pytest, no network
```

## License

MIT. See [LICENSE](LICENSE).
