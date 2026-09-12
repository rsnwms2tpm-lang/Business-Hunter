import json, re, sys, time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "BusinessHunter/1.0 (+personal research; low-frequency collector)",
    "Accept-Language": "en-GB,en;q=0.9",
}
TIMEOUT = 25

SOURCES = [
    ("Rightbiz", "https://www.rightbiz.co.uk/businesses-for-sale-in-cornwall.html"),
    ("BusinessesForSale", "https://uk.businessesforsale.com/uk/search/businesses-for-sale-in-cornwall"),
]


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def pounds(text, label=None):
    t = clean(text)
    if label:
        m = re.search(label + r"[^£]{0,25}£\s*([\d,]+(?:\.\d+)?)", t, re.I)
    else:
        m = re.search(r"£\s*([\d,]+(?:\.\d+)?)", t)
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")))
    except ValueError:
        return None


def ranged_value(text, label):
    m = re.search(label + r"\s*:\s*£?([\d,.]+)\s*([KkMm]?)\s*-\s*£?([\d,.]+)\s*([KkMm]?)", text, re.I)
    if not m:
        return None
    def cv(v, suffix):
        x = float(v.replace(",", ""))
        if suffix.lower() == "k": x *= 1000
        if suffix.lower() == "m": x *= 1_000_000
        return int(x)
    lo, hi = cv(m.group(1), m.group(2)), cv(m.group(3), m.group(4))
    return int((lo + hi) / 2)


def parse_rightbiz(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        title = clean(a.get_text(" ", strip=True))
        if not title or len(title) < 12:
            continue
        if "/businesses-for-sale/" not in href and "business-for-sale" not in href:
            continue
        url = urljoin(base, href)
        if url in seen: continue
        seen.add(url)
        block = a
        for _ in range(4):
            if block.parent: block = block.parent
        text = clean(block.get_text(" ", strip=True))
        price = pounds(text, r"(?:Asking Price|Leasehold|Freehold)") or pounds(text)
        turnover = pounds(text, r"Turnover")
        profit = pounds(text, r"Profit")
        loc = "Cornwall"
        lm = re.search(r"([A-Za-z -]+(?:in|,)?\s*Cornwall)", text)
        if lm: loc = clean(lm.group(1))[-80:]
        if price or turnover or profit:
            out.append({
                "id": "rb-" + str(abs(hash(url))),
                "name": title[:180], "location": loc, "type": "", "status": "New",
                "price": price or 0, "turnover": turnover or 0, "profit": profit or 0,
                "profitMeasure": "Seller-stated profit" if profit else "Unknown",
                "rent": 0, "employees": 0, "ownerDays": 0,
                "source": "Rightbiz", "url": url,
                "growth": "Seller advert may mention growth potential; review the source listing before relying on it.",
                "risks": "Automated extraction. Verify asking price, profit definition, lease/freehold terms and accounts with the seller.",
                "notes": "Automatically collected from the Cornwall market page."
            })
    return out[:80]


def parse_bfs(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for h in soup.find_all(["h2","h3"]):
        title = clean(h.get_text(" ", strip=True))
        if not title or len(title) < 8: continue
        a = h.find("a", href=True) or (h.parent.find("a", href=True) if h.parent else None)
        if not a: continue
        url = urljoin(base, a["href"])
        if url in seen: continue
        seen.add(url)
        block = h
        for _ in range(4):
            if block.parent: block = block.parent
        text = clean(block.get_text(" ", strip=True))
        price = pounds(text, r"Asking Price") or ranged_value(text, r"Asking Price")
        turnover = pounds(text, r"Turnover") or ranged_value(text, r"Turnover")
        profit = pounds(text, r"Net Profit") or ranged_value(text, r"Net Profit")
        if price or turnover or profit:
            out.append({
                "id": "bfs-" + str(abs(hash(url))),
                "name": title[:180], "location": "Cornwall", "type": "", "status": "New",
                "price": price or 0, "turnover": turnover or 0, "profit": profit or 0,
                "profitMeasure": "Net profit" if profit else "Unknown",
                "rent": 0, "employees": 0, "ownerDays": 0,
                "source": "BusinessesForSale", "url": url,
                "growth": "Review seller description for specific growth opportunities.",
                "risks": "Some values may be advertised as ranges; automated values use the midpoint. Verify all figures before analysis.",
                "notes": "Automatically collected from the Cornwall market page."
            })
    return out[:80]


def dedupe(items):
    by = {}
    for x in items:
        key = re.sub(r"[^a-z0-9]", "", x["name"].lower())[:80]
        if key not in by or sum(bool(by[key].get(k)) for k in ("price","turnover","profit")) < sum(bool(x.get(k)) for k in ("price","turnover","profit")):
            by[key] = x
    return list(by.values())


def main():
    all_items, errors = [], []
    for name, url in SOURCES:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            if name == "Rightbiz": all_items.extend(parse_rightbiz(r.text, url))
            else: all_items.extend(parse_bfs(r.text, url))
            time.sleep(1)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
    items = dedupe(all_items)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "region": "Cornwall",
        "count": len(items),
        "sources": [x[0] for x in SOURCES],
        "errors": errors,
        "listings": sorted(items, key=lambda x: (x.get("profit",0), x.get("turnover",0)), reverse=True),
    }
    with open("market.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(items)} listings; errors={errors}")
    return 0 if items else 1

if __name__ == "__main__":
    sys.exit(main())
