from collections.abc import Iterator

import openai
import pytest
from fastapi.testclient import TestClient

from app.main import app, get_chat, get_retriever
from tests.fakes import FakeChat, FakeRetriever, passage


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)
    app.dependency_overrides.clear()


def use(retriever: object, chat: object) -> None:
    app.dependency_overrides[get_retriever] = lambda: retriever
    app.dependency_overrides[get_chat] = lambda: chat


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "version": None}


def test_page_has_a_labelled_question_box(client: TestClient) -> None:
    html = client.get("/").text
    assert '<label for="question">' in html
    assert 'id="question"' in html
    assert 'role="status"' in html
    assert 'lang="en-AU"' in html


def test_page_tells_people_where_their_question_goes(client: TestClient) -> None:
    html = client.get("/").text
    # Screen readers announce the notice with the question box.
    assert 'aria-describedby="question-hint question-privacy"' in html
    assert 'id="question-privacy"' in html and 'href="#privacy"' in html
    assert 'id="privacy"' in html
    assert "outside the OECD" in html
    assert "privacy impact assessment" in html
    assert "<h3>Rules on where information goes</h3>" in html


def test_page_says_when_the_site_closes(client: TestClient) -> None:
    assert "shut down at the end of November 2026" in client.get("/").text


def test_ask_returns_the_answer_with_its_sources(client: TestClient) -> None:
    use(FakeRetriever([passage(1), passage(2)]), FakeChat("The Board approves it [2]."))
    body = client.post("/ask", json={"question": "  Who approves high-risk AI?  "}).json()
    assert body["question"] == "Who approves high-risk AI?"
    assert body["answer"] == "The Board approves it [2]."
    assert not body["declined"]
    assert [s["number"] for s in body["sources"]] == [2]
    assert body["sources"][0]["url"].startswith("https://www.digital.gov.au/")
    assert len(body["retrieved"]) == 2
    assert body["prompt_version"] == "answer.v4"


def test_ask_declines_off_topic_questions(client: TestClient) -> None:
    use(FakeRetriever([passage(1, similarity=0.2)]), FakeChat("unused"))
    body = client.post("/ask", json={"question": "What is the capital of France?"}).json()
    assert body["declined"] and body["decline_reason"] == "no_match"
    assert body["sources"] == []


@pytest.mark.parametrize("question", ["", "   ", "x" * 501])
def test_ask_rejects_empty_or_long_questions(client: TestClient, question: str) -> None:
    use(FakeRetriever([]), FakeChat("unused"))
    assert client.post("/ask", json={"question": question}).status_code == 422


def test_service_failure_is_a_503(client: TestClient) -> None:
    class Down:
        def complete(self, system: str, user: str, *, effort: str = "low") -> object:
            raise openai.APIConnectionError(request=None)  # type: ignore[arg-type]

    use(FakeRetriever([passage(1)]), Down())
    response = client.post("/ask", json={"question": "Who approves?"})
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]


def test_security_headers_and_a_fresh_nonce_per_page(client: TestClient) -> None:
    first, second = client.get("/"), client.get("/")
    csp = first.headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in first.text
    assert f'<style nonce="{nonce}">' in first.text
    assert nonce not in second.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.headers["strict-transport-security"].startswith("max-age=")


def test_page_signs_the_ask_body_for_cloudfront(client: TestClient) -> None:
    assert '"x-amz-content-sha256": await sha256Hex(body)' in client.get("/").text


def test_api_docs_are_off(client: TestClient) -> None:
    assert client.get("/api/docs").status_code == 404
    assert client.get("/docs").status_code == 404
