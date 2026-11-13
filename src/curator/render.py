"""``curator-render``: report JSON -> static HTML site (the "Present" stage).

Reads every daily (``YYYY-MM-DD.json``) and weekly (``YYYY-Www.json``)
report under ``<digests>/json/`` and writes:

- ``site/daily/<date>.html``   the main reading surface
- ``site/weekly/<week>.html``  weekly rollup, aggregated from the dailies
  (``weekly_threads`` from the weekly JSON are merged in when present)
- ``site/index.html``          archive index
- ``site/assets/style.css``    copied from the package

Idempotent: every run rewrites everything under ``site/``. The JSON is
the single source of truth; never edit ``site/`` by hand.

Markdown policy: ``body_md`` and ``takeaway`` render as block markdown
(tables enabled); ``argument``, ``data_points``, ``counterpoints`` and
``what_changed`` render inline (outer ``<p>`` stripped).

``display`` is exactly one of ``expanded | collapsed | line``:

- ``expanded``  full brief, open by default, counted in the reading budget
- ``collapsed`` mini-brief in a folded card ("I may skip it, but it is there")
- ``line``      one line of source links at the bottom of the page

A missing or unknown value is normalised from ``priority`` (see
``normalize_event``) and reported on stderr.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path
from typing import Any

import markdown as md_lib
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from curator.store import default_home

TEMPLATES_DIR = Path(__file__).parent / "templates"
DAILY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WEEKLY_RE = re.compile(r"^\d{4}-W\d{2}$")
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "Archive": 3}
DISPLAY_VALUES = ("expanded", "collapsed", "line")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULT_TAG = "Other"
# Tag -> colour for the weekly charts. Mid-tone hues that read on both
# light and dark backgrounds, so no theme inversion is needed.
TAG_COLOR = {
    "Macro": "#6E8AD6", "Earnings": "#5DA88A", "Research": "#C77B5A",
    "Trends": "#9B7BC7", "Momentum": "#CC7088", "Consumer": "#C7A44E", "Other": "#8A8A8A",
}
FALLBACK_COLOR = "#8A8A8A"


# ---- markdown filters ---------------------------------------------


def md_block(text: str | None) -> Markup:
    """Full block markdown with tables: deep-read bodies and takeaways."""
    if not text:
        return Markup("")
    return Markup(md_lib.markdown(text, extensions=["tables"]))


_P_WRAP = re.compile(r"^<p>(.*)</p>$", re.DOTALL)


def md_inline(text: str | None) -> Markup:
    """Inline markdown (links/bold) with the outer <p> stripped, so it can
    sit inside <li>/<summary>/<p> without nesting violations."""
    if not text:
        return Markup("")
    html = md_lib.markdown(text).strip()
    m = _P_WRAP.match(html)
    return Markup(m.group(1) if m and "<p>" not in m.group(1) else html)


_SENT_END = re.compile(r"[.!?](?=\s|$)")
_MD_BOLD = re.compile(r"\*{1,2}([^*]+)\*{1,2}")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def scan_line(takeaway: Any, cap: int = 140) -> str:
    """One-line conclusion for the at-a-glance list: the first sentence of
    ``takeaway``, hard-capped at ``cap`` characters.

    ``takeaway`` is supposed to be a single line, but reports occasionally
    arrive with a whole brief stuffed into it. The renderer trims
    defensively so the list stays scannable; the full text is still in the
    brief card one click away. Returns plain text (inline markdown
    stripped); the template escapes it.
    """
    s = _MD_LINK.sub(r"\1", str(takeaway or ""))
    s = _MD_BOLD.sub(r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    m = _SENT_END.search(s)
    if m and m.end() <= cap:
        return s[: m.end()]
    return s if len(s) <= cap else s[: cap - 1] + "…"


# ---- loading -------------------------------------------------------


def load_digests(json_dir: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (dailies newest-first, weeklies keyed by 'YYYY-Www')."""
    dailies: list[dict[str, Any]] = []
    weeklies: dict[str, dict[str, Any]] = {}
    for fp in sorted(json_dir.glob("*.json")):
        stem = fp.stem
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"warn: skipping {fp.name}: {exc}", file=sys.stderr)
            continue
        if DAILY_RE.match(stem):
            data.setdefault("date", stem)
            dailies.append(data)
        elif WEEKLY_RE.match(stem):
            weeklies[stem] = data
        else:
            print(f"warn: unrecognised report filename {fp.name}", file=sys.stderr)
    dailies.sort(key=lambda d: str(d.get("date")), reverse=True)
    return dailies, weeklies


# ---- week helpers --------------------------------------------------


def week_key(date_str: str) -> str:
    y, w, _ = date_cls.fromisoformat(date_str).isocalendar()
    return f"{y}-W{w:02d}"


def weekday_name(date_str: str) -> str:
    return WEEKDAYS[date_cls.fromisoformat(date_str).weekday()]


def week_days(week: str) -> list[date_cls]:
    """The seven dates (Mon..Sun) of ISO week 'YYYY-Www'. Raises ValueError."""
    monday = date_cls.fromisocalendar(int(week[:4]), int(week[6:]), 1)
    return [monday + timedelta(days=i) for i in range(7)]


# ---- per-page context builders ------------------------------------


def _sorted_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda e: (
            PRIORITY_ORDER.get(str(e.get("priority")), 9),
            -int(e.get("read_budget_min") or 0),
        ),
    )


def normalize_event(e: dict[str, Any]) -> dict[str, Any]:
    """Fall back to sane presentation fields instead of rendering a blank page.

    A rushed unattended run once wrote a whole report whose events had no
    ``display`` at all (2026-07-11: saved just before the timeout, 11
    events, page came out empty). So a missing or unknown ``display`` is
    inferred from priority: Archive -> line, P0 -> expanded, anything else
    -> collapsed. An event that carries ``body_md`` but no ``depth`` is
    treated as a deep read, otherwise its body would not show. The JSON on
    disk is never modified.
    """
    if e.get("display") not in DISPLAY_VALUES:
        pri = str(e.get("priority"))
        inferred = "line" if pri == "Archive" else ("expanded" if pri == "P0" else "collapsed")
        print(
            f"warn: event {e.get('cluster_id')!r} has display={e.get('display')!r}; "
            f"using {inferred!r}",
            file=sys.stderr,
        )
        e = {**e, "display": inferred}
    if not e.get("depth") and e.get("body_md"):
        e = {**e, "depth": "deep"}
    return e


def daily_ctx(d: dict[str, Any]) -> dict[str, Any]:
    events = [normalize_event(e) for e in (d.get("events") or [])]
    expanded = _sorted_events([e for e in events if e["display"] == "expanded"])
    collapsed = _sorted_events([e for e in events if e["display"] == "collapsed"])
    lines = [e for e in events if e["display"] == "line"]
    # Tag chips with counts, ordered by first appearance.
    tag_counts: dict[str, int] = {}
    for e in expanded + collapsed:
        tag = str(e.get("tag") or DEFAULT_TAG)
        tag_counts[tag] = tag_counts.get(tag, 0) + 1
    day = str(d.get("date"))
    return {
        "d": d,
        "date": day,
        "weekday": weekday_name(day),
        "week": week_key(day),
        "stats": d.get("stats") or {},
        "expanded": expanded,
        "collapsed": collapsed,
        "lines": lines,
        "tag_counts": tag_counts,
        "insights": d.get("insights") or [],
    }


def _truncate(s: Any, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def weekly_swimlane_svg(events: list[dict[str, Any]], week: str) -> str:
    """Story-evolution chart: one lane per *persistent* event (one that
    surfaced on >= 2 days this week), a dot on each day it appeared, a
    line across its active range. Pure geometry from the ``trail`` the
    weekly merge builds; no LLM. Coloured by tag. Returns "" when fewer
    than two persistent stories exist.

    This is the visual payoff of carrying ``cluster_id`` across days: the
    what-changed updates stitched together become a glanceable picture of
    which stories compound and which were one-offs.
    """
    try:
        days = week_days(week)
    except (ValueError, TypeError):
        return ""
    day_strs = [d.isoformat() for d in days]

    def in_week(e: dict[str, Any]) -> list[str]:
        return sorted(t for t in (e.get("trail") or []) if t in day_strs)

    rows = [e for e in events if len(in_week(e)) >= 2]
    rows.sort(key=lambda e: (-len(in_week(e)), PRIORITY_ORDER.get(str(e.get("priority")), 9)))
    rows = rows[:14]
    if len(rows) < 2:
        return ""

    width, label_w, header, row_h = 720, 230, 42, 30
    x0, x1 = label_w + 12, width - 26  # room for the last date label, which is centred on x1
    col_w = (x1 - x0) / 6
    height = header + len(rows) * row_h + 30

    def cx(ds: str) -> float:
        return round(x0 + day_strs.index(ds) * col_w, 1)

    p: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" class="swimlane"><title>Story evolution this week</title>'
    ]
    for i, d in enumerate(days):
        x = round(x0 + i * col_w, 1)
        p.append(
            f'<line x1="{x}" y1="{header - 10}" x2="{x}" y2="{height - 22}" '
            f'stroke="currentColor" stroke-width="0.5" opacity="0.1"/>'
        )
        p.append(
            f'<text x="{x}" y="20" text-anchor="middle" font-size="12" '
            f'fill="currentColor" opacity="0.7">{d.month:02d}-{d.day:02d}</text>'
        )
        p.append(
            f'<text x="{x}" y="33" text-anchor="middle" font-size="10" '
            f'fill="currentColor" opacity="0.4">{WEEKDAYS[i]}</text>'
        )
    for j, e in enumerate(rows):
        cy = round(header + j * row_h + row_h / 2, 1)
        color = TAG_COLOR.get(str(e.get("tag")), FALLBACK_COLOR)
        p.append(f'<circle cx="9" cy="{cy}" r="4" fill="{color}"/>')
        p.append(
            f'<text x="20" y="{round(cy + 4, 1)}" font-size="12.5" '
            f'fill="currentColor">{escape(_truncate(e.get("title"), 32))}</text>'
        )
        trail = in_week(e)
        xs = [cx(t) for t in trail]
        p.append(
            f'<line x1="{min(xs)}" y1="{cy}" x2="{max(xs)}" y2="{cy}" '
            f'stroke="{color}" stroke-width="2"/>'
        )
        for t in trail:
            x = cx(t)
            first = t == trail[0]
            p.append(f'<circle cx="{x}" cy="{cy}" r="{4.5 if first else 3.5}" fill="{color}"/>')
            if first and str(e.get("priority")) == "P0":
                p.append(
                    f'<circle cx="{x}" cy="{cy}" r="7.5" fill="none" '
                    f'stroke="{color}" stroke-width="1"/>'
                )
    p.append("</svg>")
    return "".join(p)


def weekly_tagbar_svg(by_tag: dict[str, list[dict[str, Any]]]) -> str:
    """Horizontal stacked bar of the week's attention split by tag. Pure counts."""
    counts = [(t, len(evs)) for t, evs in by_tag.items() if evs]
    total = sum(c for _, c in counts)
    if total == 0:
        return ""
    counts.sort(key=lambda x: -x[1])
    width, bar_h = 720, 26
    x = 0.0
    seg, leg = [], []
    for t, c in counts:
        w = round(c / total * width, 1)
        color = TAG_COLOR.get(t, FALLBACK_COLOR)
        seg.append(f'<rect x="{round(x, 1)}" y="0" width="{w}" height="{bar_h}" fill="{color}"/>')
        if w > 26:
            seg.append(
                f'<text x="{round(x + w / 2, 1)}" y="17" text-anchor="middle" '
                f'font-size="12" fill="#fff">{c}</text>'
            )
        leg.append(f'<span style="color:{color}">●</span> {escape(t)} {c}')
        x += w
    return (
        f'<svg viewBox="0 0 {width} {bar_h}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" class="tagbar"><title>Tags this week</title>{"".join(seg)}</svg>'
        f'<p class="tagbar-legend">{" · ".join(leg)}</p>'
    )


def weekly_ctx(
    week: str,
    dailies_in_week: list[dict[str, Any]],
    weekly_json: dict[str, Any] | None,
) -> dict[str, Any]:
    """Aggregate the week's events by tag, merged across days by ``cluster_id``.

    ``trail`` is EVERY day an event surfaced (expanded or collapsed), so
    the swimlane shows the whole arc: a story that led on Monday and was a
    footnote on Wednesday is still one continuous story. ``ever_expanded``
    marks the ones that earned a full brief at least once; only those make
    the editorial lists, while collapsed-only stories still draw a lane.
    ``line``/Archive events are left out entirely.
    """
    ordered = sorted(dailies_in_week, key=lambda d: str(d.get("date")))
    merged: dict[str, dict[str, Any]] = {}
    timeline: list[dict[str, Any]] = []
    for daily in ordered:
        day = str(daily.get("date"))
        events = [normalize_event(e) for e in (daily.get("events") or [])]
        for e in events:
            if e["display"] == "line" or str(e.get("priority")) == "Archive":
                continue
            cid = str(e.get("cluster_id"))
            is_exp = e["display"] == "expanded"
            if cid not in merged:
                merged[cid] = {
                    **e, "first_seen": day, "trail": [day], "page": day, "ever_expanded": is_exp,
                }
            else:
                merged[cid].update(
                    {k: e[k] for k in ("title", "takeaway", "priority", "tag") if e.get(k)}
                )
                merged[cid]["trail"].append(day)
                if is_exp:
                    merged[cid]["page"] = day  # anchor to the latest full brief
                    merged[cid]["ever_expanded"] = True

        # Per-day timeline: that day's headline events (P0 -> deep read ->
        # top P1), at most three, in chronological order.
        evs = [e for e in events if e["display"] == "expanded"]
        evs.sort(
            key=lambda e: (
                PRIORITY_ORDER.get(str(e.get("priority")), 9),
                0 if e.get("depth") == "deep" else 1,
            )
        )
        if evs:
            timeline.append(
                {
                    "date": day,
                    "weekday": weekday_name(day),
                    "highlights": [
                        {
                            "title": e.get("title"),
                            "priority": e.get("priority"),
                            "deep": e.get("depth") == "deep",
                            "cluster_id": e.get("cluster_id"),
                            "color": TAG_COLOR.get(str(e.get("tag")), FALLBACK_COLOR),
                        }
                        for e in evs[:3]
                    ],
                }
            )

    listed = [e for e in merged.values() if e.get("ever_expanded")]
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for ev in _sorted_events(listed):
        by_tag.setdefault(str(ev.get("tag") or DEFAULT_TAG), []).append(ev)

    # Automatic main threads, shown until the weekly run writes narrative
    # ones: the stories that ran longest this week, top six.
    auto_threads = sorted(
        (e for e in listed if len(e["trail"]) >= 2),
        key=lambda e: (-len(e["trail"]), PRIORITY_ORDER.get(str(e.get("priority")), 9)),
    )[:6]

    return {
        "week": week,
        "dates": [str(d.get("date")) for d in ordered],
        "threads": (weekly_json or {}).get("weekly_threads"),
        "auto_threads": auto_threads,
        "timeline": timeline,
        "by_tag": by_tag,
        "event_count": len(listed),
        "total_min": sum(int(e.get("read_budget_min") or 0) for e in listed),
        "swimlane_svg": Markup(weekly_swimlane_svg(list(merged.values()), week)),
        "tagbar_svg": Markup(weekly_tagbar_svg(by_tag)),
        "insights": (weekly_json or {}).get("insights") or [],
    }


# ---- main ----------------------------------------------------------


def _index_entries(dailies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    for d in dailies:
        ctx = daily_ctx(d)
        top = next((e for e in ctx["expanded"] if e.get("priority") == "P0"), None)
        top = top or (ctx["expanded"][0] if ctx["expanded"] else {})
        entries.append(
            {
                "date": ctx["date"], "weekday": ctx["weekday"], "week": ctx["week"],
                "stats": ctx["stats"], "top_title": top.get("title", ""), "gap": False,
            }
        )
    # Any date between the earliest and latest daily with no report gets a
    # grey placeholder row, so a missed day is visible instead of silent.
    have = {e["date"] for e in entries}
    cur, hi = date_cls.fromisoformat(min(have)), date_cls.fromisoformat(max(have))
    while cur <= hi:
        ds = cur.isoformat()
        if ds not in have:
            entries.append(
                {
                    "date": ds, "weekday": weekday_name(ds), "week": week_key(ds),
                    "stats": {}, "top_title": "", "gap": True,
                }
            )
        cur += timedelta(days=1)
    return entries


def render_site(digests_dir: Path) -> int:
    json_dir = digests_dir / "json"
    site = digests_dir / "site"
    if not json_dir.is_dir():
        print(f"error: {json_dir} not found", file=sys.stderr)
        return 1
    dailies, weeklies = load_digests(json_dir)
    if not dailies:
        print(f"error: no daily report json under {json_dir}", file=sys.stderr)
        return 1

    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md"] = md_block
    env.filters["mdi"] = md_inline
    env.filters["scan"] = scan_line

    for sub in ("daily", "weekly", "assets"):
        (site / sub).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATES_DIR / "style.css", site / "assets" / "style.css")

    daily_tpl = env.get_template("daily.html.j2")
    weeks: dict[str, list[dict[str, Any]]] = {}
    for d in dailies:
        ctx = daily_ctx(d)
        out = site / "daily" / f"{ctx['date']}.html"
        out.write_text(daily_tpl.render(root="../", **ctx), encoding="utf-8")
        weeks.setdefault(ctx["week"], []).append(d)

    # A week that has only a weekly review and no dailies still gets a page.
    weekly_tpl = env.get_template("weekly.html.j2")
    all_weeks = sorted(set(weeks) | set(weeklies))
    for week in all_weeks:
        ctx = weekly_ctx(week, weeks.get(week, []), weeklies.get(week))
        out = site / "weekly" / f"{week}.html"
        out.write_text(weekly_tpl.render(root="../", **ctx), encoding="utf-8")

    (site / "index.html").write_text(
        env.get_template("index.html.j2").render(
            root="", entries=_index_entries(dailies), weekly_keys=set(weeklies)
        ),
        encoding="utf-8",
    )
    print(f"rendered: dailies={len(dailies)} weeklies={len(all_weeks)} -> {site}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render report JSON into a static HTML site")
    parser.add_argument(
        "--digests-dir",
        type=Path,
        default=None,
        help="reports root holding json/ (default: $CURATOR_HOME/digests, i.e. ~/.curator/digests)",
    )
    args = parser.parse_args(argv)
    digests_dir = args.digests_dir or default_home() / "digests"
    return render_site(digests_dir.expanduser())


if __name__ == "__main__":
    sys.exit(main())
