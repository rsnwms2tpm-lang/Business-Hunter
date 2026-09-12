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
    "User-Agent": "BusinessHunter/2.1 (+personal research; low-frequency collector)",
    "Accept-Language": "en-GB,en;q=0.9",
}
TIMEOUT = 25
DETAIL_LIMIT = 60
DETAIL_TEXT_LIMIT = 3200

SOURCES = [
    {"name": "Rightbiz", "url": "https://www.rightbiz.co.uk/businesses-for-sale-in-cornwall.html", "parser": "rightbiz"},
    {"name": "BusinessesForSale", "url": "https://uk.businessesforsale.com/uk/search/businesses-for-sale-in-cornwall", "parser": "bfs"},
    {"name": "Daltons", "url": "https://www.daltonsbusiness.com/listing-businesses-for-sale-in-cornwall/", "parser": "daltons"},
    {"name": "Intelligent", "url": "https://www.intelligent.co.uk/businesses-for-sale/cornwall-businesses-for-sale", "parser": "intelligent"},
]


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def stable_id(source, url):
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:14]
    return f"{source.lower().replace(' ', '-')}-{digest}"


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
            t, re.I,
        )
        if ranged:
            lo = amount_value(ranged.group(1) + ranged.group(2))
            hi = amount_value(ranged.group(3) + ranged.group(4))
            if lo is not None and hi is not None:
                return int((lo + hi) / 2), clean(ranged.group(0)), True
        exact = re.search(
            rf"{label}\s*:?\s*(?:c\.?|circa|over|>|from)?\s*£\s*([\d,.]+)\s*([KkMm]?)",
            t, re.I,
        )
        if exact:
            value = amount_value(exact.group(1) + exact.group(2))
            if value is not None:
                return value, clean(exact.group(0)), False
    return None, "", False


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


def is_rightbiz_detail(url):
    p = urlparse(url)
    return p.netloc.endswith("rightbiz.co.uk") and bool(re.search(r"/buy_business/for_sale/\d+_[^/]+\.html$", p.path, re.I))


def is_bfs_detail(url):
    p = urlparse(url)
    return p.netloc.endswith("businessesforsale.com") and p.path.lower().endswith(".aspx") and "/search/" not in p.path.lower()


def is_daltons_detail(url):
    p = urlparse(url)
    return p.netloc.endswith("daltonsbusiness.com") and "/listing/" in p.path.lower()


def is_intelligent_detail(url):
    p = urlparse(url)
    path = p.path.lower().rstrip("/")
    return p.netloc.endswith("intelligent.co.uk") and "/businesses-for-sale/" in path and not path.endswith("/businesses-for-sale") and not path.endswith("/cornwall-businesses-for-sale")


def block_for_anchor(a, levels=6):
    """Pick the smallest card-like ancestor, rather than a container holding neighbouring listings."""
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
    if candidates:
        # Smallest useful ancestor is normally the listing card.
        return sorted(candidates, key=lambda z: z[0])[0][1]
    return a.parent or a


def title_from(a, block):
    title = clean(a.get_text(" ", strip=True))
    if len(title) >= 8 and title.lower() not in {"details", "contact seller", "read more"}:
        return title
    for h in block.find_all(["h1", "h2", "h3", "h4"]):
        candidate = clean(h.get_text(" ", strip=True))
        if len(candidate) >= 8:
            return candidate
    return ""


def listing_from_block(source, url, title, text, location="Cornwall"):
    price, price_raw, price_range = extract_money(text, [r"Asking Price", r"Freehold Price", r"Leasehold Price", r"Price", r"Freehold", r"Leasehold"])
    turnover, turnover_raw, turnover_range = extract_money(text, [r"Annual Turnover", r"Turnover", r"Revenue"])
    profit, profit_raw, profit_range, profit_measure = infer_profit(text)
    if not any(v is not None for v in (price, turnover, profit)):
        return None
    return {
        "id": stable_id(source, url), "name": title[:180], "location": location or "Cornwall", "type": "", "status": "New",
        "price": price or 0, "priceDisplay": price_raw, "priceIsRange": price_range,
        "turnover": turnover or 0, "turnoverDisplay": turnover_raw, "turnoverIsRange": turnover_range,
        "profit": profit or 0, "profitDisplay": profit_raw, "profitIsRange": profit_range, "profitMeasure": profit_measure,
        "rent": 0, "employees": 0, "ownerDays": 0, "source": source, "url": url,
        "growth": "Review the full seller description for evidenced growth opportunities.",
        "risks": "Automated screening data. Verify seller figures, owner role, staffing, tenure and accounts before relying on it.",
        "notes": f"Automatically collected from {source}; individual advert URL retained.",
        "description": "", "sellerSignals": [],
    }


def parse_cards(html, base, source, validator, require_cornwall=False):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"])
        if not validator(url) or url in seen:
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
        loc = "Cornwall"
        lm = re.search(r"([A-Za-z][A-Za-z '\-]{2,50},\s*Cornwall(?:,\s*England)?)", text, re.I)
        if lm:
            loc = clean(lm.group(1))
        item = listing_from_block(source, url, title, text, loc)
        if item:
            out.append(item)
    return out[:100]


def parse_rightbiz(html, base): return parse_cards(html, base, "Rightbiz", is_rightbiz_detail)
def parse_bfs(html, base): return parse_cards(html, base, "BusinessesForSale", is_bfs_detail)
def parse_daltons(html, base): return parse_cards(html, base, "Daltons", is_daltons_detail)
def parse_intelligent(html, base): return parse_cards(html, base, "Intelligent", is_intelligent_detail, require_cornwall=True)


def extract_description(soup):
    # Prefer a genuine Description heading over generic Overview/category blocks.
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
    if meta and meta.get("content"):
        return clean(meta["content"])[:DETAIL_TEXT_LIMIT]
    return ""


def detail_lead_text(soup, title):
    """Use only the listing's leading content so figures from related/recommended cards cannot leak in."""
    main = soup.find("main") or soup.body or soup
    text = clean(main.get_text(" ", strip=True))
    if title:
        pos = text.lower().find(clean(title).lower()[:60])
        if pos >= 0:
            text = text[pos:]
    # Most seller headline metrics and description occur early. Avoid page footers/recommendations.
    cut_markers = ["recently viewed", "featured listings", "similar businesses", "related businesses", "other businesses"]
    low = text.lower()
    cuts = [low.find(m) for m in cut_markers if low.find(m) >= 0]
    if cuts:
        text = text[:min(cuts)]
    return text[:6500]


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

        price, raw, ranged = extract_money(evidence, [r"Asking Price", r"Freehold Price", r"Leasehold Price", r"Price", r"Freehold", r"Leasehold"])
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
        if any(w in lower for w in ["owner run", "owner-run", "owner operated", "owner-operated", "hands-on operator"]): flags.append("Seller indicates material owner involvement.")
        if any(w in lower for w in ["retirement sale", "retiring", "pending retirement"]): flags.append("Seller states retirement as a reason for sale.")
        if "stock at valuation" in lower: flags.append("Stock is stated to be additional / at valuation.")
        if any(w in lower for w in ["relocatable", "home based", "home-based"]): flags.append("Advert indicates a flexible or relocatable operating model.")
        if any(w in lower for w in ["repeat customer", "repeat business", "recurring income", "annual subscription"]): flags.append("Advert mentions repeat or recurring customer revenue.")
        item["sellerSignals"] = flags[:5]
        if flags:
            item["notes"] += " " + " ".join(flags)
        return item
    except Exception as e:
        item["notes"] += f" Detail enrichment unavailable ({type(e).__name__})."
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
        with open("market.json", "r", encoding="utf-8") as f: return json.load(f).get("listings", [])
    except Exception: return []


def main():
    session = requests.Session()
    all_items, errors = [], []
    parsers = {"rightbiz": parse_rightbiz, "bfs": parse_bfs, "daltons": parse_daltons, "intelligent": parse_intelligent}
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
    if not items:
        previous = load_previous()
        if previous:
            items = previous
            errors.append("All live collectors returned no usable listings; retained previous feed.")

    enriched = []
    for i, item in enumerate(items):
        enriched.append(enrich_item(session, item) if i < DETAIL_LIMIT else item)
        if i < DETAIL_LIMIT: time.sleep(0.25)

    enriched = dedupe(enriched)
    enriched.sort(key=lambda x: (bool(x.get("description")), x.get("profit", 0), x.get("turnover", 0)), reverse=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(), "region": "Cornwall", "count": len(enriched),
        "sources": [x["name"] for x in SOURCES], "errors": errors, "collector_version": 2.1, "listings": enriched,
    }
    with open("market.json", "w", encoding="utf-8") as f: json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(enriched)} listings; errors={errors}")
    return 0 if enriched else 1


if __name__ == "__main__": sys.exit(main())
