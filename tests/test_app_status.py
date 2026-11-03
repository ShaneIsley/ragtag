import io
import json
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import status
from app.main import app, get_status
from app.settings import Settings
from tests.test_evals_status import snapshot

SNAP = snapshot(answered=3, declined=1, masked=2)


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)
    app.dependency_overrides.clear()
    status.clear_cache()


def show(snap: dict[str, Any] | None, alarms: list[status.Alarm] | None) -> None:
    current = status.Status(snapshot=snap, alarms=alarms, trends=status.trends(snap))
    app.dependency_overrides[get_status] = lambda: current


ALARMS = [
    status.Alarm("Errors answering questions", "OK", True, "2 Nov, 3:00 PM"),
    status.Alarm("The weekly update of the sources", "Alarm", False, "16 Nov, 3:05 AM"),
]


def test_the_status_page_shows_the_checks_without_inline_styles(client: TestClient) -> None:
    show(SNAP, ALARMS)
    response = client.get("/status")
    html = response.text
    assert response.status_code == 200 and 'lang="en-AU"' in html
    # The content security policy allows only the nonced style block.
    assert not re.search(r"\sstyle=", html)
    assert "1 of 2 alarms raised" in html
    assert "Expected facts stated" in html and "83%" in html
    assert 'role="img"' in html and "over the last 3 full runs" in html
    assert "Checking the checkers" in html and "93%" in html
    assert "Sources changed" in html
    assert html.count("<dt>") == 7  # the Assurance Framework's parts


def test_small_counts_show_as_fewer_than_five(client: TestClient) -> None:
    show(SNAP, None)
    html = client.get("/status").text
    assert "fewer than 5" in html
    assert '<td class="num">3</td>' not in html and '<td class="num">2</td>' not in html
    assert "shown on the deployed site only" in html


def test_alarms_that_couldn_t_be_read_aren_t_mistaken_for_a_local_run(client: TestClient) -> None:
    show(SNAP, [])
    html = client.get("/status").text
    assert "aren't available at the moment" in html and "deployed site only" not in html


def test_a_missing_snapshot_is_looked_for_again_soon(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    found: list[dict[str, Any] | None] = [None, {"generated": "now"}]
    status.clear_cache()
    assert status._cached("snapshot", status.SNAPSHOT_TTL_S, lambda: found[0]) is None
    clock[0] += status.MISSING_TTL_S + 1
    assert status._cached("snapshot", status.SNAPSHOT_TTL_S, lambda: found[1]) == {
        "generated": "now"
    }
    clock[0] += status.MISSING_TTL_S + 1  # a snapshot that was found is kept longer
    assert status._cached("snapshot", status.SNAPSHOT_TTL_S, lambda: None) == {"generated": "now"}
    status.clear_cache()


def test_without_a_snapshot_the_page_says_so(client: TestClient) -> None:
    show(None, None)
    html = client.get("/status").text
    assert "results aren't available yet" in html


def test_the_main_page_links_to_the_status_page(client: TestClient) -> None:
    assert 'href="/status"' in client.get("/").text


def test_the_trend_line_puts_the_minimum_below_the_values() -> None:
    points, floor = status.line([0.84, 0.80, 0.83], 0.68)
    assert points.startswith("0.0,") and points.count(" ") == 2
    assert floor is not None and all(float(p.split(",")[1]) < floor for p in points.split())
    assert status.line([], 0.5) == ("", None)
    one, _ = status.line([0.9], None)
    assert one.count(",") == 2  # a single run draws as a flat line


def deployed() -> Settings:
    return Settings(_env_file=None, eval_records_bucket="records")


def test_alarms_are_labelled_in_order_and_unavailable_on_error() -> None:
    when = datetime(2026, 11, 16, 4, 5, tzinfo=UTC)

    class CloudWatch:
        def get_paginator(self, name: str) -> Any:
            class Pages:
                def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                    assert kwargs["AlarmNamePrefix"] == "policy-desk-"
                    alarms = [
                        {
                            "AlarmName": "policy-desk-drift",
                            "StateValue": "ALARM",
                            "StateUpdatedTimestamp": when,
                        },
                        {
                            "AlarmName": "policy-desk-app-errors",
                            "StateValue": "OK",
                            "StateUpdatedTimestamp": when,
                        },
                        {
                            "AlarmName": "someone-else",
                            "StateValue": "OK",
                            "StateUpdatedTimestamp": when,
                        },
                    ]
                    return [{"MetricAlarms": alarms}]

            return Pages()

    status.clear_cache()
    found = status.alarms(deployed(), CloudWatch())
    assert found is not None
    assert [a.label for a in found] == [status.ALARMS["app-errors"], status.ALARMS["drift"]]
    assert found[1].state == "Alarm" and not found[1].ok and found[1].since == "16 Nov, 3:05 PM"

    class Broken:
        def get_paginator(self, name: str) -> Any:
            raise RuntimeError("AccessDenied")

    status.clear_cache()
    assert status.alarms(deployed(), Broken()) == []  # "not available", not "not deployed"
    assert status.alarms(Settings(_env_file=None)) is None


def test_the_snapshot_is_read_from_the_bucket_and_cached() -> None:
    calls: list[str] = []

    class S3:
        def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
            calls.append(Key)
            return {"Body": io.BytesIO(json.dumps({"generated": "now"}).encode())}

    status.clear_cache()
    assert status.snapshot(deployed(), S3()) == {"generated": "now"}
    assert status.snapshot(deployed(), S3()) == {"generated": "now"}
    assert calls == [status.SNAPSHOT_KEY]
    status.clear_cache()


@pytest.mark.parametrize("path", ["/", "/status"])
def test_both_pages_offer_auto_light_and_dark(client: TestClient, path: str) -> None:
    if path == "/status":
        show(SNAP, None)
    html = client.get(path).text
    head = html.split("</head>")[0]
    # The saved choice is applied before the page draws, and only by a nonced script.
    assert 'localStorage.getItem("theme")' in head and "<script nonce=" in head
    assert '<fieldset class="theme" id="theme" hidden>' in html  # shown by script only
    assert [v for v in ("auto", "light", "dark") if f'value="{v}"' in html] == [
        "auto",
        "light",
        "dark",
    ]
    assert not re.search(r"\sstyle=", html)


def test_the_privacy_notice_mentions_the_remembered_theme(client: TestClient) -> None:
    assert "your browser remembers the choice" in client.get("/").text
