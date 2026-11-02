from unittest.mock import patch

import httpx
import pytest

from ingest.fetch import FetchError, fetch, make_client
from ingest.sources import Source

PDF = b"%PDF-1.7\n...rest of a PDF..."
HTML = b"<html><body><main><h1>Policy</h1><p>Text</p></main></body></html>"


def client_returning(status: int, content: bytes, content_type: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content, headers={"content-type": content_type})

    return make_client(httpx.MockTransport(handler))


def test_pdf(pdf_source: Source) -> None:
    doc = fetch(client_returning(200, PDF, "application/pdf"), pdf_source)
    assert doc.kind == "pdf"
    assert doc.content == PDF


def test_html(source: Source) -> None:
    doc = fetch(client_returning(200, HTML, "text/html; charset=utf-8"), source)
    assert doc.kind == "html"


def test_moved_pdf_returning_a_page_is_an_error(pdf_source: Source) -> None:
    # The NSW policy's old address returned "Page not found" as HTML with HTTP 200.
    with pytest.raises(FetchError, match="Expected a PDF"):
        fetch(client_returning(200, HTML, "text/html"), pdf_source)


def test_http_error_status(source: Source) -> None:
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch(client_returning(404, b"", "text/html"), source)


def test_unsupported_content_type(source: Source) -> None:
    with pytest.raises(FetchError, match="Unsupported"):
        fetch(client_returning(200, b"PK\x03\x04", "application/zip"), source)


def test_network_failure(source: Source) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(FetchError, match="ReadTimeout"):
        fetch(make_client(httpx.MockTransport(handler)), source)


def test_sends_an_honest_user_agent(source: Source) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html"})

    fetch(make_client(httpx.MockTransport(handler)), source)
    assert seen[0].startswith("PolicyDesk-ingest/")


def test_requests_to_one_host_are_paced() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html"})

    with patch("ingest.fetch.time.sleep", sleeps.append):
        client = make_client(httpx.MockTransport(handler), crawl_delay=10.0)
        client.get("https://a.example/1")
        client.get("https://b.example/1")  # another host: no wait
        client.get("https://a.example/2")
    assert len(sleeps) == 1 and 9.0 < sleeps[0] <= 10.0
