"""Mask personal information with Presidio before text reaches a model, a store or a log.

Questions lose contact details, identifiers and people's names: a question about UNSW
policy never needs a name. Documents lose contact details and identifiers only, since
the names in UNSW's public pages are the roles and units that answers need. An
allow-list keeps tool and service names that name detection can take for people.
"""

from dataclasses import dataclass
from functools import cache

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import (
    AuMedicareRecognizer,
    AuTfnRecognizer,
    PhoneRecognizer,
)
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

SPACY_MODEL = "en_core_web_sm"

# What each entity becomes in the masked text, and what the page calls it.
LABELS = {
    "PERSON": "name",
    "EMAIL_ADDRESS": "email address",
    "PHONE_NUMBER": "phone number",
    "UNSW_ZID": "zID",
    "AU_TFN": "tax file number",
    "AU_MEDICARE": "Medicare number",
    "CREDIT_CARD": "card number",
    "IP_ADDRESS": "IP address",
}
DOCUMENT_ENTITIES = [e for e in LABELS if e != "PERSON"]
QUESTION_ENTITIES = list(LABELS)

# Names of tools, services and styles that a question may use and name detection
# may take for a person.
ALLOW_LIST = [
    "APA",
    "ChatGPT",
    "ChatGPT Edu",
    "Claude",
    "Copilot",
    "Copilot Chat",
    "DeepSeek",
    "Gemini",
    "Grammarly",
    "Harvard",
    "Microsoft Copilot",
    "Moodle",
    "Quillbot",
    "Scout",
    "SharePoint",
    "Teams",
    "Turnitin",
    "UNSW",
]

# Detections below this score are left alone (Presidio scores 0 to 1).
MIN_SCORE = 0.4


@dataclass(frozen=True)
class Masked:
    text: str
    # What was removed, as the page names it, each once, in order of first appearance.
    removed: tuple[str, ...] = ()


@cache
def _analyzer() -> AnalyzerEngine:
    nlp = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": SPACY_MODEL}],
        }
    ).create_engine()
    registry = RecognizerRegistry()
    registry.load_predefined_recognizers(nlp_engine=nlp, languages=["en"])
    registry.remove_recognizer("PhoneRecognizer")
    registry.add_recognizer(PhoneRecognizer(supported_regions=("AU", "GB", "US")))
    registry.add_recognizer(AuTfnRecognizer())
    registry.add_recognizer(AuMedicareRecognizer())
    registry.add_recognizer(
        PatternRecognizer(
            supported_entity="UNSW_ZID",
            name="UnswZidRecognizer",
            patterns=[Pattern("zID", r"\b[zZ]\d{7}\b", 0.9)],
        )
    )
    return AnalyzerEngine(registry=registry, nlp_engine=nlp, supported_languages=["en"])


@cache
def _anonymizer() -> AnonymizerEngine:
    return AnonymizerEngine()


def _mask(text: str, entities: list[str]) -> Masked:
    if not text.strip():
        return Masked(text)
    found = _analyzer().analyze(
        text=text,
        language="en",
        entities=entities,
        allow_list=ALLOW_LIST,
        score_threshold=MIN_SCORE,
    )
    if not found:
        return Masked(text)
    operators = {
        entity: OperatorConfig("replace", {"new_value": f"[{label}]"})
        for entity, label in LABELS.items()
    }
    masked = _anonymizer().anonymize(text=text, analyzer_results=found, operators=operators)  # type: ignore[arg-type]
    # Count the replacements made, not the detections: overlapping detections (an
    # email address also taken for a name) become one replacement.
    in_order = sorted(masked.items, key=lambda item: item.start)
    removed = tuple(dict.fromkeys(LABELS[item.entity_type] for item in in_order))
    return Masked(masked.text, removed)


def mask_question(text: str) -> Masked:
    """Contact details, identifiers and people's names."""
    return _mask(text, QUESTION_ENTITIES)


def mask_document(text: str) -> Masked:
    """Contact details and identifiers; names stay."""
    return _mask(text, DOCUMENT_ENTITIES)
