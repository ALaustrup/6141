# 6141 Sweeney Road Zillow Fetcher

Small Python utility for fetching selected public Zillow property details for:

- Address: `6141 Sweeney Road, Somerset, CA 95684`
- ZPID: `18608823`

The script first records that Zillow's legacy official XML API is retired, then
tries Zillow's web GraphQL endpoint. If GraphQL is unavailable or rejected, it
falls back to parsing the public Zillow property page with `requests` and
`BeautifulSoup`.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

```bash
python fetch_zillow_property.py
```

Write JSON to a file:

```bash
python fetch_zillow_property.py --output property.json
```

Override the target:

```bash
python fetch_zillow_property.py \
  --zpid 18608823 \
  --address "6141 Sweeney Road, Somerset, CA 95684"
```

The JSON output includes `price`, `status`, `description`, `photos_count`,
publicly exposed owner fields if present, listing agent/broker fields if
present, and diagnostics about which access path succeeded.
