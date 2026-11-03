"""Every colour pairing the pages use meets WCAG 2.2 AA, in each palette and theme.

Read from app/templates/_base.css itself, so a later change to a colour can't drop a
pairing below AA unnoticed. Text needs 4.5:1; control edges and the focus ring, which
aren't text, need 3:1 (WCAG 1.4.11).
"""

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parents[1] / "app" / "templates" / "_base.css").read_text()

# Each palette's light and dark rules, by selector. The dark values appear twice (for
# the device's setting and for an explicit choice); both must match.
PALETTES = {
    "standard": (":root", ':root:not([data-theme="light"])', ':root[data-theme="dark"]'),
    "reading": (
        ':root[data-palette="reading"]',
        ':root[data-palette="reading"]:not([data-theme="light"])',
        ':root[data-palette="reading"][data-theme="dark"]',
    ),
}
# (foreground, background, minimum ratio, what it is)
PAIRS = [
    ("text", "bg", 4.5, "body text"),
    ("text", "surface", 4.5, "the answer and cards"),
    ("text", "notice-bg", 4.5, "notices"),
    ("muted", "bg", 4.5, "hints and captions"),
    ("muted", "surface", 4.5, "source details"),
    ("muted", "notice-bg", 4.5, "muted text on a notice"),
    ("accent", "bg", 4.5, "links"),
    ("accent", "surface", 4.5, "links and citations in an answer"),
    ("accent", "notice-bg", 4.5, "links on a notice"),
    ("accent-text", "accent", 4.5, "button text, the cited mark"),
    ("ok", "bg", 4.5, "'passed'"),
    ("ok", "surface", 4.5, "'passed' in a table"),
    ("bad", "bg", 4.5, "'failed'"),
    ("bad", "surface", 4.5, "'failed' in a table"),
    ("control", "bg", 3.0, "the question box's and chips' edges"),
    ("control", "surface", 3.0, "a control's edge on a card"),
    ("focus", "bg", 3.0, "the focus ring"),
    ("focus", "surface", 3.0, "the focus ring on a card"),
]


def rules(css: str) -> dict[str, dict[str, str]]:
    """Each rule's selector, including rules inside @media, and its colour properties."""
    found: dict[str, dict[str, str]] = {}
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        colours = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", body))
        if colours:
            found[" ".join(selector.split())] = colours
    return found


def luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a: str, b: str) -> float:
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


RULES = rules(CSS)
CASES = [
    (f"{name} {theme}", selector)
    for name, (light, dark, chosen_dark) in PALETTES.items()
    for theme, selector in (("light", light), ("dark", dark))
]


@pytest.mark.parametrize(("palette", "selector"), CASES)
def test_every_pairing_meets_aa(palette: str, selector: str) -> None:
    colours = RULES[selector]
    failing = [
        f"{what}: {fg} on {bg} is {ratio(colours[fg], colours[bg]):.2f}, needs {minimum}"
        for fg, bg, minimum, what in PAIRS
        if ratio(colours[fg], colours[bg]) < minimum
    ]
    assert not failing, f"{palette}: {failing}"


@pytest.mark.parametrize("name", PALETTES)
def test_each_palette_sets_every_colour_and_both_darks_match(name: str) -> None:
    light, dark, chosen_dark = PALETTES[name]
    tokens = set(RULES[":root"])
    assert set(RULES[light]) == tokens
    assert RULES[dark] == RULES[chosen_dark]
    assert set(RULES[dark]) == tokens


def test_the_ratio_matches_wcags_examples() -> None:
    assert ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert ratio("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)
