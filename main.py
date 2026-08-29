"""CLI entry point: read identifiers, pull EDGAR records, write the deliverable.

Usage:
    python main.py --input data/companies.csv --email you@example.com
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

from edgar_extractor import __version__
from edgar_extractor.edgar import CompanyNotFound, TickerIndex, fetch_company
from edgar_extractor.http_client import HttpClient
from edgar_extractor.report import write_csvs, write_workbook

log = logging.getLogger("edgar_extractor")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract public-company directory and filing data from SEC EDGAR.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="CSV file with an 'identifier' column")
    source.add_argument("--tickers", help="Comma-separated tickers or company names")

    parser.add_argument("--email", required=True,
                        help="Contact email; required by the SEC access policy")
    parser.add_argument("--forms", default="10-K,10-Q,8-K",
                        help="Comma-separated form types, or 'ALL'")
    parser.add_argument("--since", default="2023-01-01",
                        help="Only filings on/after this date (YYYY-MM-DD)")
    parser.add_argument("--max-filings", type=int, default=25,
                        help="Max filings kept per company")
    parser.add_argument("--limit", type=int, help="Only process the first N identifiers")
    parser.add_argument("--output", type=Path, default=Path("output"), help="Output directory")
    parser.add_argument("--rps", type=float, default=6.0,
                        help="Requests per second (SEC allows up to 10)")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the on-disk cache")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve identifiers only; make no filing requests")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def read_identifiers(args) -> list[str]:
    if args.tickers:
        values = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        if not args.input.exists():
            raise SystemExit(f"Input file not found: {args.input}")
        with args.input.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "identifier" not in reader.fieldnames:
                raise SystemExit(
                    f"{args.input} must have an 'identifier' column "
                    f"(found: {reader.fieldnames})"
                )
            values = [row["identifier"].strip() for row in reader if row.get("identifier", "").strip()]

    if not values:
        raise SystemExit("No identifiers to process.")
    return values[: args.limit] if args.limit else values


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    identifiers = read_identifiers(args)
    forms = None if args.forms.upper() == "ALL" else {f.strip().upper() for f in args.forms.split(",")}
    try:
        since = date.fromisoformat(args.since) if args.since else None
    except ValueError:
        raise SystemExit(f"--since must be YYYY-MM-DD, got: {args.since}")

    client = HttpClient(
        user_agent=f"EdgarCompanyExtractor/{__version__} ({args.email})",
        requests_per_second=args.rps,
        use_cache=not args.no_cache,
    )

    started = time.monotonic()
    log.info("Processing %d identifier(s) | forms=%s | since=%s",
             len(identifiers), args.forms, args.since)

    index = TickerIndex.load(client)
    companies, filings, errors = [], [], []

    for position, identifier in enumerate(identifiers, start=1):
        prefix = f"[{position}/{len(identifiers)}] {identifier}"
        try:
            match = index.resolve(identifier)
        except CompanyNotFound as exc:
            log.warning("%s -> SKIPPED (%s)", prefix, exc)
            errors.append([identifier, "Could not resolve", str(exc)])
            continue

        if args.dry_run:
            log.info("%s -> CIK %d (%s) [dry run]", prefix, match["cik"], match["title"])
            continue

        try:
            company, company_filings = fetch_company(
                client, match["cik"], forms, since, args.max_filings
            )
        except Exception as exc:  # keep one bad record from killing the run
            log.error("%s -> FAILED (%s)", prefix, exc)
            errors.append([identifier, type(exc).__name__, str(exc)[:300]])
            continue

        companies.append(company)
        filings.extend(company_filings)
        log.info("%s -> %s | %d filings", prefix, company.name, len(company_filings))

    if args.dry_run:
        log.info("Dry run complete: %d resolved, %d unresolved",
                 len(identifiers) - len(errors), len(errors))
        return 0

    duration = time.monotonic() - started
    stats = {
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round(duration, 1),
        "requested": len(identifiers),
        "failed": len(errors),
        "success_rate": len(companies) / len(identifiers) if identifiers else 0.0,
        **client.stats,
    }

    stem = f"edgar_companies_{datetime.now():%Y%m%d_%H%M%S}"
    workbook = write_workbook(companies, filings, errors, stats, args.output / f"{stem}.xlsx")
    write_csvs(companies, filings, args.output, stem)

    log.info(
        "Done in %.1fs | %d companies, %d filings, %d failed | %d downloads, %d cached",
        duration, len(companies), len(filings), len(errors),
        client.stats["downloads"], client.stats["cache_hits"],
    )
    log.info("Deliverable: %s", workbook.resolve())
    return 1 if not companies else 0


if __name__ == "__main__":
    sys.exit(main())
