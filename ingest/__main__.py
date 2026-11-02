"""Run the ingest job: python -m ingest [--dry-run] [--only ID ...] [--include-unverified]"""

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any

from app.embedding import AzureEmbedder
from app.settings import get_settings
from ingest.fetch import make_client
from ingest.pipeline import record, run
from ingest.sources import load_sources, select_sources
from ingest.store import AzureSearchStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest", description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch, convert and chunk only; no Azure calls"
    )
    parser.add_argument("--only", nargs="+", metavar="ID", help="ingest only these source IDs")
    parser.add_argument(
        "--include-unverified",
        action="store_true",
        help="also ingest sources whose licence is unverified",
    )
    args = parser.parse_args(argv)

    # force: in Lambda the runtime has already configured logging.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True
    )
    for noisy in ("httpx", "azure", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    all_sources = load_sources()
    sources, skipped = select_sources(
        all_sources,
        include_unverified=args.include_unverified,
        only=set(args.only) if args.only else None,
    )
    embedder, store = (None, None)
    if not args.dry_run:
        settings = get_settings()
        embedder, store = AzureEmbedder(settings), AzureSearchStore(settings)

    started, clock = datetime.now(UTC), time.monotonic()
    with make_client() as client:
        result = run(sources, skipped, client, embedder, store, listed={s.id for s in all_sources})

    totals = {
        k: sum(getattr(r, k) for r in result.results)
        for k in ("chunks", "added", "unchanged", "removed")
    }
    logging.info(
        "Done: %d sources, %d failed, %d skipped. chunks=%d added=%d unchanged=%d removed=%d",
        len(result.results),
        len(result.failed),
        len(result.skipped_unverified),
        *totals.values(),
    )
    bucket = "" if args.dry_run else get_settings().eval_records_bucket
    if bucket:
        summary = record(
            result, started=started, duration_s=time.monotonic() - clock, partial=bool(args.only)
        )
        publish(summary, bucket)
    return 1 if result.failed else 0


def publish(summary: dict[str, Any], bucket: str, client: Any = None) -> None:
    """Records the run in the evaluation records bucket. A failure here doesn't fail the
    ingest, whose index is already up to date; the drift check reports a missing record."""
    try:
        if client is None:
            import boto3

            client = boto3.client("s3")
        client.put_object(
            Bucket=bucket,
            Key=f"ingest/{summary['id']}.json",
            Body=json.dumps(summary, indent=2).encode(),
            ContentType="application/json",
        )
        logging.info("Recorded the run: s3://%s/ingest/%s.json", bucket, summary["id"])
    except Exception as e:
        logging.warning("Couldn't record the run in %s: %s", bucket, e)


if __name__ == "__main__":
    sys.exit(main())
