import pymupdf
import pytest

from ingest.convert import (
    ConvertError,
    clean_markdown,
    html_to_markdown,
    labelled_rows_to_markdown,
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
    assert "\n### Can I enter student data?\n" in md  # a heading below FAQ, not a list item
    assert md.index("Can I enter student data?") < md.index("Only if they consented")
    assert "Search" not in md


def test_a_panels_headings_stay_inside_its_question() -> None:
    # A fixed level put the AI Assessment Modes under the framework before them.
    page = b"""<main><h3>Levels of AI assistance</h3><ul>
    <li><button aria-controls="a1"><div class="accordion-item-heading"><p>No assistance</p></div>
      </button><div id="a1"><p>Complete it without AI.</p></div></li>
    <li><button aria-controls="a2"><div class="accordion-item-heading"><p>Framework for
      Hexamester 6 2026</p></div><div class="accordion-item-subheading"><p>In this transitional
      period, the framework below applies.</p></div></button>
      <div id="a2"><h4>AI Assessment Modes</h4><p>UNSW uses three modes.</p>
      <h5>The three modes are:</h5><p>Can't use AI; can use AI; must use AI.</p></div></li>
    </ul><h3>Frequently asked questions</h3></main>"""
    md = clean_markdown(html_to_markdown(page))
    assert [line for line in md.splitlines() if line.startswith("#")] == [
        "### Levels of AI assistance",
        "#### No assistance",
        "#### Framework for Hexamester 6 2026",
        "##### AI Assessment Modes",
        "###### The three modes are:",
        "### Frequently asked questions",
    ]
    assert "In this transitional period, the framework below applies." in md


def test_each_tabs_label_heads_its_panel() -> None:
    # Unlabelled, the Chicago format read as APA's (the label line ended the passage before).
    page = b"""<main><h3>Examples of AI referencing</h3><div class="cmp-tabs">
    <ul role="tablist"><li role="tab"><span>Harvard (UNSW)</span></li>
      <li role="tab"><button class="accordion-item">Harvard (UNSW)</button></li>
      <li role="tab"><span>APA (7th edition)</span></li>
      <li role="tab"><button class="accordion-item">APA (7th edition)</button></li></ul>
    <div role="tabpanel"><p>Company. Year, Product Name.</p></div>
    <div role="tabpanel"><p>Company. (Year). Tool (Month Date version) [Large language model].</p>
      </div></div></main>"""
    md = clean_markdown(html_to_markdown(page))
    assert md.split("\n\n") == [
        "### Examples of AI referencing",
        "#### Harvard (UNSW)",
        "Company. Year, Product Name.",
        "#### APA (7th edition)",
        "Company. (Year). Tool (Month Date version) [Large language model].\n",
    ]


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
        (
            "|**Copying**|Using the same text<br>without acknowledging it.|",
            "|**Copying**|Using the same text without acknowledging it.|",
        ),
    ],
)
def test_clean_markdown(raw: str, expected: str) -> None:
    assert clean_markdown(raw).strip() == expected


def test_link_only_lines_are_dropped() -> None:
    menu = "#### [Privacy guidance](https://x)\n\n* [Organisations](https://x/o)\n  + [Credit](https://x/c)"
    md = clean_markdown(menu + "\n\nReal text.")
    assert md.strip() == "Real text."


def test_a_list_after_a_sentence_keeps_its_links() -> None:
    # The AI Assurance Framework's "Critical policies to consider include:" lost its list.
    md = (
        "Critical policies to consider include:\n\n* [Cyber Security](https://x/c)\n"
        "* [Privacy Policy](https://x/p)\n\nThere are standards for better practice.\n\n"
        "* [Data Classification Standard](https://x/d)\n\n#### See also\n\n"
        "* [Related page](https://x/r)\n\n[AI in Teaching](/ai)\n\n* [Another page](https://x/a)"
    )
    assert clean_markdown(md).strip().split("\n\n") == [
        "Critical policies to consider include:",
        "* Cyber Security\n* Privacy Policy",
        "There are standards for better practice.",
        "* Data Classification Standard",
        "#### See also",
        "AI in Teaching",
    ]


def test_an_empty_heading_is_dropped() -> None:
    # It started a section with no name, taking the text below from its heading.
    md = "## AI capability framework for teaching staff\n\n##\n\nThis framework provides guidance."
    assert clean_markdown(md).strip().split("\n\n") == [
        "## AI capability framework for teaching staff",
        "This framework provides guidance.",
    ]


def test_unicode_spaces_become_spaces() -> None:
    # LangChain's header splitter deletes them, which joined words in the index.
    md = clean_markdown("identified in the\xa0Cyber Security\u202fPolicy.")
    assert md.strip() == "identified in the Cyber Security Policy."


def test_formatting_that_holds_only_a_space_keeps_it() -> None:
    page = b"<main><p>The <em>Framework </em>aims<i> </i>to<i> </i>establish it.</p></main>"
    assert "aims to establish it." in html_to_markdown(page)


def test_a_cards_subheadings_sit_under_its_title() -> None:
    page = b"""<main><h3>What factors should you consider?</h3>
    <div class="grid-accordion-item"><h4>Fairness</h4><p>Manage data bias.</p></div>
    <div class="grid-accordion-item"><h4>Transparency</h4><p>Explain the outputs.</p>
      <h4><b>Assessing AI-generated insights</b></h4><p>Document the harms.</p></div></main>"""
    md = clean_markdown(html_to_markdown(page))
    assert [line for line in md.splitlines() if line.startswith("#")] == [
        "### What factors should you consider?",
        "#### Fairness",
        "#### Transparency",
        "##### Assessing AI-generated insights",
    ]


def test_a_link_over_two_lines_becomes_text() -> None:
    md = clean_markdown("Submit your details through [the support\nportal.](https://x/sp)")
    assert md.strip() == "Submit your details through the support\nportal."


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


def make_labelled_rows_pdf() -> bytes:
    """Two pages of a two-column table: bold labels left, text right, as UNSW's legal fact
    sheet is laid out, with its header row repeated and a running footer."""
    doc = pymupdf.open()
    rows = [
        (
            ["Contravention of", "privacy laws"],
            [
                "Receipt of personal information will be",
                "subject to obligations on how such",
                "information can be used.",
            ],
        ),
        (["Confidentiality"], ["A breach of an obligation of", "confidence may follow."]),
    ]
    for number in (1, 2):
        page = doc.new_page()
        page.insert_text(
            (47, 60), "Risks associated with content input", fontsize=11, fontname="hebo"
        )
        page.insert_text((52, 87), "Legal issue", fontsize=10, fontname="hebo")
        page.insert_text((159, 87), "Risks", fontsize=10, fontname="hebo")
        y = 111.0
        for label, text in rows:
            for i, line in enumerate(label):
                page.insert_text((52, y + 11.5 * i), line, fontsize=10, fontname="hebo")
            for i, line in enumerate(text):
                page.insert_text(
                    (159, y + 11.5 * i), f"{line} (page {number})" if i == 0 else line, fontsize=10
                )
            y += 11.5 * max(len(label), len(text)) + 30
        page.insert_text((524, 786), f"Page {number}", fontsize=8)
    data: bytes = doc.tobytes()
    return data


def test_a_labelled_rows_pdf_keeps_each_label_over_its_own_text() -> None:
    md = clean_markdown(labelled_rows_to_markdown(make_labelled_rows_pdf(), 140))
    assert [line for line in md.splitlines() if line.startswith("#")] == [
        "## Risks associated with content input",
        "### Contravention of privacy laws",
        "### Confidentiality",
    ] * 2  # the repeated "Legal issue | Risks" header row is gone
    assert (
        "Receipt of personal information will be (page 1) subject to obligations on how such "
        "information can be used." in md
    )  # not spliced with "privacy laws"
    assert "Page 1" not in md
