import pymupdf
import pytest

from ingest.convert import (
    ConvertError,
    clean_markdown,
    html_to_markdown,
    main_content,
    pdf_to_markdown,
    to_markdown,
)
from ingest.fetch import Fetched
from ingest.sources import Source

PAGE = b"""<html><body>
<header><h2>Follow</h2><a href="/">Home</a></header>
<div class="cookie-banner"><h2>We use cookies on this site</h2></div>
<div class="cmp-filtered-cards__filter-heading">Personalise</div>
<main>
  <div class="breadcrumb"><a href="/privacy">Privacy</a></div>
  <nav><ul><li><a href="/a">Credit reporting</a></li></ul></nav>
  <h1>Guidance on <strong>AI</strong> products</h1>
  <p>Organisations should consider privacy obligations before using
     <a href="/ai">commercially available AI products</a>.</p>
  <h2><img src="icon.png" alt=""> Overview</h2>
  <p>This guidance explains how the Privacy Act applies.</p>
  <script>track()</script>
</main>
<footer><h2>Acknowledgement of Country</h2></footer>
</body></html>"""


def test_main_content_drops_page_furniture() -> None:
    html = main_content(PAGE)
    for gone in [
        "Follow",
        "cookies",
        "breadcrumb",
        "Credit reporting",
        "track()",
        "Acknowledgement",
        "Personalise",
    ]:
        assert gone not in html
    assert "Guidance on" in html


def test_cloudflare_email_links_are_stable() -> None:
    page = b"""<main><p>Email <a href="/cdn-cgi/l/email-protection#%s">
    <span class="__cf_email__">[email&#160;protected]</span></a> for help.</p></main>"""
    first, second = (main_content(page % key) for key in (b"8eebe0", b"751011"))
    assert first == second
    assert "[email address]" in first


def test_accordion_questions_become_headings() -> None:
    page = b"""<main><h2>FAQ</h2><ul>
    <li><button aria-controls="a1"><div><p>Can I enter student data?</p></div>
        <span class="accordion-item-button"></span></button>
        <div id="a1"><p>Only if they consented or it is de-identified.</p></div></li>
    <li><button type="submit">Search</button></li></ul></main>"""
    md = clean_markdown(html_to_markdown(page))
    assert "\n###### Can I enter student data?\n" in md  # a heading, not a list item
    assert md.index("Can I enter student data?") < md.index("Only if they consented")
    assert "Search" not in md


def test_html_to_markdown_keeps_headings_and_text() -> None:
    md = clean_markdown(html_to_markdown(PAGE))
    assert "# Guidance on AI products" in md
    assert "## Overview" in md
    assert "commercially available AI products." in md
    assert "](" not in md  # links become plain text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "## **Statement from Data** **and Digital Ministers**",
            "## Statement from Data and Digital Ministers",
        ),
        ("### **1. Human-centred values**", "### 1. Human-centred values"),
        (
            "## Implementing _Australia's AI Ethics Principles_ in government",
            "## Implementing Australia's AI Ethics Principles in government",
        ),
        ("## ![](https://x/icon.png) Overview", "## Overview"),
        ("See [the policy](https://x/p.pdf) for details.", "See the policy for details."),
        ("### Statement 6: <u>Manage system bias</u>", "### Statement 6: Manage system bias"),
        ("<mark>Cultural protocols are central.</mark>", "Cultural protocols are central."),
        ("<!-- Start of picture text -->Figure 2<!-- End of picture text -->", "Figure 2"),
    ],
)
def test_clean_markdown(raw: str, expected: str) -> None:
    assert clean_markdown(raw).strip() == expected


def test_link_only_lines_are_dropped() -> None:
    menu = "#### [Privacy guidance](https://x)\n\n* [Organisations](https://x/o)\n  + [Credit](https://x/c)"
    md = clean_markdown(menu + "\n\nReal text.")
    assert md.strip() == "Real text."


def test_bold_clause_lines_become_headings_one_level_down() -> None:
    md = "### Principles\n\n  **3.2.\xa0We act with integrity**\n\n  3.2.1. We cite sources.\n"
    assert clean_markdown(md).splitlines()[2] == "#### 3.2. We act with integrity"


def test_bold_text_that_is_not_a_clause_stays_text() -> None:
    for line in ["**1. Responsibilities**", "**Effective:** 1 February 2025", "**Criterion 59**"]:
        assert clean_markdown(line).strip() == line


def test_snake_case_survives_heading_cleanup() -> None:
    assert clean_markdown("## The content_hash field").strip() == "## The content_hash field"


def make_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Assurance framework", fontsize=24)
    y = 110
    for i in range(12):
        page.insert_text(
            (72, y), f"Agencies must assess risk {i} before deploying an AI system.", fontsize=11
        )
        y += 16
    data: bytes = doc.tobytes()
    return data


def test_pdf_to_markdown_extracts_text() -> None:
    md = pdf_to_markdown(make_pdf())
    assert "Assurance framework" in md
    assert "assess risk 11" in md


def test_too_little_text_is_an_error(source: Source) -> None:
    doc = Fetched(source, "html", b"<main><p>Page not found</p></main>", source.url)
    with pytest.raises(ConvertError, match="words"):
        to_markdown(doc)
