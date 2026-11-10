"""Small copy guardrails for the local CoverageFlow interface."""

from pathlib import Path


STATIC = Path(__file__).parents[1] / "ui" / "static"


def test_user_interface_contains_no_em_dash() -> None:
    """The desk voice uses full stops, colons, and commas instead."""
    for name in ("index.html", "app.js", "style.css"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert "\u2014" not in text, f"em dash found in ui/static/{name}"


def test_shell_copy_starts_with_capitals() -> None:
    """Pin the always-visible shell labels that frame every route."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    expected = (
        "Research operations",
        "Today",
        "Start coverage",
        "Source bundles",
        "Rehearsal",
        "Run replays",
        "Activity log",
        "Model routing",
        "Find a ticker or case…",
        "Opening the desk…",
    )
    for phrase in expected:
        assert phrase in html
        assert phrase[0].isupper()
