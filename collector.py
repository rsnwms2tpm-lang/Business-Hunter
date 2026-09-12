import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "BusinessHunter/2.0 (+personal research; low-frequency collector)",
    "Accept-Language": "en-GB,en;q=0.9",
}
TIMEOUT = 25
DETAIL_LIMIT = 60
DETAIL_TEXT_LIMIT = 3200

SOURCES = [
    {
        "name": "Rightbiz",
        "url": "https://www.rightbiz.co.uk/businesses-for-sale-in-cornwall.html",
        "parser": "rightbiz",
    },
    {
        "name": "BusinessesForSale",
        "url": "https://uk.businessesforsale.com/uk/search/businesses-for-sale-in-cornwall",
        "parser": "bfs",
    },
    {
        "name": "Daltons",
        "url": "https://www.daltonsbusiness.com/listing-businesses-for-sale-in-cornwall/",
        "parser": "daltons",
    },
    {
        "name": "Intelligent",
        "url": "https://www.intelligent.co.uk/businesses-for-sale/cornwall-businesses-for-sale",
        "parser": "intelligent",
    },
]


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def stable_id(source, url):
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:14]
    return f"{source.lower().replace(' ', '-')}-{digest}"


def amount_value(raw):
    if not raw:
        return None
    s = clean(raw).replace("£", "")
    m = re.search(r"([\d,.]+)\s*([KkMm]?)", s)
    if not m:
        return None
    try:
        x = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = m.group(2).lower()
    if suffix == "k":
        x *= 1_000
    elif suffix == "m":
        x *= 1_000_000
    return int(x)


def extract_money(text, labels):
    """Return (representative numeric value, seller display text, is_range)."""
    t = clean(text)
    for label in labels:
        # Exact figure, including >£320k and circa £90k.
        exact = re.search(
            rf"{label}\s*:?\s*(?:c\.?|circa|over|>|from)?\s*£\s*([\d,.]+)\s*([KkMm]?)",
            t,
            re.I,
        )
        # Range such as £200k - £500k.
        ranged = re.search(
            rf"{label}\s*:?\s*£?\s*([\d,.]+)\s*([KkMm]?)\s*(?:-|–|to)\s*£?\s*([\d,.]+)\s*([KkMm]?)",
            t,
            re.I,
        )
        if ranged:
            lo = amount_value(ranged.group(1) + ranged.group(2))
            hi = amount_value(ranged.group(3) + ranged.group(4))
            if lo is not None and hi is not None:
                return int((lo + hi) / 2), clean(ranged.group(0)), True
        if exact:
            value = amount_value(exact.group(1) + exact.group(2))
            if value is not None:
                return value, clean(exact.group(0)), False
    return None, "", False


def infer_profit(text):
    measures = [
        ("EBITDA", [r"EBITDA"]),
        ("Adjusted EBITDA", [r"Adjusted EBITDA", r"Indicative Adjusted EBITDA"]),
        ("Reconstituted profit", [r"Reconstituted (?:Net )?Profit"]),
        ("Annual net profit", [r"Annual Net Profit", r"Net Profit"]),
        ("Adjusted net profit", [r"Adjusted Net Profit"]),
        ("Seller-stated profit", [r"Profit"]),
    ]
    # Prefer more specific measures before generic Profit.
    for measure, labels in measures:
        v, raw, ranged = extract_money(text, labels)
        if v is not None:
            return v, raw, ranged, measure
    return None, "", False, "Unknown"


def is_rightbiz_detail(url):
    p = urlparse(url)
    return p.netloc.endswith("rightbiz.co.uk") and bool(
        re.search(r"/buy_business/for_sale/\d+_[^/]+\.html$", p.path, re.I)
    )


def is_bfs_detail(url):
    p = urlparse(url)
    return (
        p.netloc.endswith("businessesforsale.com")
        and p.path.lower().endswith(".aspx")
        and "/search/" not in p.path.lower()
    )


def is_daltons_detail(url):
    p = urlparse(url)
    return p.netloc.endswith("daltonsbusiness.com") and "/listing/" in p.path.lower()


def is_intelligent_detail(url):
    p = urlparse(url)
    path = p.path.lower().rstrip("/")
    return (
        p.netloc.endswith("intelligent.co.uk")
        and "/businesses-for-sale/" in path
        and not path.endswith("/businesses-for-sale")
        and not path.endswith("/cornwall-businesses-for-sale")
    )


def block_for_anchor(a, levels=5):
    block = a
    for _ in range(levels):
        if block.parent:
            block = block.parent
    return block


def title_from(a, block):
    title = clean(a.get_text(" ", strip=True))
    if len(title) >= 8 and title.lower() not in {"details", "contact seller", "read more"}:
        return title
    headings = block.find_all(["h1", "h2", "h3", "h4"])
    for h in headings:
        candidate = clean(h.get_text(" ", strip=True))
        if len(candidate) >= 8:
            return candidate
    return ""


def listing_from_block(source, url, title, text, location="Cornwall"):
    price, price_raw, price_range = extract_money(
        text, [r"Asking Price", r"Freehold Price", r"Leasehold Price", r"Price", r"Freehold", r"Leasehold"]
    )
    turnover, turnover_raw, turnover_range = extract_money(text, [r"Annual Turnover", r"Turnover", r"Revenue"])
    profit, profit_raw, profit_range, profit_measure = infer_profit(text)
    if not any(v is not None for v in (price, turnover, profit)):
        return None
    return {
        "id": stable_id(source, url),
        "name": title[:180],
        "location": location or "Cornwall",
        "type": "",
        "status": "New",
        "price": price or 0,
        "priceDisplay": price_raw,
        "priceIsRange": price_range,
        "turnover": turnover or 0,
        "turnoverDisplay": turnover_raw,
        "turnoverIsRange": turnover_range,
        "profit": profit or 0,
        "profitDisplay": profit_raw,
        "profitIsRange": profit_range,
        "profitMeasure": profit_measure,
        "rent": 0,
        "employees": 0,
        "ownerDays": 0,
        "source": source,
        "url": url,
        "growth": "Review the full seller description for evidenced growth opportunities.",
        "risks": "Automated screening data. Verify the seller figures, owner role, staffing, tenure and accounts before relying on it.",
        "notes": f"Automatically collected from {source}; individual advert URL retained.",
        "description": "",
    }


def parse_rightbiz(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"])
        if not is_rightbiz_detail(url) or url in seen:
            continue
        seen.add(url)
        block = block_for_anchor(a, 5)
        text = clean(block.get_text(" ", strip=True))
        title = title_from(a, block)
        if not title:
            continue
        loc = "Cornwall"
        lm = re.search(r"([A-Za-z][A-Za-z '\-]{2,50},?\s+Cornwall)", text, re.I)
        if lm:
            loc = clean(lm.group(1))
        item = listing_from_block("Rightbiz", url, title, text, loc)
        if item:
            out.append(item)
    return out[:100]


def parse_bfs(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"])
        if not is_bfs_detail(url) or url in seen:
            continue
        seen.add(url)
        block = block_for_anchor(a, 4)
        text = clean(block.get_text(" ", strip=True))
        title = title_from(a, block)
        if not title:
            continue
        item = listing_from_block("BusinessesForSale", url, title, text, "Cornwall")
        if item:
            out.append(item)
    return out[:100]


def parse_daltons(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"])
        if not is_daltons_detail(url) or url in seen:
            continue
        seen.add(url)
        block = block_for_anchor(a, 5)
        text = clean(block.get_text(" ", strip=True))
        if "nationwide" in text.lower() and "cornwall" not in text.lower():
            continue
        title = title_from(a, block)
        if not title:
            continue
        loc = "Cornwall"
        lm = re.search(r"([A-Za-z][A-Za-z '\-]{2,50},\s*Cornwall(?:,\s*England)?)", text, re.I)
        if lm:
            loc = clean(lm.group(1))
        item = listing_from_block("Daltons", url, title, text, loc)
        if item:
            out.append(item)
    return out[:100]


def parse_intelligent(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"])
        if not is_intelligent_detail(url) or url in seen:
            continue
        seen.add(url)
        block = block_for_anchor(a, 4)
        text = clean(block.get_text(" ", strip=True))
        if "cornwall" not in text.lower():
            continue
        title = title_from(a, block)
        if not title:
            continue
        item = listing_from_block("Intelligent", url, title, text, "Cornwall")
        if item:
            out.append(item)
    return out[:100]


def extract_description(soup):
    # Prefer content following an explicit Description/Overview heading.
    chunks = []
    for h in soup.find_all(["h2", "h3", "h4"]):
        label = clean(h.get_text(" ", strip=True)).lower()
        if label in {"description", "overview", "business description", "the business"}:
            for sib in h.find_all_next(limit=8):
                if sib is h:
                    continue
                if sib.name in {"h1", "h2", "h3"} and chunks:
                    break
                if sib.name in {"p", "li", "div"}:
                    s = clean(sib.get_text(" ", strip=True))
                    if len(s) >= 25 and s not in chunks:
                        chunks.append(s)
                if sum(len(x) for x in chunks) >= DETAIL_TEXT_LIMIT:
                    break
            if chunks:
                break
    if not chunks:
        meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
        if meta and meta.get("content"):
            chunks.append(clean(meta["content"]))
    if not chunks:
        main = soup.find("main") or soup.body
        if main:
            chunks.append(clean(main.get_text(" ", strip=True)))
    return clean(" ".join(chunks))[:DETAIL_TEXT_LIMIT]


def enrich_item(session, item):
    try:
        r = session.get(item["url"], headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        body_text = clean((soup.find("main") or soup.body or soup).get_text(" ", strip=True))
        desc = extract_description(soup)
        evidence = clean((desc + " " + body_text))[:12000]
        item["description"] = desc

        price, raw, ranged = extract_money(
            evidence, [r"Asking Price", r"Freehold Price", r"Leasehold Price", r"Price", r"Freehold", r"Leasehold"]
        )
        if price is not None:
            item["price"], item["priceDisplay"], item["priceIsRange"] = price, raw, ranged
        turnover, raw, ranged = extract_money(evidence, [r"Annual Turnover", r"Turnover", r"Revenue"])
        if turnover is not None:
            item["turnover"], item["turnoverDisplay"], item["turnoverIsRange"] = turnover, raw, ranged
        profit, raw, ranged, measure = infer_profit(evidence)
        if profit is not None:
            item["profit"], item["profitDisplay"], item["profitIsRange"] = profit, raw, ranged
            item["profitMeasure"] = measure

        lower = evidence.lower()
        flags = []
        if any(w in lower for w in ["owner run", "owner-run", "owner operated", "owner-operated", "hands-on operator"]):
            flags.append("Seller indicates material owner involvement.")
        if any(w in lower for w in ["retirement sale", "retiring", "pending retirement"]):
            flags.append("Seller states retirement as a reason for sale.")
        if "stock at valuation" in lower:
            flags.append("Stock is stated to be additional / at valuation.")
        if any(w in lower for w in ["relocatable", "home based", "home-based"]):
            flags.append("Advert indicates a flexible or relocatable operating model.")
        if any(w in lower for w in ["repeat customer", "repeat business", "recurring income", "annual subscription"]):
            flags.append("Advert mentions repeat or recurring customer revenue.")
        item["sellerSignals"] = flags[:5]
        if flags:
            item["notes"] += " " + " ".join(flags)
        return item
    except Exception as e:
        item["notes"] += f" Detail enrichment unavailable ({type(e).__name__})."
        return item


def dedupe(items):
    by_url = {}
    for x in items:
        by_url[x["url"].rstrip("/")] = x
    # Conservative title dedupe: only collapse near-identical titles, keeping richer evidence.
    by_title = {}
    for x in by_url.values():
        key = re.sub(r"[^a-z0-9]", "", x["name"].lower())[:100]
        richness = sum(bool(x.get(k)) for k in ("price", "turnover", "profit", "description"))
        if key not in by_title:
            by_title[key] = x
        else:
            old = by_title[key]
            old_richness = sum(bool(old.get(k)) for k in ("price", "turnover", "profit", "description"))
            if richness > old_richness:
                by_title[key] = x
    return list(by_title.values())


def load_previous():
    try:
        with open("market.json", "r", encoding="utf-8") as f:
            return json.load(f).get("listings", [])
    except Exception:
        return []


def main():
    session = requests.Session()
    all_items, errors = [], []
    parsers = {
        "rightbiz": parse_rightbiz,
        "bfs": parse_bfs,
        "daltons": parse_daltons,
        "intelligent": parse_intelligent,
    }

    for src in SOURCES:
        try:
            r = session.get(src["url"], headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            found = parsers[src["parser"]](r.text, src["url"])
            all_items.extend(found)
            print(f"{src['name']}: found {len(found)} listing candidates")
            time.sleep(0.7)
        except Exception as e:
            errors.append(f"{src['name']}: {type(e).__name__}: {e}")

    items = dedupe(all_items)

    # If every live source failed, preserve the existing feed rather than blanking the app.
    if not items:
        previous = load_previous()
        if previous:
            items = previous
            errors.append("All live collectors returned no usable listings; retained previous feed.")

    enriched = []
    for i, item in enumerate(items):
        if i < DETAIL_LIMIT:
            enriched.append(enrich_item(session, item))
            time.sleep(0.25)
        else:
            enriched.append(item)

    enriched = dedupe(enriched)
    enriched.sort(key=lambda x: (bool(x.get("description")), x.get("profit", 0), x.get("turnover", 0)), reverse=True)

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "region": "Cornwall",
        "count": len(enriched),
        "sources": [x["name"] for x in SOURCES],
        "errors": errors,
        "collector_version": 2,
        "listings": enriched,
    }
    with open("market.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(enriched)} listings; errors={errors}")
    return 0 if enriched else 1


if __name__ == "__main__":
    sys.exit(main())
