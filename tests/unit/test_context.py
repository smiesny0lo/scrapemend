from pathlib import Path

import yaml

from scrapemend.heal.anchors import Anchor
from scrapemend.heal.context import MAX_TEXT, examples, prune_html, spec_yaml
from scrapemend.spec import SourceSpec, load_spec

DEMO = load_spec(Path(__file__).parents[2] / "examples" / "sources" / "demo.yaml")


def test_scripts_styles_and_comments_go() -> None:
    html = (
        "<head><title>T</title><style>p{}</style></head><body><script>track()</script>"
        "<!-- hi --><p class='a'>kept</p>tail<svg><path/></svg></body>"
    )
    out = prune_html(html)
    assert out is not None
    assert "track" not in out and "p{}" not in out and "hi" not in out and "svg" not in out
    assert '<p class="a">kept</p>' in out and "tail" in out


def test_only_selector_relevant_attributes_stay() -> None:
    out = prune_html(
        "<a class='next' href='/p?page=2' rel='next' onclick='x()' style='color:red'"
        " data-id='7' aria-label='Next page'>Next</a>"
    )
    assert out is not None
    for kept in (
        'class="next"',
        'href="/p?page=2"',
        'rel="next"',
        'data-id="7"',
        'aria-label="Next page"',
    ):
        assert kept in out
    assert "onclick" not in out and "style" not in out


def test_long_text_is_shortened() -> None:
    out = prune_html(f"<p>{'word ' * 100}</p>")
    assert out is not None and "..." in out
    assert len(out) < MAX_TEXT + 40


def test_listing_items_are_collapsed() -> None:
    rows = "".join(f"<tr class='row'><td>{i}</td></tr>" for i in range(20))
    listing = prune_html(f"<table>{rows}</table>", listing=True)
    assert listing is not None
    assert listing.count("<tr class") == 3 and "17 more similar <tr> elements" in listing


def test_detail_rows_are_never_collapsed() -> None:
    # On a detail page similar rows are different fields: all of them must stay visible.
    rows = "".join(f"<div class='meta-row'><dt>Label {i}</dt><dd>{i}</dd></div>" for i in range(8))
    detail = prune_html(f"<dl>{rows}</dl>")
    assert detail is not None and detail.count("meta-row") == 8


def test_mixed_siblings_are_kept() -> None:
    out = prune_html("<div>" + "<p>a</p><h2>b</h2>" * 4 + "</div>", listing=True)
    assert out is not None and out.count("<p>") == 4


def test_pages_are_capped() -> None:
    out = prune_html(
        "<div>" + "".join(f"<p class='x{i}'>{i}</p>" for i in range(5000)) + "</div>",
        max_chars=1000,
    )
    assert out is not None and "cut:" in out and len(out) < 1100


def test_empty_page() -> None:
    assert prune_html("") is None


def test_spec_yaml_round_trips() -> None:
    assert SourceSpec.model_validate(yaml.safe_load(spec_yaml(DEMO))) == DEMO


def test_examples_show_values_and_their_text() -> None:
    anchor = Anchor(
        key="k",
        url="https://x.org/1",
        values={"deadline": "2026-09-27"},
        raw={"deadline": "27 Sep 2026"},
        strategies={"deadline": 0},
        snapshot_id=None,
        run_id="r",
    )
    [example] = examples([anchor])
    assert (example.values, example.shown_as) == (
        {"deadline": "2026-09-27"},
        {"deadline": "27 Sep 2026"},
    )
