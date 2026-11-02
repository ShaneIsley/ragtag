"""Convert a fetched document to Markdown whose headings give each passage its section.

PDFs go through PyMuPDF4LLM in layout mode, which nests headings and drops running
headers and footers. Web pages go through MarkItDown, using only the page's main
content so menus, cookie banners and footers don't become passages.
See docs/decisions.md, "Document conversion".
"""

import io
import re

import pymupdf
import pymupdf4llm
from bs4 import BeautifulSoup
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

_markitdown = MarkItDown()


class ConvertError(Exception):
    pass


def to_markdown(doc: Fetched) -> str:
    markdown = pdf_to_markdown(doc.content) if doc.kind == "pdf" else html_to_markdown(doc.content)
    markdown = clean_markdown(markdown)
    if len(markdown.split()) < 50:
        raise ConvertError(f"Only {len(markdown.split())} words after conversion")
    return markdown


def pdf_to_markdown(content: bytes) -> str:
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        text: str = pymupdf4llm.to_markdown(pdf, header=False, footer=False, use_ocr=False)
    return text


CF_EMAIL = "/cdn-cgi/l/email-protection"
# Below any heading a page is likely to use for its own sections.
ACCORDION_HEADING = "h6"


def main_content(html: bytes) -> str:
    """The page's main content as HTML, without navigation, scripts or images."""
    soup = BeautifulSoup(html, "html.parser")
    main = next((m for sel in MAIN_SELECTORS if (m := soup.select_one(sel))), soup)
    # An accordion's button holds the question its panel answers (FAQs). Keep it as a
    # heading, so each answer is chunked under its question, before buttons go.
    for button in main.select("button[aria-controls]"):
        if text := " ".join(button.get_text(" ").split()):
            heading = soup.new_tag(ACCORDION_HEADING)
            heading.string = text
            button.replace_with(heading)
    for tag in main.find_all(NOISE_TAGS):
        tag.decompose()
    for tag in main.find_all(class_=NOISE_CLASSES):
        if not tag.decomposed:
            tag.decompose()
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
# A bold line that is only a clause number and title, such as "**3.2. We are accurate**".
# Some policy pages mark clauses this way instead of with headings.
_BOLD_CLAUSE = re.compile(r"^\*\*(\d+\.\d+(?:\.\d+)*\.?\s+[^*]+?)\*\*$")
# A list item or heading that is nothing but a link: menus, "related pages", downloads.
_LINK_ONLY = re.compile(r"^\s*(?:[*+-]|#{1,6})\s+\[[^\]]*\]\([^)]*\)\s*$")


def clean_heading(text: str) -> str:
    """Strip bold and italic markers that converters leave inside headings."""
    text = _IMAGE.sub("", text)
    text = _EMPHASIS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_markdown(markdown: str) -> str:
    lines = []
    level = 0  # of the last heading, so a clause sits one level below it
    for line in markdown.splitlines():
        line = _INLINE_HTML.sub("", _IMAGE.sub("", line)).rstrip()
        if _LINK_ONLY.match(line):
            continue
        line = _LINK.sub(r"\1", line)
        if m := _LIST_HEADING.match(line):
            line = m.group(1)
        if m := _BOLD_CLAUSE.match(line.strip()):
            line = f"{'#' * min(level + 1, 6)} {' '.join(m.group(1).split())}"
        elif m := _HEADING.match(line):
            title = clean_heading(m.group(2))
            if not title:
                continue
            level = len(m.group(1))
            line = f"{m.group(1)} {title}"
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"
