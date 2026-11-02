"""Download a source and check it is the kind of document its URL promises."""

import time
from dataclasses import dataclass, field
from typing import Literal

import httpx

from ingest.sources import Source

USER_AGENT = "PolicyDesk-ingest/0.1 (weekly fetch of public policy documents)"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
# www.unsw.edu.au asks for 10 seconds between requests (robots.txt Crawl-delay).
CRAWL_DELAY = 10.0

Kind = Literal["pdf", "html"]


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class Fetched:
    source: Source
    kind: Kind
    content: bytes
    final_url: str


@dataclass
class Pacer:
    """Waits so that requests to one host are at least `delay` seconds apart."""

    delay: float
    last: dict[str, float] = field(default_factory=dict)

    def __call__(self, request: httpx.Request) -> None:
        host = request.url.host
        wait = self.last.get(host, float("-inf")) + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last[host] = time.monotonic()


def make_client(
    transport: httpx.BaseTransport | None = None, crawl_delay: float | None = None
) -> httpx.Client:
    """An HTTP client for fetching sources. Requests to the same host are paced by
    `crawl_delay`, which defaults to CRAWL_DELAY on the network and to 0 for a test
    transport."""
    if crawl_delay is None:
        crawl_delay = CRAWL_DELAY if transport is None else 0.0
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
        follow_redirects=True,
        transport=transport,
        event_hooks={"request": [Pacer(crawl_delay)]},
    )


def fetch(client: httpx.Client, source: Source) -> Fetched:
    try:
        response = client.get(source.url)
    except httpx.HTTPError as e:
        raise FetchError(f"{type(e).__name__}: {e}") from e
    if response.status_code != 200:
        raise FetchError(f"HTTP {response.status_code}")

    content = response.content
    content_type = response.headers.get("content-type", "").lower()
    is_pdf = content.startswith(b"%PDF-")

    # A moved PDF often comes back as a "page not found" page with HTTP 200.
    if source.url.lower().endswith(".pdf") and not is_pdf:
        raise FetchError(f"Expected a PDF but got {content_type or 'unknown content'}")
    if is_pdf:
        return Fetched(source, "pdf", content, str(response.url))
    if "html" not in content_type:
        raise FetchError(f"Unsupported content type: {content_type or 'unknown'}")
    return Fetched(source, "html", content, str(response.url))
