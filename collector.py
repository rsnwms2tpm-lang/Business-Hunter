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
    "User-Agent": "BusinessHunter/2.3 (+personal research; low-frequency collector)",
    "Accept-Language": "en-GB,en;q=0.9",
}
TIMEOUT = 25
DETAIL_LIMIT = 60
DETAIL_TEXT_LIMIT = 3200
MAX_ASKING_PRICE = 250_000

SOURCES = [
    {"name": "Rightbiz", "url": "https://www.rightbiz.co.uk/businesses-for-sale-in-cornwall.html", "parser": "rightbiz"},
    {"name": "BusinessesForSale", "url": "https://uk.businessesforsale.com/uk/search/businesses-for-sale-in-cornwall", "parser": "bfs"},
    {"name": "Daltons", "url": "https://www.daltonsbusiness.com/listing-businesses-for-sale-in-cornwall/", "parser": "daltons"},
    {"name": "Intelligent", "url": "https://www.intelligent.co.uk/businesses-for-sale/cornwall-businesses-for-sale", "parser": "intelligent"},
]


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def stable_id(source, url):
    return f"{source.lower().replace(' ', '-')}-{hashlib.sha1(url.encode()).hexdigest()[:14]}"


def amount_value(raw):
    if not raw:
        return None
    m = re.search(r"([\d,.]+)\s*([KkMm]?)", clean(raw).replace("£", ""))
    if not m:
        return None
    try:
        x = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if m.group(2).lower() == "k":
        x *= 1_000
    elif m.group(2).lower() == "m":
        x *= 1_000_000
    return int(x)


def extract_money(text, labels):
    t = clean(text)
    for label in labels:
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
        exact = re.search(
            rf"{label}\s*:?\s*(?:c\.?|circa|over|>|from|offers over|offers around|o\/a|oir[o]?)?\s*£\s*([\d,.]+)\s*([KkMm]?)",
            t,
            re.I,
        )
        if exact:
            value = amount_value(exact.group(1) + exact.group(2))
            if value is not None:
                return value, clean(exact.group(0)), False
    return None, "", False


def extract_asking_price(text):
    """Only accept an explicit sale-price label. Never infer price from bare Freehold/Leasehold text."""
    groups = [
        ("High", [r"Asking Price", r"Business Asking Price", r"Guide Price", r"Sale Price", r"Offers? Around", r"Offers? Over", r"OIRO", r"O\/A"]),
        ("Medium", [r"Freehold Price", r"Leasehold Price"]),
        ("Low", [r"Price"]),
    ]
    for confidence, labels in groups:
        value, raw, ranged = extract_money(text, labels)
        if value is not None:
            # Very small 'leasehold price' figures are frequently annual rent/premium artefacts.
            if confidence == "Medium" and value < 10_000:
                continue
            return value, raw, ranged, confidence
    return None, "", False, "Unknown"


def infer_profit(text):
    measures = [
        ("Adjusted EBITDA", [r"Indicative Adjusted EBITDA", r"Adjusted EBITDA"]),
        ("EBITDA", [r"EBITDA"]),
        ("Reconstituted profit", [r"Reconstituted (?:Net )?Profit"]),
        ("Adjusted net profit", [r"Adjusted Net Profit"]),
        ("Annual net profit", [r"Annual Net Profit", r"Net Profit"]),
        ("Seller-stated profit", [r"Profit"]),
    ]
    for measure, labels in measures:
        v, raw, ranged = extract_money(text, labels)
        if v is not None:
            return v, raw, ranged, measure
    return None, "", False, "Unknown"


def valid(url, kind):
    p = urlparse(url)
    path = p.path.lower().rstrip("/")
    if kind == "rightbiz":
        return p.netloc.endswith("rightbiz.co.uk") and bool(re.search(r"/buy_business/for_sale/\d+_[^/]+\.html$", p.path, re.I))
    if kind == "bfs":
        return p.netloc.endswith("businessesforsale.com") and path.endswith(".aspx") and "/search/" not in path
    if kind == "daltons":
        return p.netloc.endswith("daltonsbusiness.com") and "/listing/" in path
    return p.netloc.endswith("intelligent.co.uk") and "/businesses-for-sale/" in path and not path.endswith("/businesses-for-sale") and not path.endswith("/cornwall-businesses-for-sale")


def block_for_anchor(a, levels=6):
    node = a
    candidates = []
    for _ in range(levels):
        if not node.parent:
            break
        node = node.parent
        text = clean(node.get_text(" ", strip=True))
        if 70 <= len(text) <= 2400:
            candidates.append((len(text), node))
        if len(text) > 4000:
            break
    return sorted(candidates, key=lambda z: z[0])[0][1] if candidates else (a.parent or a)


def title_from(a, block):
    anchor = clean(a.get_text(" ", strip=True))
    if 8 <= len(anchor) <= 110 and anchor.lower() not in {"details", "contact seller", "read more"}:
        return anchor
    for h in block.find_all(["h1", "h2", "h3", "h4"]):
        candidate = clean(h.get_text(" ", strip=True))
        if 8 <= len(candidate) <= 140:
            return candidate
    return anchor[:110] if len(anchor) >= 8 else ""


def listing_from_block(source, url, title, text, location="Cornwall"):
    price, price_raw, price_range, price_conf = extract_asking_price(text)
    turnover, turnover_raw, turnover_range = extract_money(text, [r"Annual Turnover", r"Turnover", r"Revenue"])
    profit, profit_raw, profit_range, profit_measure = infer_profit(text)
    if not any(v is not None for v in (price, turnover, profit)):
        return None
    return {
        "id": stable_id(source, url),
        "name": title[:140],
        "location": location or "Cornwall",
        "type": "",
        "status": "New",
        "price": price or 0,
        "priceDisplay": price_raw,
        "priceIsRange": price_range,
        "priceConfidence": price_conf,
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
        "risks": "Automated screening data. Verify seller figures, owner role, staffing, tenure and accounts before relying on it.",
        "notes": f"Automatically collected from {source}; individual advert URL retained.",
        "description": "",
        "sellerSignals": [],
    }


def parse_cards(html, base, source, kind, require_cornwall=False):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"])
        if not valid(url, kind) or url in seen:
            continue
        seen.add(url)
        block = block_for_anchor(a)
        text = clean(block.get_text(" ", strip=True))
        low = text.lower()
        if require_cornwall and "cornwall" not in low:
            continue
        if "nationwide" in low and "cornwall" not in low:
            continue
        title = title_from(a, block)
        if not title:
            continue
        lm = re.search(r"([A-Za-z][A-Za-z '\-]{2,50},\s*Cornwall(?:,\s*England)?)", text, re.I)
        loc = clean(lm.group(1)) if lm else "Cornwall"
        item = listing_from_block(source, url, title, text, loc)
        if item:
            out.append(item)
    return out[:100]


def extract_description(soup):
    headings = soup.find_all(["h2", "h3", "h4"])
    ordered = [h for h in headings if clean(h.get_text(" ", strip=True)).lower() in {"description", "business description", "the business"}]
    ordered += [h for h in headings if clean(h.get_text(" ", strip=True)).lower() == "overview"]
    for h in ordered:
        chunks = []
        for sib in h.find_all_next(limit=20):
            if sib is h:
                continue
            if sib.name in {"h1", "h2", "h3", "h4"} and chunks:
                break
            if sib.name in {"p", "li"}:
                s = clean(sib.get_text(" ", strip=True))
                if len(s) >= 25 and s not in chunks:
                    chunks.append(s)
            if sum(len(x) for x in chunks) >= DETAIL_TEXT_LIMIT:
                break
        if chunks:
            return clean(" ".join(chunks))[:DETAIL_TEXT_LIMIT]
    meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    return clean(meta.get("content"))[:DETAIL_TEXT_LIMIT] if meta and meta.get("content") else ""


def detail_lead_text(soup, title):
    main = soup.find("main") or soup.body or soup
    text = clean(main.get_text(" ", strip=True))
    if title:
        pos = text.lower().find(clean(title).lower()[:50])
        if pos >= 0:
            text = text[pos:]
    low = text.lower()
    cuts = [low.find(m) for m in ["recently viewed", "featured listings", "similar businesses", "related businesses", "other businesses"] if low.find(m) >= 0]
    if cuts:
        text = text[:min(cuts)]
    return text[:6500]


def is_franchise_text(text):
    low = text.lower()
    return any(w in low for w in [
        "franchise opportunity", "franchise resale", "franchise territory", "franchise fee",
        "become a franchisee", "franchise package", "franchise investment", "franchise available",
    ])


def is_property_only(item):
    t = clean(f"{item.get('name','')} {item.get('description','')}").lower()
    return any(w in t for w in ["development site", "residential development", "investment property", "property investment opportunity"]) and not any(w in t for w in ["turnover", "profit", "trading business", "going concern"])


def enrich_item(session, item):
    try:
        r = session.get(item["url"], headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        desc = extract_description(soup)
        lead = detail_lead_text(soup, item.get("name", ""))
        evidence = clean(desc + " " + lead)
        item["description"] = desc

        price, raw, ranged, confidence = extract_asking_price(evidence)
        if price is not None:
            item["price"] = price
            item["priceDisplay"] = raw
            item["priceIsRange"] = ranged
            item["priceConfidence"] = confidence
        else:
            # Do not trust a card-level price if the detail advert cannot confirm it.
            item["price"] = 0
            item["priceDisplay"] = "Price not confidently identified"
            item["priceIsRange"] = False
            item["priceConfidence"] = "Unknown"

        turnover, raw, ranged = extract_money(evidence, [r"Annual Turnover", r"Turnover", r"Revenue"])
        if turnover is not None:
            item["turnover"], item["turnoverDisplay"], item["turnoverIsRange"] = turnover, raw, ranged
        profit, raw, ranged, measure = infer_profit(evidence)
        if profit is not None:
            item["profit"], item["profitDisplay"], item["profitIsRange"], item["profitMeasure"] = profit, raw, ranged, measure

        low = evidence.lower()
        flags = []
        if any(w in low for w in ["owner run", "owner-run", "owner operated", "owner-operated", "hands-on operator"]):
            flags.append("Seller indicates material owner involvement.")
        if any(w in low for w in ["retirement sale", "retiring", "pending retirement"]):
            flags.append("Seller states retirement as a reason for sale.")
        if "stock at valuation" in low:
            flags.append("Stock is stated to be additional / at valuation.")
        if any(w in low for w in ["relocatable", "home based", "home-based"]):
            flags.append("Advert indicates a flexible or relocatable operating model.")
        if any(w in low for w in ["repeat customer", "repeat business", "recurring income", "annual subscription"]):
            flags.append("Advert mentions repeat or recurring customer revenue.")

        item["isFranchise"] = is_franchise_text(evidence)
        if item["isFranchise"]:
            flags.append("Franchise / territory opportunity rather than a standalone acquisition.")
        item["sellerSignals"] = flags[:6]
        if flags:
            item["notes"] += " " + " ".join(flags)
        return item
    except Exception as e:
        item["notes"] += f" Detail enrichment unavailable ({type(e).__name__})."
        item["priceConfidence"] = "Unknown"
        item["price"] = 0
        item["priceDisplay"] = "Price not confidently identified"
        return item


def dedupe(items):
    by_url = {x["url"].rstrip("/"): x for x in items}
    by_title = {}
    for x in by_url.values():
        key = re.sub(r"[^a-z0-9]", "", x["name"].lower())[:100]
        richness = sum(bool(x.get(k)) for k in ("price", "turnover", "profit", "description"))
        if key not in by_title or richness > sum(bool(by_title[key].get(k)) for k in ("price", "turnover", "profit", "description")):
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
    for src in SOURCES:
        try:
            r = session.get(src["url"], headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            found = parse_cards(r.text, src["url"], src["name"], src["parser"], src["parser"] == "intelligent")
            all_items.extend(found)
            print(f"{src['name']}: found {len(found)} listing candidates")
            time.sleep(0.7)
        except Exception as e:
            errors.append(f"{src['name']}: {type(e).__name__}: {e}")

    items = dedupe(all_items)
    if not items:
        previous = load_previous()
        if previous:
            items = previous
            errors.append("All live collectors returned no usable listings; retained previous feed.")

    enriched = []
    for i, item in enumerate(items):
        enriched.append(enrich_item(session, item) if i < DETAIL_LIMIT else item)
        if i < DETAIL_LIMIT:
            time.sleep(0.25)

    candidates = dedupe(enriched)
    excluded = {"franchise": 0, "over_cap": 0, "price_unknown": 0, "property_only": 0}
    kept = []
    for x in candidates:
        if x.get("isFranchise"):
            excluded["franchise"] += 1
            continue
        if is_property_only(x):
            excluded["property_only"] += 1
            continue
        price = int(x.get("price") or 0)
        if not price or x.get("priceConfidence") == "Unknown":
            excluded["price_unknown"] += 1
            continue
        if price > MAX_ASKING_PRICE:
            excluded["over_cap"] += 1
            continue
        x["acquisitionBand"] = "Core" if price <= 200_000 else "Stretch"
        kept.append(x)

    kept.sort(key=lambda x: (x.get("acquisitionBand") == "Stretch", -x.get("profit", 0), x.get("price", 0)))
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "region": "Cornwall",
        "count": len(kept),
        "sources": [x["name"] for x in SOURCES],
        "errors": errors,
        "collector_version": 2.3,
        "acquisition_policy": {
            "main_max": 200_000,
            "hard_max": MAX_ASKING_PRICE,
            "franchises_excluded": True,
            "unknown_prices_excluded": True,
            "property_only_excluded": True,
            "reserve_policy": "Keep 6-12 months personal living costs in reserve; assess usable cash, working capital and finance/vendor terms before affordability.",
        },
        "excluded": excluded,
        "listings": kept,
    }
    with open("market.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(kept)} acquisition listings; excluded={excluded}; errors={errors}")
    return 0 if kept else 1


if __name__ == "__main__":
    sys.exit(main())