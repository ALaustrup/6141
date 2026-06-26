#!/usr/bin/env python3
"""Fetch selected Zillow property details as JSON.

This script targets a single Zillow property by ZPID. It first tries Zillow's
public web GraphQL endpoint with a conservative, read-only query. If Zillow
rejects that request or the schema has changed, it falls back to scraping the
public property page and extracting embedded JSON plus visible HTML metadata.

The scraper intentionally does not bypass paywalls, login walls, CAPTCHAs, or
other access controls. Use it only where your use complies with Zillow's terms
and applicable law.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover - exercised only without deps.
    missing = exc.name or "required dependency"
    print(
        f"Missing dependency: {missing}. Install dependencies with "
        "`python -m pip install -r requirements.txt`.",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


DEFAULT_ZPID = "18608823"
DEFAULT_ADDRESS = "6141 Sweeney Road, Somerset, CA 95684"
ZILLOW_BASE_URL = "https://www.zillow.com"
GRAPHQL_URL = f"{ZILLOW_BASE_URL}/graphql/"


@dataclass(frozen=True)
class FetchConfig:
    """Network behavior for polite retries and rate-limit handling."""

    timeout_seconds: float = 20.0
    max_retries: int = 3
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0


@dataclass
class Diagnostics:
    """Structured notes about what succeeded, failed, or changed upstream."""

    api_checked: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class RateLimitedError(RuntimeError):
    """Raised when Zillow keeps responding with HTTP 429."""


def build_session() -> requests.Session:
    """Create a requests session with browser-like headers.

    Zillow may reject default Python clients. These headers describe a normal
    browser request without attempting to bypass authentication or bot checks.
    """

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json;q=0.8,*/*;q=0.7"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    return session


def sleep_between_requests(config: FetchConfig) -> None:
    """Sleep a small randomized interval to avoid rapid-fire requests."""

    if config.max_delay_seconds <= 0:
        return
    low = max(0.0, config.min_delay_seconds)
    high = max(low, config.max_delay_seconds)
    time.sleep(random.uniform(low, high))


def request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    config: FetchConfig,
    **kwargs: Any,
) -> requests.Response:
    """Send a request and retry transient/rate-limited responses.

    HTTP 429 uses the server-provided Retry-After value when present. Other
    transient failures use exponential backoff with jitter.
    """

    last_error: Exception | None = None
    for attempt in range(config.max_retries + 1):
        if attempt:
            sleep_between_requests(config)

        try:
            response = session.request(
                method,
                url,
                timeout=config.timeout_seconds,
                **kwargs,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= config.max_retries:
                raise
            continue

        if response.status_code == 429:
            if attempt >= config.max_retries:
                raise RateLimitedError(f"Rate limited by Zillow after {attempt + 1} attempts")

            retry_after = response.headers.get("Retry-After")
            delay = parse_retry_after(retry_after) if retry_after else None
            if delay is None:
                delay = min(60.0, (2**attempt) + random.uniform(0.0, 1.0))
            time.sleep(delay)
            continue

        if response.status_code in {500, 502, 503, 504} and attempt < config.max_retries:
            delay = min(30.0, (2**attempt) + random.uniform(0.0, 1.0))
            time.sleep(delay)
            continue

        return response

    if last_error:
        raise last_error
    raise RuntimeError("Request failed without a response")


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After as seconds; ignore HTTP-date values for simplicity."""

    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def build_property_url(zpid: str, address: str) -> str:
    """Build the canonical Zillow detail URL from address and ZPID."""

    slug = re.sub(r"[^A-Za-z0-9]+", "-", address).strip("-")
    return f"{ZILLOW_BASE_URL}/homedetails/{quote_plus(slug)}/{zpid}_zpid/"


def check_legacy_official_api(diagnostics: Diagnostics) -> None:
    """Record why the old official Zillow API is not used.

    Zillow's historical XML Web Services API required a ZWSID and has been
    retired for general public use, so there is no unauthenticated official
    property endpoint to call here.
    """

    diagnostics.api_checked.append(
        {
            "name": "Zillow legacy XML Web Services",
            "endpoint": "https://api.zillow.com/webservice/GetUpdatedPropertyDetails.htm",
            "usable": False,
            "reason": "Deprecated/retired and requires unavailable ZWSID credentials.",
        }
    )


def try_graphql_property_lookup(
    session: requests.Session,
    zpid: str,
    config: FetchConfig,
    diagnostics: Diagnostics,
) -> dict[str, Any] | None:
    """Attempt Zillow's unofficial GraphQL property endpoint.

    Zillow often uses persisted GraphQL queries tied to its web app release, so
    this ad-hoc query is best-effort. Any failure is treated as a signal to use
    the public property page fallback.
    """

    query = """
    query PropertyDetailByZpid($zpid: ID!) {
      property(zpid: $zpid) {
        zpid
        homeStatus
        homeStatusForHDP
        price
        unformattedPrice
        description
        hdpUrl
        address {
          streetAddress
          city
          state
          zipcode
        }
        attributionInfo {
          agentName
          agentPhoneNumber
          brokerName
          brokerPhoneNumber
          trueStatus
        }
        listingAgents {
          memberFullName
          agentEmail
          agentPhoneNumber
        }
        responsivePhotos {
          caption
          url
        }
      }
    }
    """
    payload = {
        "operationName": "PropertyDetailByZpid",
        "query": query,
        "variables": {"zpid": zpid},
    }

    try:
        response = request_with_retries(
            session,
            "POST",
            GRAPHQL_URL,
            config,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": ZILLOW_BASE_URL,
                "Referer": f"{ZILLOW_BASE_URL}/homedetails/{zpid}_zpid/",
            },
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics should capture all failures.
        diagnostics.api_checked.append(
            {
                "name": "Zillow GraphQL",
                "endpoint": GRAPHQL_URL,
                "usable": False,
                "reason": f"Request failed: {exc}",
            }
        )
        return None

    if response.status_code != 200:
        diagnostics.api_checked.append(
            {
                "name": "Zillow GraphQL",
                "endpoint": GRAPHQL_URL,
                "usable": False,
                "status_code": response.status_code,
                "reason": "Non-200 response; falling back to property page scraping.",
            }
        )
        return None

    try:
        body = response.json()
    except ValueError:
        diagnostics.api_checked.append(
            {
                "name": "Zillow GraphQL",
                "endpoint": GRAPHQL_URL,
                "usable": False,
                "reason": "Response was not valid JSON.",
            }
        )
        return None

    if body.get("errors"):
        diagnostics.api_checked.append(
            {
                "name": "Zillow GraphQL",
                "endpoint": GRAPHQL_URL,
                "usable": False,
                "reason": "GraphQL returned errors.",
                "errors": body.get("errors"),
            }
        )
        return None

    property_data = nested_get(body, ["data", "property"])
    if not isinstance(property_data, Mapping):
        diagnostics.api_checked.append(
            {
                "name": "Zillow GraphQL",
                "endpoint": GRAPHQL_URL,
                "usable": False,
                "reason": "No property object returned.",
            }
        )
        return None

    diagnostics.api_checked.append(
        {
            "name": "Zillow GraphQL",
            "endpoint": GRAPHQL_URL,
            "usable": True,
            "reason": "Returned a property object for the requested ZPID.",
        }
    )
    return dict(property_data)


def fetch_property_page(
    session: requests.Session,
    url: str,
    config: FetchConfig,
    diagnostics: Diagnostics,
) -> str | None:
    """Fetch the public Zillow property page for embedded-data parsing."""

    try:
        response = request_with_retries(session, "GET", url, config)
    except RateLimitedError as exc:
        diagnostics.warnings.append(str(exc))
        return None
    except requests.RequestException as exc:
        diagnostics.warnings.append(f"Failed to fetch property page: {exc}")
        return None

    if response.status_code in {401, 403}:
        diagnostics.warnings.append(
            f"Zillow denied property-page access with HTTP {response.status_code}."
        )
        return None
    if response.status_code >= 400:
        diagnostics.warnings.append(
            f"Property-page fetch returned HTTP {response.status_code}."
        )
        return None

    return response.text


def extract_embedded_json(html: str, zpid: str, diagnostics: Diagnostics) -> list[Any]:
    """Extract likely JSON payloads from Zillow's HTML.

    Zillow changes its client-side state names over time. This function handles
    common sources: Next.js data, Apollo preloaded data, JSON-LD, and script
    blobs containing escaped JSON with the requested ZPID.
    """

    soup = BeautifulSoup(html, "html.parser")
    payloads: list[Any] = []

    next_data = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if next_data and next_data.string:
        parsed = parse_json_text(next_data.string)
        if parsed is not None:
            payloads.append(parsed)

    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text(strip=True)
        parsed = parse_json_text(text)
        if parsed is not None:
            payloads.append(parsed)

    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text or zpid not in text:
            continue

        # Zillow has used hdpApolloPreloadedData and similar global variables.
        for variable in (
            "hdpApolloPreloadedData",
            "__APOLLO_STATE__",
            "__NEXT_DATA__",
        ):
            for match in re.finditer(rf"{re.escape(variable)}\s*=\s*({{.*?}})\s*;", text, re.DOTALL):
                parsed = parse_json_text(match.group(1))
                if parsed is not None:
                    payloads.append(parsed)

        # Fallback: find balanced JSON-like snippets around the ZPID.
        snippet = extract_balanced_object_around(text, zpid)
        parsed = parse_json_text(snippet) if snippet else None
        if parsed is not None:
            payloads.append(parsed)

    if not payloads:
        diagnostics.warnings.append("No embedded Zillow JSON payloads were found in the page.")

    return payloads


def parse_json_text(text: str | None) -> Any | None:
    """Parse JSON text after unescaping common HTML/script encodings."""

    if not text:
        return None
    candidates = [text.strip()]
    candidates.append(text.replace(r"\"", '"').replace(r"\/", "/").strip())

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def extract_balanced_object_around(text: str, needle: str) -> str | None:
    """Return the smallest surrounding JSON object containing `needle`.

    This is a last-resort parser for script blobs. It tracks braces while
    respecting string literals, which is safer than a greedy regex.
    """

    index = text.find(needle)
    if index == -1:
        return None

    start = text.rfind("{", 0, index)
    while start != -1:
        candidate = balanced_object_from(text, start)
        if candidate and needle in candidate:
            return candidate
        start = text.rfind("{", 0, start)
    return None


def balanced_object_from(text: str, start: int) -> str | None:
    """Extract a balanced `{...}` object from `text[start:]`."""

    depth = 0
    in_string = False
    escape = False
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : pos + 1]
    return None


def normalize_from_graphql(data: Mapping[str, Any], zpid: str) -> dict[str, Any]:
    """Normalize GraphQL property data to the requested output shape."""

    return {
        "zpid": zpid,
        "address": format_address(data.get("address")),
        "price": first_present(data, ["unformattedPrice", "price"]),
        "status": first_present(data, ["homeStatus", "homeStatusForHDP"]),
        "description": clean_text(data.get("description")),
        "photos_count": count_sequence(data.get("responsivePhotos")),
        "owner_info": extract_owner_info(data),
        "listing_agent_info": extract_listing_agent_info(data),
    }


def normalize_from_scraped_payloads(
    payloads: Sequence[Any],
    html: str,
    zpid: str,
    address: str,
) -> dict[str, Any]:
    """Normalize scraped JSON and page metadata to the requested output shape."""

    soup = BeautifulSoup(html, "html.parser")
    matching_objects = find_property_objects(payloads, zpid)
    all_roots: list[Any] = list(matching_objects) + list(payloads)

    price = first_key_value(all_roots, ("unformattedPrice", "price", "zestimate"))
    status = first_key_value(
        all_roots,
        (
            "homeStatus",
            "homeStatusForHDP",
            "statusText",
            "listingStatus",
            "trueStatus",
        ),
    )
    description = first_key_value(
        all_roots,
        ("description", "homeDescription", "postingDescription"),
    )
    photos_count = first_photo_count(all_roots)

    return {
        "zpid": zpid,
        "address": first_key_value(all_roots, ("streetAddress", "address")) or address,
        "price": price or parse_price_from_html(soup),
        "status": status or parse_status_from_html(soup),
        "description": clean_text(description) or parse_description_from_html(soup),
        "photos_count": photos_count if photos_count is not None else count_image_urls(payloads),
        "owner_info": first_owner_info(all_roots),
        "listing_agent_info": first_listing_agent_info(all_roots),
    }


def find_property_objects(payloads: Iterable[Any], zpid: str) -> list[Mapping[str, Any]]:
    """Find dictionaries that appear to represent the requested property."""

    matches: list[Mapping[str, Any]] = []
    for item in walk_json(payloads):
        if not isinstance(item, Mapping):
            continue
        item_zpid = item.get("zpid") or item.get("propertyId")
        if str(item_zpid) == str(zpid):
            matches.append(item)
    return matches


def walk_json(value: Any) -> Iterator[Any]:
    """Yield every nested JSON value in depth-first order."""

    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def nested_get(mapping: Mapping[str, Any], path: Sequence[str]) -> Any:
    """Safely read a nested dictionary path."""

    value: Any = mapping
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def first_present(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    """Return the first non-empty direct value from a mapping."""

    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def first_key_value(roots: Iterable[Any], keys: Sequence[str]) -> Any:
    """Return the first non-empty value for any matching key in nested JSON."""

    for root in roots:
        for item in walk_json(root):
            if not isinstance(item, Mapping):
                continue
            for key in keys:
                value = item.get(key)
                if value not in (None, "", [], {}):
                    return value
    return None


def first_photo_count(roots: Iterable[Any]) -> int | None:
    """Find an explicit photo count or infer it from known photo arrays."""

    explicit = first_key_value(roots, ("photoCount", "photosCount", "responsivePhotosCount"))
    if isinstance(explicit, int):
        return explicit
    if isinstance(explicit, str) and explicit.isdigit():
        return int(explicit)

    for root in roots:
        for item in walk_json(root):
            if not isinstance(item, Mapping):
                continue
            for key in ("responsivePhotos", "originalPhotos", "photos", "hugePhotos"):
                count = count_sequence(item.get(key))
                if count:
                    return count
    return None


def count_sequence(value: Any) -> int | None:
    """Count list-like photo values without treating strings as sequences."""

    if isinstance(value, list):
        return len(value)
    return None


def count_image_urls(payloads: Iterable[Any]) -> int:
    """Count unique Zillow image URLs found in embedded JSON as a fallback."""

    urls: set[str] = set()
    image_pattern = re.compile(r"https?://[^\"'\\\s]+?\.(?:jpg|jpeg|png|webp)", re.IGNORECASE)
    for payload in payloads:
        for match in image_pattern.findall(json.dumps(payload, ensure_ascii=False)):
            urls.add(match)
    return len(urls)


def extract_owner_info(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract publicly exposed owner fields if Zillow includes them."""

    return compact_dict(
        {
            "owner_name": first_present(data, ["ownerName", "owner"]),
            "owner_phone": first_present(data, ["ownerPhone", "ownerPhoneNumber"]),
        }
    )


def first_owner_info(roots: Iterable[Any]) -> dict[str, Any] | None:
    """Find owner information in nested scraped payloads, if publicly present."""

    info = compact_dict(
        {
            "owner_name": first_key_value(roots, ("ownerName", "owner")),
            "owner_phone": first_key_value(roots, ("ownerPhone", "ownerPhoneNumber")),
        }
    )
    return info


def extract_listing_agent_info(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract agent/broker fields from a GraphQL property object."""

    attribution = data.get("attributionInfo")
    if not isinstance(attribution, Mapping):
        attribution = {}
    agents = data.get("listingAgents")
    primary_agent = agents[0] if isinstance(agents, list) and agents else {}
    if not isinstance(primary_agent, Mapping):
        primary_agent = {}

    return compact_dict(
        {
            "agent_name": first_present(
                {**dict(attribution), **dict(primary_agent)},
                ("agentName", "memberFullName", "agent_name"),
            ),
            "agent_phone": first_present(
                {**dict(attribution), **dict(primary_agent)},
                ("agentPhoneNumber", "agentPhone", "agent_phone"),
            ),
            "agent_email": first_present(primary_agent, ("agentEmail", "email")),
            "broker_name": first_present(attribution, ("brokerName", "broker_name")),
            "broker_phone": first_present(attribution, ("brokerPhoneNumber", "brokerPhone")),
        }
    )


def first_listing_agent_info(roots: Iterable[Any]) -> dict[str, Any] | None:
    """Find listing-agent information in nested scraped payloads."""

    return compact_dict(
        {
            "agent_name": first_key_value(
                roots,
                ("agentName", "memberFullName", "listingAgentName", "agent_name"),
            ),
            "agent_phone": first_key_value(
                roots,
                ("agentPhoneNumber", "agentPhone", "listingAgentPhone", "phoneNumber"),
            ),
            "agent_email": first_key_value(roots, ("agentEmail", "agentEmailAddress", "email")),
            "broker_name": first_key_value(roots, ("brokerName", "brokerageName")),
            "broker_phone": first_key_value(roots, ("brokerPhoneNumber", "brokerPhone")),
        }
    )


def compact_dict(values: Mapping[str, Any]) -> dict[str, Any] | None:
    """Remove empty values and return None if nothing remains."""

    compacted = {
        key: clean_text(value) if isinstance(value, str) else value
        for key, value in values.items()
        if value not in (None, "", [], {})
    }
    return compacted or None


def format_address(value: Any) -> str | None:
    """Format Zillow's structured address object into one line."""

    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return None
    parts = [
        value.get("streetAddress"),
        value.get("city"),
        value.get("state"),
        value.get("zipcode"),
    ]
    return clean_text(", ".join(str(part) for part in parts if part))


def clean_text(value: Any) -> str | None:
    """Normalize whitespace for text fields."""

    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def parse_price_from_html(soup: BeautifulSoup) -> str | None:
    """Extract a price-looking value from visible page metadata."""

    selectors = [
        {"property": "product:price:amount"},
        {"name": "price"},
        {"property": "og:title"},
        {"name": "twitter:title"},
    ]
    for attrs in selectors:
        tag = soup.find("meta", attrs=attrs)
        content = tag.get("content") if tag else None
        if not content:
            continue
        match = re.search(r"\$[\d,]+(?:\.\d+)?", content)
        if match:
            return match.group(0)
        if attrs.get("property") == "product:price:amount":
            return content
    return None


def parse_status_from_html(soup: BeautifulSoup) -> str | None:
    """Infer listing status from common title/meta snippets."""

    text_parts = [
        soup.title.string if soup.title and soup.title.string else "",
        *(tag.get("content", "") for tag in soup.find_all("meta")),
    ]
    haystack = " ".join(text_parts).lower()
    for status in ("for sale", "for rent", "sold", "pending", "off market", "contingent"):
        if status in haystack:
            return status.title()
    return None


def parse_description_from_html(soup: BeautifulSoup) -> str | None:
    """Extract description from standard SEO metadata."""

    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        content = tag.get("content") if tag else None
        cleaned = clean_text(content)
        if cleaned:
            return cleaned
    return None


def build_output(
    source: str,
    url: str,
    property_data: Mapping[str, Any],
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    """Build the final JSON document."""

    return {
        "source": source,
        "fetched_at": datetime.now(UTC).isoformat(),
        "url": url,
        "property": property_data,
        "diagnostics": {
            "api_checked": diagnostics.api_checked,
            "warnings": diagnostics.warnings,
        },
    }


def fetch_property(zpid: str, address: str, config: FetchConfig) -> dict[str, Any]:
    """Fetch property details using API-first, scraper-second strategy."""

    diagnostics = Diagnostics()
    session = build_session()
    property_url = build_property_url(zpid, address)

    check_legacy_official_api(diagnostics)

    graphql_data = try_graphql_property_lookup(session, zpid, config, diagnostics)
    if graphql_data:
        normalized = normalize_from_graphql(graphql_data, zpid)
        return build_output("zillow_graphql", property_url, normalized, diagnostics)

    sleep_between_requests(config)
    html = fetch_property_page(session, property_url, config, diagnostics)
    if not html:
        diagnostics.warnings.append("No scraper data available because the page could not be fetched.")
        return build_output(
            "unavailable",
            property_url,
            {
                "zpid": zpid,
                "address": address,
                "price": None,
                "status": None,
                "description": None,
                "photos_count": None,
                "owner_info": None,
                "listing_agent_info": None,
            },
            diagnostics,
        )

    payloads = extract_embedded_json(html, zpid, diagnostics)
    normalized = normalize_from_scraped_payloads(payloads, html, zpid, address)
    return build_output("zillow_property_page", property_url, normalized, diagnostics)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse CLI options."""

    parser = argparse.ArgumentParser(
        description="Fetch selected Zillow property details and output JSON."
    )
    parser.add_argument("--zpid", default=DEFAULT_ZPID, help="Zillow property ID.")
    parser.add_argument("--address", default=DEFAULT_ADDRESS, help="Property address.")
    parser.add_argument(
        "--output",
        help="Optional path to write JSON. Defaults to stdout.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("ZILLOW_FETCH_TIMEOUT", "20")),
        help="Request timeout in seconds.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=int(os.getenv("ZILLOW_FETCH_MAX_RETRIES", "3")),
        help="Retries for transient errors and HTTP 429.",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=float(os.getenv("ZILLOW_FETCH_MIN_DELAY", "2")),
        help="Minimum randomized delay between requests.",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=float(os.getenv("ZILLOW_FETCH_MAX_DELAY", "5")),
        help="Maximum randomized delay between requests.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""

    args = parse_args(argv or sys.argv[1:])
    config = FetchConfig(
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
        min_delay_seconds=args.min_delay,
        max_delay_seconds=args.max_delay,
    )

    result = fetch_property(args.zpid, args.address, config)
    output = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output)
            handle.write("\n")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
