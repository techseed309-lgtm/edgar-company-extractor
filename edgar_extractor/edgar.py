"""SEC EDGAR lookups: resolve identifiers to CIKs and pull company records.

Only endpoints permitted by https://www.sec.gov/robots.txt are used:
/Archives/edgar/data is explicitly allowed, /cgi-bin is disallowed and is
therefore never requested.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import date

log = logging.getLogger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FILING_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
COMPANY_PAGE_URL = "https://www.sec.gov/edgar/browse/?CIK={cik:010d}"

# Legal-form suffixes stripped before fuzzy name matching.
_NAME_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
    "LLC", "LTD", "LP", "PLC", "SA", "NV", "AG", "HOLDINGS", "GROUP", "THE",
}


class CompanyNotFound(Exception):
    """Raised when an identifier cannot be resolved to a CIK."""


@dataclass
class Company:
    cik: int
    name: str
    ticker: str = ""
    exchange: str = ""
    sic_code: str = ""
    industry: str = ""
    phone: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    state_of_incorporation: str = ""
    fiscal_year_end: str = ""
    filings_matched: int = 0
    missing_fields: str = ""
    edgar_url: str = ""

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class Filing:
    cik: int
    company: str
    ticker: str
    form: str
    filing_date: str
    report_date: str
    accession_number: str
    document_url: str
    size_kb: int = 0

    def to_row(self) -> dict:
        return asdict(self)


def normalize_name(name: str) -> str:
    """Uppercase, strip punctuation and legal suffixes for tolerant matching."""
    cleaned = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    words = [w for w in cleaned.split() if w not in _NAME_SUFFIXES]
    return " ".join(words)


def normalize_phone(raw: str) -> str:
    """Format US numbers as (XXX) XXX-XXXX; leave anything else untouched.

    EDGAR stores phone numbers however the filer typed them, so the raw feed
    mixes "4048761234", "425-882-8080" and "(408) 996-1010" in one column.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return (raw or "").strip()


class TickerIndex:
    """Lookup table mapping tickers and company names to CIK numbers."""

    def __init__(self, records: dict) -> None:
        self.by_ticker: dict[str, dict] = {}
        self.by_name: dict[str, list[dict]] = {}
        for entry in records.values():
            item = {
                "cik": int(entry["cik_str"]),
                "ticker": entry["ticker"].upper(),
                "title": entry["title"],
            }
            self.by_ticker.setdefault(item["ticker"], item)
            # Roughly 1,400 names in SEC's index map to more than one CIK, so
            # keep every candidate and let resolve() refuse to guess.
            bucket = self.by_name.setdefault(normalize_name(item["title"]), [])
            if all(existing["cik"] != item["cik"] for existing in bucket):
                bucket.append(item)

    @classmethod
    def load(cls, client) -> "TickerIndex":
        log.info("Loading SEC ticker index...")
        index = cls(client.get_json(TICKER_MAP_URL))
        log.info("Indexed %d tickers / %d company names", len(index.by_ticker), len(index.by_name))
        return index

    def resolve(self, identifier: str) -> dict:
        """Resolve a ticker, company name, or bare CIK to an index entry."""
        text = identifier.strip()
        if not text:
            raise CompanyNotFound("empty identifier")

        if text.upper() in self.by_ticker:
            return self.by_ticker[text.upper()]

        normalized = normalize_name(text)
        exact = self.by_name.get(normalized, [])
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise CompanyNotFound(_ambiguity_message(exact))

        if text.isdigit():
            return {"cik": int(text), "ticker": "", "title": f"CIK {text}"}

        # Fall back to a unique prefix match before giving up.
        if normalized:
            prefixed = [
                item
                for key, bucket in self.by_name.items()
                if key.startswith(normalized)
                for item in bucket
            ]
            if len(prefixed) == 1:
                return prefixed[0]
            if len(prefixed) > 1:
                raise CompanyNotFound(_ambiguity_message(prefixed))
        raise CompanyNotFound("no matching ticker or company name")


def _ambiguity_message(candidates: list[dict]) -> str:
    """Explain a collision using the CIKs the client can disambiguate with."""
    shown = ", ".join(f"{c['title']} (CIK {c['cik']})" for c in candidates[:3])
    suffix = ", ..." if len(candidates) > 3 else ""
    return f"ambiguous - {len(candidates)} companies match: {shown}{suffix}. Use a ticker or CIK."


def _accession_no_dashes(accession: str) -> str:
    return accession.replace("-", "")


def fetch_company(client, cik: int, forms: set[str] | None, since: date | None,
                  max_filings: int) -> tuple[Company, list[Filing]]:
    """Fetch one company's profile and its matching filings."""
    payload = client.get_json(SUBMISSIONS_URL.format(cik=cik))
    address = (payload.get("addresses") or {}).get("business") or {}
    tickers = payload.get("tickers") or []
    exchanges = payload.get("exchanges") or []

    company = Company(
        cik=cik,
        name=payload.get("name", "").strip(),
        ticker=tickers[0] if tickers else "",
        exchange=exchanges[0] if exchanges else "",
        sic_code=str(payload.get("sic") or ""),
        industry=payload.get("sicDescription", "") or "",
        phone=normalize_phone(payload.get("phone") or ""),
        street=(address.get("street1") or "").strip(),
        city=(address.get("city") or "").strip(),
        state=(address.get("stateOrCountry") or "").strip(),
        zip_code=(address.get("zipCode") or "").strip(),
        state_of_incorporation=payload.get("stateOfIncorporation", "") or "",
        fiscal_year_end=payload.get("fiscalYearEnd", "") or "",
        edgar_url=COMPANY_PAGE_URL.format(cik=cik),
    )

    # Flag thin records so the client can see data quality at a glance.
    expected = {"phone": company.phone, "street": company.street,
                "city": company.city, "industry": company.industry}
    company.missing_fields = ", ".join(k for k, v in expected.items() if not v)

    recent = (payload.get("filings") or {}).get("recent") or {}
    filings: list[Filing] = []
    for i in range(len(recent.get("accessionNumber", []))):
        form = recent["form"][i]
        if forms and form.upper() not in forms:
            continue
        filed = recent["filingDate"][i]
        if since and filed < since.isoformat():
            continue
        document = recent.get("primaryDocument", [""] * (i + 1))[i]
        if not document:
            continue
        accession = recent["accessionNumber"][i]
        filings.append(
            Filing(
                cik=cik,
                company=company.name,
                ticker=company.ticker,
                form=form,
                filing_date=filed,
                report_date=recent.get("reportDate", [""] * (i + 1))[i] or "",
                accession_number=accession,
                document_url=FILING_DOC_URL.format(
                    cik=cik, accession=_accession_no_dashes(accession), document=document
                ),
                size_kb=round((recent.get("size", [0] * (i + 1))[i] or 0) / 1024),
            )
        )
        if len(filings) >= max_filings:
            break

    company.filings_matched = len(filings)
    return company, filings
