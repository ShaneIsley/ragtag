import pytest
from fastapi.testclient import TestClient

from app.answer import answer
from app.main import app, get_chat, get_retriever
from app.masking import mask_document, mask_question
from tests.fakes import FakeChat, FakeRetriever, passage


@pytest.mark.parametrize(
    ("question", "removed", "gone"),
    [
        (
            "Hi, I'm Jordan Example, student z0000000 (jordan@example.com). Can I use Copilot?",
            ("name", "zID", "email address"),
            ["Jordan Example", "z0000000", "jordan@example.com"],
        ),
        ("Call me on 0400 000 000. How do I reference ChatGPT?", ("phone number",), ["0400"]),
        ("Ring (02) 9385 1000 about Turnitin.", ("phone number",), ["9385 1000"]),
        ("Student Z1234567 got 45% on Turnitin.", ("zID",), ["Z1234567"]),
    ],
)
def test_questions_lose_names_contact_details_and_ids(
    question: str, removed: tuple[str, ...], gone: list[str]
) -> None:
    masked = mask_question(question)
    assert masked.removed == removed
    assert not any(text in masked.text for text in gone)


@pytest.mark.parametrize(
    "question",
    [
        "Can I use DeepSeek for my UNSW work?",
        "Is my data protected when I use Copilot Chat at UNSW?",
        "What is Scout?",
        "Can I use Grammarly or Quillbot on my assignment?",
        "How do I reference ChatGPT in Harvard style?",
    ],
)
def test_tool_names_are_left_alone(question: str) -> None:
    masked = mask_question(question)
    assert masked.text == question and masked.removed == ()


def test_documents_lose_contact_details_but_keep_names() -> None:
    masked = mask_document("Professor Jane Smith chairs it. Email integrity@unsw.edu.au.")
    assert masked.text == "Professor Jane Smith chairs it. Email [email address]."
    assert masked.removed == ("email address",)


def test_documents_keep_the_sign_in_address_format_and_questions_dont() -> None:
    text = "Sign in using your zID@ad.unsw.edu.au email address, not name@unsw.edu.au."
    assert mask_document(text).text == text
    assert mask_question(text).text == (
        "Sign in using your [email address] email address, not [email address]."
    )


def test_the_models_and_search_see_only_the_masked_question() -> None:
    retriever = FakeRetriever([passage(1)])
    chat = FakeChat("Ask your convenor [1].")
    result = answer("I'm z0000000, jo@example.com. Can I use Copilot?", retriever, chat)
    seen = [*chat.rewrite_calls, *(user for _, user in chat.calls), *retriever.questions]
    assert seen and not any("z0000000" in s or "jo@example.com" in s for s in seen)
    assert result.masked == ("zID", "email address")
    assert result.sent_question == "I'm [zID], [email address]. Can I use Copilot?"
    assert result.question == "I'm z0000000, jo@example.com. Can I use Copilot?"


def test_the_api_reports_what_was_masked() -> None:
    app.dependency_overrides[get_retriever] = lambda: FakeRetriever([passage(1)])
    app.dependency_overrides[get_chat] = lambda: FakeChat("Ask your convenor [1].")
    try:
        client = TestClient(app)
        body = client.post(
            "/ask", json={"question": "Call 0400 000 000: can I use Copilot?"}
        ).json()
        page = client.get("/").text
    finally:
        app.dependency_overrides.clear()
    assert body["masked"] == ["phone number"]
    assert "0400" not in body["sent_question"]
    assert 'id="masked"' in page and "Removed from your question before it was sent" in page
