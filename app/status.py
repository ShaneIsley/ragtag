"""The status page's data: the snapshot the scheduled evaluation writes (evals/status.py)
and the alarms' states now. Both are cached briefly, so a busy page doesn't call AWS on
every view, and either can be missing without breaking the page.
"""

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.settings import Settings

log = logging.getLogger(__name__)

SNAPSHOT_KEY = "status/snapshot.json"
SNAPSHOT_TTL_S = 300
ALARMS_TTL_S = 60
# A missing snapshot or a failed read is tried again sooner: the snapshot may have just
# been written, or the fault fixed.
MISSING_TTL_S = 30
ALARM_PREFIX = "policy-desk-"
SYDNEY = ZoneInfo("Australia/Sydney")
# Each alarm, in the order the page lists them, and what it watches in plain words.
ALARMS = {
    "app-errors": "Errors answering questions",
    "app-slow": "Slow answers",
    "app-throttled": "Too many questions at once",
    "mostly-declined": "Most questions declined, as if search had stopped working",
    "blocked-answer": "An answer blocked by the content safety filter",
    "failovers": "Answers coming from the standby model",
    "ingest-failed": "The weekly update of the sources",
    "eval-failed": "The scheduled checks of answer quality",
    "drift": "Changes in the models, sources or questions",
}
STATES = {"OK": "OK", "ALARM": "Alarm", "INSUFFICIENT_DATA": "Not enough data yet"}

_cache: dict[str, tuple[float, Any]] = {}


def _cached[T](key: str, ttl_s: float, load: Callable[[], T]) -> T:
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None:
        kept_for = ttl_s if hit[1] else MISSING_TTL_S
        if now - hit[0] < kept_for:
            value: T = hit[1]
            return value
    fresh = load()
    _cache[key] = (now, fresh)
    return fresh


def clear_cache() -> None:
    _cache.clear()


@dataclass(frozen=True)
class Alarm:
    label: str
    state: str
    ok: bool
    since: str


@dataclass(frozen=True)
class Trend:
    """One measure across the recent full runs, drawn as a line."""

    name: str
    label: str
    latest: float | None
    minimum: float | None
    values: list[float]
    points: str
    floor_y: float | None
    description: str


@dataclass(frozen=True)
class Status:
    snapshot: dict[str, Any] | None
    alarms: list[Alarm] | None
    trends: list[Trend] = field(default_factory=list)


def snapshot(settings: Settings, client: Any = None) -> dict[str, Any] | None:
    """The latest snapshot: from the records bucket when deployed, or a local file."""
    if settings.eval_records_bucket:

        def load() -> dict[str, Any] | None:
            try:
                s3 = client
                if s3 is None:
                    import boto3

                    s3 = boto3.client("s3")
                body = s3.get_object(Bucket=settings.eval_records_bucket, Key=SNAPSHOT_KEY)
                data: dict[str, Any] = json.loads(body["Body"].read())
                return data
            # S3 answers AccessDenied, not NoSuchKey, for a missing object when the
            # reader may not list the bucket: before the first snapshot, that's expected.
            except Exception as e:  # the page shows that it's missing instead
                log.warning("No status snapshot: %s: %s", type(e).__name__, e)
                return None

        return _cached("snapshot", SNAPSHOT_TTL_S, load)
    if settings.status_snapshot_file:
        path = Path(settings.status_snapshot_file)
        if path.exists():
            data: dict[str, Any] = json.loads(path.read_text())
            return data
    return None


def alarms(settings: Settings, client: Any = None) -> list[Alarm] | None:
    """The alarms' states now, on the deployed site. None where there are no alarms
    (locally); an empty list when they couldn't be read."""
    if not settings.eval_records_bucket:
        return None

    def load() -> list[Alarm]:
        try:
            cloudwatch = client
            if cloudwatch is None:
                import boto3

                cloudwatch = boto3.client("cloudwatch")
            found: dict[str, Alarm] = {}
            pages = cloudwatch.get_paginator("describe_alarms").paginate(
                AlarmNamePrefix=ALARM_PREFIX, AlarmTypes=["MetricAlarm"]
            )
            for page in pages:
                for a in page["MetricAlarms"]:
                    name = a["AlarmName"].removeprefix(ALARM_PREFIX)
                    if name in ALARMS:
                        when = a["StateUpdatedTimestamp"].astimezone(SYDNEY)
                        found[name] = Alarm(
                            label=ALARMS[name],
                            state=STATES.get(a["StateValue"], a["StateValue"]),
                            ok=a["StateValue"] != "ALARM",
                            since=f"{when.day} {when:%b}, {when:%-I:%M %p}",
                        )
            return [found[n] for n in ALARMS if n in found]
        except Exception as e:  # the page says the states are unavailable
            log.warning("No alarm states: %s: %s", type(e).__name__, e)
            return []

    return _cached("alarms", ALARMS_TTL_S, load)


def line(
    values: Sequence[float], floor: float | None, width: int = 160, height: int = 36
) -> tuple[str, float | None]:
    """SVG polyline points for a trend, and the y of its minimum, on a scale from just
    below the lowest value or minimum up to 1."""
    if not values:
        return "", None
    low = min([*values, floor if floor is not None else 1.0]) - 0.02
    span = max(1.0 - low, 0.01)

    def y(v: float) -> float:
        return round(height - (v - low) / span * height, 1)

    step = width / max(len(values) - 1, 1)
    xs = (
        [round(i * step, 1) for i in range(len(values))] if len(values) > 1 else [0.0, float(width)]
    )
    ys = [y(v) for v in values] if len(values) > 1 else [y(values[0])] * 2
    return " ".join(f"{x},{yy}" for x, yy in zip(xs, ys, strict=True)), (
        y(floor) if floor is not None else None
    )


def trends(snap: dict[str, Any] | None) -> list[Trend]:
    if not snap:
        return []
    quality = snap.get("quality") or {}
    runs = quality.get("full_runs") or []
    found = []
    for m in quality.get("measures") or []:
        values = [r["means"][m["name"]] for r in runs if m["name"] in (r.get("means") or {})]
        points, floor_y = line(values, m.get("minimum"))
        described = ", ".join(f"{v:.2f}" for v in values) or "no runs yet"
        found.append(
            Trend(
                name=m["name"],
                label=m["label"],
                latest=values[-1] if values else None,
                minimum=m.get("minimum"),
                values=values,
                points=points,
                floor_y=floor_y,
                description=f"{m['label']} over the last {len(values)} full runs: {described}",
            )
        )
    return found


def current(settings: Settings) -> Status:
    snap = snapshot(settings)
    return Status(snapshot=snap, alarms=alarms(settings), trends=trends(snap))
