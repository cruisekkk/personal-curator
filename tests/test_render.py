import json
import shutil
from pathlib import Path

from curator import render

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "digests"


def _render_examples(tmp_path: Path) -> Path:
    shutil.copytree(EXAMPLES / "json", tmp_path / "json")
    assert render.main(["--digests-dir", str(tmp_path)]) == 0
    return tmp_path / "site"


def test_renders_bundled_examples(tmp_path):
    site = _render_examples(tmp_path)

    daily = (site / "daily" / "2026-03-03.html").read_text(encoding="utf-8")
    for label in ("At a glance", "Briefs", "Doubts to keep", "Sources:", "Links only",
                  "Est. reading time", "97 raw items → 6 events", "Deep read", "Since last time"):
        assert label in daily, label
    assert "<table>" in daily  # the deep read's markdown table
    assert "2026-03-03 Tue" in daily
    assert 'href="../weekly/2026-W10.html">This week</a>' in daily

    weekly = (site / "weekly" / "2026-W10.html").read_text(encoding="utf-8")
    assert '<svg viewBox="0 0 720' in weekly and 'class="swimlane"' in weekly
    assert 'class="tagbar"' in weekly
    for label in ("Timeline", "Main threads", "From pass-through to price war"):
        assert label in weekly, label
    # Three stories span more than one day -> three lanes (one tag dot each).
    assert weekly.count('<circle cx="9"') == 3

    index = (site / "index.html").read_text(encoding="utf-8")
    assert "2026-W10 weekly page" in index
    assert 'href="daily/2026-03-04.html"' in index
    assert (site / "assets" / "style.css").is_file()


def test_examples_stats_are_consistent():
    for fp in sorted((EXAMPLES / "json").glob("2026-03-*.json")):
        d = json.loads(fp.read_text(encoding="utf-8"))
        events = d["events"]
        assert d["stats"]["events"] == len(events)
        expanded = [e for e in events if e["display"] == "expanded"]
        assert d["stats"]["est_read_min"] == sum(e["read_budget_min"] for e in expanded) + 4
        assert {e["display"] for e in events} == {"expanded", "collapsed", "line"}
        for e in events:
            assert all(s["url"].startswith("https://example.com/") for s in e["sources"])


def test_unknown_display_is_normalised_from_priority(capsys):
    def ev(priority, display):
        return {"cluster_id": f"evt-{priority}", "priority": priority, "display": display, "title": "t"}

    assert render.normalize_event(ev("Archive", "L0"))["display"] == "line"
    assert render.normalize_event(ev("P0", None))["display"] == "expanded"
    assert render.normalize_event(ev("P1", "folded"))["display"] == "collapsed"
    assert "display='folded'" in capsys.readouterr().err

    # Valid values pass through untouched, and the input dict is never mutated.
    original = ev("P2", "expanded")
    assert render.normalize_event(original) is original

    ctx = render.daily_ctx({"date": "2026-03-02", "events": [ev("P0", "bogus"), ev("Archive", "")]})
    assert [e["cluster_id"] for e in ctx["expanded"]] == ["evt-P0"]
    assert [e["cluster_id"] for e in ctx["lines"]] == ["evt-Archive"]


def test_scan_line_takes_first_sentence_and_caps_length():
    assert render.scan_line("Margins rose 3.5% on mix. Then a lot more detail.") == "Margins rose 3.5% on mix."
    assert render.scan_line("**Bold** and [a link](https://example.com) stay text") == "Bold and a link stay text"
    long = "word " * 80
    assert len(render.scan_line(long)) == 140
