"""Convert a fetched document to Markdown whose headings give each passage its section.

PDFs go through PyMuPDF4LLM in layout mode, which nests headings and drops running
headers and footers. Web pages go through MarkItDown, using only the page's main
content so menus, cookie banners and footers don't become passages.
See docs/decisions.md, "Document conversion".
"""

import io
import re
from collections import Counter
from dataclasses import dataclass

import pymupdf
import pymupdf4llm
from bs4 import BeautifulSoup, Tag
from markitdown import MarkItDown, StreamInfo

from ingest.fetch import Fetched

MAIN_SELECTORS = ["main", "[role=main]", "article", "#main-content", "body"]
NOISE_TAGS = [
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "button",
    "svg",
    "img",
    "iframe",
]
# Breadcrumbs, share buttons and similar page furniture that sits inside the main content.
NOISE_CLASSES = re.compile(r"breadcrumb|social|share|skip-link|cookie|cards__filter", re.I)
INLINE_TAGS = ["i", "em", "b", "strong", "u", "span"]
HEADINGS = re.compile(r"^h[1-6]$")
# UNSW's cards: each is a title heading with its own content, which may have subheadings.
CARD_SELECTOR = ".grid-accordion-item"
# UNSW's tabs: a list of labels, then the panels in the same order.
TABS_SELECTOR = ".cmp-tabs"

_markitdown = MarkItDown()


class ConvertError(Exception):
    pass


def to_markdown(doc: Fetched) -> str:
    if doc.kind != "pdf":
        markdown = html_to_markdown(doc.content)
    elif doc.source.label_column is not None:
        markdown = labelled_rows_to_markdown(doc.content, doc.source.label_column)
    else:
        markdown = pdf_to_markdown(doc.content)
    markdown = clean_markdown(markdown)
    if len(markdown.split()) < 50:
        raise ConvertError(f"Only {len(markdown.split())} words after conversion")
    return markdown


def pdf_to_markdown(content: bytes) -> str:
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        text: str = pymupdf4llm.to_markdown(pdf, header=False, footer=False, use_ocr=False)
    return text


@dataclass(frozen=True)
class _Line:
    y: float
    x: float
    size: float
    bold: bool
    text: str


def _pdf_lines(page: pymupdf.Page, smallest: float) -> list[_Line]:
    """The page's lines, without running footers (smaller type) or the copyright line."""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            text = " ".join("".join(s["text"] for s in spans).split())
            size = max((s["size"] for s in spans), default=0)
            if not text or size < smallest or text.startswith("©"):
                continue
            bold = all(s["flags"] & pymupdf.TEXT_FONT_BOLD for s in spans)
            lines.append(_Line(line["bbox"][1], line["bbox"][0], size, bold, text))
    return sorted(lines, key=lambda line: (round(line.y), line.x))


def _group(lines: list[_Line], gap: float) -> list[list[_Line]]:
    """Lines into runs, a new run wherever the space above a line is wider than `gap`
    times its type size."""
    runs: list[list[_Line]] = []
    for line in lines:
        if runs and line.y - runs[-1][-1].y <= gap * line.size:
            runs[-1].append(line)
        else:
            runs.append([line])
    return runs


def labelled_rows_to_markdown(content: bytes, label_column: float) -> str:
    """A PDF laid out as a two-column table: bold labels on the left, their text right.

    PyMuPDF4LLM's layout mode reads such a page line by line across both columns,
    splicing a label into the text beside it ("on how such privacy laws information can
    be used"), and its standard mode reads the whole label column first. Here each label
    is a heading over the text beside and below it. The type sizes give the rest: the
    document's title, each page's title, and the body (the commonest size).
    """
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        sizes = Counter(
            round(span["size"])
            for page in pdf
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            if span["text"].strip()
        )
        body = sizes.most_common(1)[0][0]
        pages = [_pdf_lines(page, body - 1.5) for page in pdf]
    # (y, Markdown) for each piece of each page, in reading order.
    pieces: list[list[tuple[float, str]]] = []
    header_rows: Counter[tuple[str, str]] = Counter()
    for lines in pages:
        title = [line for line in lines if line.size >= body * 1.8]
        page_title = [
            line
            for line in lines
            if body + 0.5 < line.size < body * 1.8 and line.bold and line.x < label_column
        ]
        labels = [
            line
            for line in lines
            if line.bold and line.x < label_column and line.size <= body + 0.5
        ]
        # Bold type larger than the body, in the text column, labels a figure.
        figure = [line for line in lines if line.bold and line.size > body + 0.5]
        rest = [line for line in lines if line not in {*title, *page_title, *labels, *figure}]
        page: list[tuple[float, str]] = []
        if title:
            page.append((title[0].y, "# " + " ".join(line.text for line in title)))
        for run in _group(page_title, 1.6):
            page.append((run[0].y, "## " + " ".join(line.text for line in run)))
        for run in _group(labels, 1.3):
            page.append((run[0].y, "### " + " ".join(line.text for line in run)))
        # Lines level with each other are one line. A bullet is a glyph of its own, level
        # with its text but sometimes a point lower.
        text: list[_Line] = []
        for line in rest:
            last = text[-1] if text else None
            if last is None or abs(line.y - last.y) >= 3:
                text.append(line)
            elif last.text == "•":
                text[-1] = _Line(last.y, last.x, line.size, line.bold, "- " + line.text)
            elif line.text == "•":
                text[-1] = _Line(last.y, line.x, last.size, last.bold, "- " + last.text)
            else:
                joined = f"{last.text} {line.text}"
                text[-1] = _Line(last.y, last.x, last.size, last.bold and line.bold, joined)
        # A paragraph ends where a label or title starts beside it.
        starts = [y for y, _ in page]
        for run in _group(text, 1.45):
            paragraphs: list[list[_Line]] = []
            for line in run:  # each bullet starts a paragraph of its own
                beside_start = bool(paragraphs) and any(
                    paragraphs[-1][-1].y + 2 < y <= line.y + 2 for y in starts
                )
                if not paragraphs or line.text.startswith("- ") or beside_start:
                    paragraphs.append([line])
                else:
                    paragraphs[-1].append(line)
            for para in paragraphs:
                # A web address set straight after its title, with no space.
                joined = " ".join(line.text for line in para)
                words = re.sub(r"(?<=\w)(?=https?://)", " ", joined)
                if (
                    all(line.bold for line in para)
                    and len(words.split()) <= 12
                    and words[-1] not in ".:,;"
                ):
                    page.append((para[0].y, "#### " + words))
                else:
                    page.append((para[0].y, words))
        # A table's header row, repeated on each page it continues on.
        for y, md in page:
            if md.startswith("### "):
                beside = [m for yy, m in page if m.startswith("#### ") and abs(yy - y) < 3]
                if beside:
                    header_rows[(md, beside[0])] += 1
        pieces.append(sorted(page, key=lambda piece: piece[0]))
    repeated = {row for row, n in header_rows.items() if n > 1}
    out = []
    for page in pieces:
        drop = {m for row in repeated for m in row if any(md == row[0] for _, md in page)}
        # A page that neither has a title nor repeats the header row doesn't continue the
        # table before it (the last page's document control and contacts): its labels go
        # up a level, out of the last page title's section.
        titled = any(md.startswith(("# ", "## ")) for _, md in page)
        for _, md in page:
            if md in drop:
                continue
            out.append(md[1:] if not (drop or titled) and md.startswith("###") else md)
    return "\n\n".join(out) + "\n"


CF_EMAIL = "/cdn-cgi/l/email-protection"


def _level(tag: Tag) -> int:
    return int(tag.name[1])


def _heading_or_button(tag: Tag) -> bool:
    return tag.name == "button" or bool(HEADINGS.match(tag.name))


def nest_accordions(soup: BeautifulSoup, main: Tag) -> None:
    """Turns each accordion's button into a heading, with its panel's headings below it.

    The button holds the question its panel answers (FAQs), so each answer is chunked
    under its question. The question goes one level below the heading it sits under, and
    the panel's own headings below the question, keeping their order: a fixed level put
    the AI Assessment Modes, inside the "Hexamester 6" question, under the framework
    before it.
    """
    question_level: dict[str, int] = {}  # each panel's ID: its question's level
    panel_top: dict[str, int] = {}  # each panel's ID: its highest heading level
    outside = 1  # the level of the last heading outside any panel
    for tag in main.find_all(_heading_or_button):
        panel = next(
            (pid for p in tag.find_parents(id=True) if (pid := str(p["id"])) in question_level),
            None,
        )
        if tag.name != "button":
            if panel is None:
                outside = _level(tag)
            else:
                shift = question_level[panel] + 1 - panel_top[panel]
                tag.name = f"h{min(_level(tag) + shift, 6)}"
            continue
        controls = str(tag.get("aria-controls") or "")
        title = tag.select_one(".accordion-item-heading") or tag
        if not controls or not (text := " ".join(title.get_text(" ").split())):
            continue
        level = min((question_level[panel] if panel else outside) + 1, 6)
        heading = soup.new_tag(f"h{level}")
        heading.string = text
        # Some questions have a line under them; it's the answer's first sentence.
        if (note := tag.select_one(".accordion-item-subheading")) and note.get_text(strip=True):
            line = soup.new_tag("p")
            line.string = " ".join(note.get_text(" ").split())
            tag.replace_with(heading, line)
        else:
            tag.replace_with(heading)
        question_level[controls] = level
        target = soup.find(id=controls)
        levels = [_level(h) for h in target.find_all(HEADINGS)] if target else []
        panel_top[controls] = min(levels, default=level + 1)


def label_tabs(soup: BeautifulSoup, main: Tag) -> None:
    """Puts each tab's label over its panel as a heading, and drops the list of tabs.

    UNSW's tabs list their labels, then their panels in the same order, with nothing
    else linking the two. Without the labels, the AI referencing page's four tables of
    formats lost their styles: each table's "See all … referencing guides" line ended a
    passage and began the next, so the Chicago format read as APA's. A panel that
    starts with a heading of its own keeps it instead.
    """
    in_panels = {str(b.get("aria-controls")) for b in main.select("button[aria-controls]")}
    for tabs in main.select(TABS_SELECTOR):
        labels = list(
            dict.fromkeys(" ".join(tab.get_text(" ").split()) for tab in tabs.select("[role=tab]"))
        )
        panels = tabs.select("[role=tabpanel]")
        if not labels or len(labels) != len(panels):
            continue
        # The heading the tabs sit under, not one inside an accordion's panel.
        above = next(
            (
                h
                for h in tabs.find_all_previous(HEADINGS)
                if not any(str(p["id"]) in in_panels for p in h.find_parents(id=True))
            ),
            None,
        )
        level = min(_level(above) + 1, 6) if above else 2
        for label, panel in zip(labels, panels, strict=True):
            if not panel.find(HEADINGS):
                heading = soup.new_tag(f"h{level}")
                heading.string = label
                panel.insert(0, heading)
        for tablist in tabs.select("[role=tablist]"):
            tablist.decompose()


def main_content(html: bytes) -> str:
    """The page's main content as HTML, without navigation, scripts or images."""
    soup = BeautifulSoup(html, "html.parser")
    main = next((m for sel in MAIN_SELECTORS if (m := soup.select_one(sel))), soup)
    # Before buttons go with the other page furniture.
    label_tabs(soup, main)
    nest_accordions(soup, main)
    for tag in main.find_all(NOISE_TAGS):
        tag.decompose()
    for tag in main.find_all(class_=NOISE_CLASSES):
        if not tag.decomposed:
            tag.decompose()
    # MarkItDown drops formatting that holds only a space, joining the words either side
    # ("aims<i> </i>to" became "aimsto"). Keep the space.
    for tag in main.find_all(INLINE_TAGS):
        if tag.parent is not None and tag.get_text() and not tag.get_text().strip():
            tag.replace_with(" ")
    # A card's subheadings belong under its title. At the same level they read as more
    # cards: the AI Assurance Framework's seven parts would become eight.
    for card in main.select(CARD_SELECTOR):
        headings = card.find_all(HEADINGS)
        if headings:
            level = int(headings[0].name[1])
            for heading in headings[1:]:
                if int(heading.name[1]) <= level:
                    heading.name = f"h{min(level + 1, 6)}"
    # Cloudflare re-encodes email links on every request, which would change the
    # passage hash each run. Presidio would mask the address anyway.
    for tag in main.select(f'a[href*="{CF_EMAIL}"], .__cf_email__'):
        tag.replace_with("[email address]")
    return str(main)


def html_to_markdown(content: bytes) -> str:
    stream = io.BytesIO(main_content(content).encode("utf-8"))
    result = _markitdown.convert_stream(
        stream, stream_info=StreamInfo(extension=".html", charset="utf-8")
    )
    return result.text_content


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# A list item that is only a heading, as accordions inside lists become ("* ###### Q").
_LIST_HEADING = re.compile(r"^\s*[*+-]\s+(#{1,6}\s.*)$")
_EMPHASIS = re.compile(r"(\*\*|__|(?<!\w)[*_]|[*_](?!\w))")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# PyMuPDF4LLM marks underlined and highlighted text, and text inside pictures, with HTML.
_INLINE_HTML = re.compile(r"</?(?:u|mark)>|<!--.*?-->")
# And a line break inside a table cell, often mid-sentence.
_CELL_BREAK = re.compile(r"\s*<br\s*/?>\s*")
# A bold line that is only a clause number and title, such as "**3.2. We are accurate**".
# Some policy pages mark clauses this way instead of with headings.
_BOLD_CLAUSE = re.compile(r"^\*\*(\d+\.\d+(?:\.\d+)*\.?\s+[^*]+?)\*\*$")
# A list item or heading that is nothing but a link: menus, "related pages", downloads.
_LINK_ONLY = re.compile(r"^\s*(?:[*+-]|#{1,6})\s+\[[^\]]*\]\([^)]*\)\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[*+-]|\d+\.)\s")
_BARE_HEADING = re.compile(r"^\s*#{1,6}\s*$")
# A table of contents row: a title, dots and a page number. Like the sections chunking
# skips, contents match questions about anything and answer none.
_CONTENTS_ROW = re.compile(r"\.{5,}\s*\d+\s*\|?\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-{0,}:?\s*$")
# No-break and other Unicode spaces. LangChain's header splitter deletes them as
# unprintable, joining the words ("the Cyber Security Policy" became "theCyber
# SecurityPolicy").
_UNICODE_SPACE = re.compile(r"[^\S \n\t]")


def clean_heading(text: str) -> str:
    """Strip bold and italic markers that converters leave inside headings."""
    text = _IMAGE.sub("", text)
    text = _EMPHASIS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_markdown(markdown: str) -> str:
    lines: list[str] = []
    level = 0  # of the last heading, so a clause sits one level below it
    # In a list that follows a sentence ("Critical policies to consider include:"), a
    # link is content, not a menu: the list is what the sentence says. Menus follow a
    # heading ("See also") or another link.
    introduced = False
    for line in _UNICODE_SPACE.sub(" ", markdown).splitlines():
        line = _CELL_BREAK.sub(" ", _INLINE_HTML.sub("", _IMAGE.sub("", line))).rstrip()
        listed = bool(_LIST_ITEM.match(line))
        if line.strip() and not listed:
            introduced = line.endswith((":", "."))
        if _LINK_ONLY.match(line) and not (introduced and listed):
            continue
        # An empty heading would start a section with no name, taking the text below it
        # away from its real heading.
        if _BARE_HEADING.match(line) or _CONTENTS_ROW.search(line):
            continue
        # A table's rule with no header row above it: what's left of a contents table.
        if _TABLE_RULE.match(line) and not (lines and lines[-1].lstrip().startswith("|")):
            continue
        line = _LINK.sub(r"\1", line)
        if m := _LIST_HEADING.match(line):
            line = m.group(1)
        if m := _BOLD_CLAUSE.match(line.strip()):
            line = f"{'#' * min(level + 1, 6)} {' '.join(m.group(1).split())}"
        # Indented inside a list item, as an accordion's panel often is. The splitter
        # reads it as a heading, so it counts as one here too.
        elif m := _HEADING.match(line.strip()):
            title = clean_heading(m.group(2))
            if not title:
                continue
            level = len(m.group(1))
            line = f"{m.group(1)} {title}"
        lines.append(line)
    # Again over the whole text, for a link whose text runs over more than one line.
    text = _LINK.sub(r"\1", "\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
