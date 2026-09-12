import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent":"BusinessHunter/2.2 (+personal research; low-frequency collector)","Accept-Language":"en-GB,en;q=0.9"}
TIMEOUT=25; DETAIL_LIMIT=60; DETAIL_TEXT_LIMIT=3200
SOURCES=[
 {"name":"Rightbiz","url":"https://www.rightbiz.co.uk/businesses-for-sale-in-cornwall.html","parser":"rightbiz"},
 {"name":"BusinessesForSale","url":"https://uk.businessesforsale.com/uk/search/businesses-for-sale-in-cornwall","parser":"bfs"},
 {"name":"Daltons","url":"https://www.daltonsbusiness.com/listing-businesses-for-sale-in-cornwall/","parser":"daltons"},
 {"name":"Intelligent","url":"https://www.intelligent.co.uk/businesses-for-sale/cornwall-businesses-for-sale","parser":"intelligent"},
]

def clean(s): return re.sub(r"\s+"," ",(s or "")).strip()
def stable_id(source,url): return f"{source.lower().replace(' ','-')}-{hashlib.sha1(url.encode()).hexdigest()[:14]}"
def amount_value(raw):
 if not raw:return None
 m=re.search(r"([\d,.]+)\s*([KkMm]?)",clean(raw).replace("£",""))
 if not m:return None
 try:x=float(m.group(1).replace(",",""))
 except ValueError:return None
 if m.group(2).lower()=="k":x*=1000
 elif m.group(2).lower()=="m":x*=1000000
 return int(x)
def extract_money(text,labels):
 t=clean(text)
 for label in labels:
  m=re.search(rf"{label}\s*:?\s*£?\s*([\d,.]+)\s*([KkMm]?)\s*(?:-|–|to)\s*£?\s*([\d,.]+)\s*([KkMm]?)",t,re.I)
  if m:
   lo,hi=amount_value(m.group(1)+m.group(2)),amount_value(m.group(3)+m.group(4))
   if lo is not None and hi is not None:return int((lo+hi)/2),clean(m.group(0)),True
  m=re.search(rf"{label}\s*:?\s*(?:c\.?|circa|over|>|from)?\s*£\s*([\d,.]+)\s*([KkMm]?)",t,re.I)
  if m:
   v=amount_value(m.group(1)+m.group(2))
   if v is not None:return v,clean(m.group(0)),False
 return None,"",False
def infer_profit(text):
 for measure,labels in [("Adjusted EBITDA",[r"Indicative Adjusted EBITDA",r"Adjusted EBITDA"]),("EBITDA",[r"EBITDA"]),("Reconstituted profit",[r"Reconstituted (?:Net )?Profit"]),("Adjusted net profit",[r"Adjusted Net Profit"]),("Annual net profit",[r"Annual Net Profit",r"Net Profit"]),("Seller-stated profit",[r"Profit"])]:
  v,raw,rng=extract_money(text,labels)
  if v is not None:return v,raw,rng,measure
 return None,"",False,"Unknown"
def valid(url,kind):
 p=urlparse(url);path=p.path.lower().rstrip("/")
 if kind=="rightbiz":return p.netloc.endswith("rightbiz.co.uk") and bool(re.search(r"/buy_business/for_sale/\d+_[^/]+\.html$",p.path,re.I))
 if kind=="bfs":return p.netloc.endswith("businessesforsale.com") and path.endswith(".aspx") and "/search/" not in path
 if kind=="daltons":return p.netloc.endswith("daltonsbusiness.com") and "/listing/" in path
 return p.netloc.endswith("intelligent.co.uk") and "/businesses-for-sale/" in path and not path.endswith("/businesses-for-sale") and not path.endswith("/cornwall-businesses-for-sale")
def block_for_anchor(a):
 node=a;c=[]
 for _ in range(6):
  if not node.parent:break
  node=node.parent;text=clean(node.get_text(" ",strip=True))
  if 70<=len(text)<=2400:c.append((len(text),node))
  if len(text)>4000:break
 return sorted(c,key=lambda z:z[0])[0][1] if c else (a.parent or a)
def title_from(a,b):
 t=clean(a.get_text(" ",strip=True))
 if len(t)>=8 and t.lower() not in {"details","contact seller","read more"}:return t
 for h in b.find_all(["h1","h2","h3","h4"]):
  t=clean(h.get_text(" ",strip=True))
  if len(t)>=8:return t
 return ""
def listing(source,url,title,text,loc="Cornwall"):
 price,pr,prr=extract_money(text,[r"Asking Price",r"Freehold Price",r"Leasehold Price",r"Price",r"Freehold",r"Leasehold"]);turn,tr,trr=extract_money(text,[r"Annual Turnover",r"Turnover",r"Revenue"]);profit,por,porr,pm=infer_profit(text)
 if not any(v is not None for v in (price,turn,profit)):return None
 return {"id":stable_id(source,url),"name":title[:180],"location":loc or "Cornwall","type":"","status":"New","price":price or 0,"priceDisplay":pr,"priceIsRange":prr,"turnover":turn or 0,"turnoverDisplay":tr,"turnoverIsRange":trr,"profit":profit or 0,"profitDisplay":por,"profitIsRange":porr,"profitMeasure":pm,"rent":0,"employees":0,"ownerDays":0,"source":source,"url":url,"growth":"Review the full seller description for evidenced growth opportunities.","risks":"Automated screening data. Verify seller figures, owner role, staffing, tenure and accounts before relying on it.","notes":f"Automatically collected from {source}; individual advert URL retained.","description":"","sellerSignals":[]}
def parse_cards(html,base,source,kind,require_cornwall=False):
 soup=BeautifulSoup(html,"html.parser");out=[];seen=set()
 for a in soup.find_all("a",href=True):
  url=urljoin(base,a["href"])
  if not valid(url,kind) or url in seen:continue
  seen.add(url);b=block_for_anchor(a);text=clean(b.get_text(" ",strip=True));low=text.lower()
  if require_cornwall and "cornwall" not in low:continue
  if "nationwide" in low and "cornwall" not in low:continue
  title=title_from(a,b)
  if not title:continue
  lm=re.search(r"([A-Za-z][A-Za-z '\-]{2,50},\s*Cornwall(?:,\s*England)?)",text,re.I);loc=clean(lm.group(1)) if lm else "Cornwall"
  x=listing(source,url,title,text,loc)
  if x:out.append(x)
 return out[:100]
def extract_description(soup):
 hs=soup.find_all(["h2","h3","h4"]);ordered=[h for h in hs if clean(h.get_text(" ",strip=True)).lower() in {"description","business description","the business"}]+[h for h in hs if clean(h.get_text(" ",strip=True)).lower()=="overview"]
 for h in ordered:
  chunks=[]
  for sib in h.find_all_next(limit=20):
   if sib is h:continue
   if sib.name in {"h1","h2","h3","h4"} and chunks:break
   if sib.name in {"p","li"}:
    s=clean(sib.get_text(" ",strip=True))
    if len(s)>=25 and s not in chunks:chunks.append(s)
   if sum(map(len,chunks))>=DETAIL_TEXT_LIMIT:break
  if chunks:return clean(" ".join(chunks))[:DETAIL_TEXT_LIMIT]
 meta=soup.find("meta",attrs={"name":re.compile("description",re.I)})
 return clean(meta.get("content"))[:DETAIL_TEXT_LIMIT] if meta and meta.get("content") else ""
def detail_lead_text(soup,title):
 main=soup.find("main") or soup.body or soup;text=clean(main.get_text(" ",strip=True));pos=text.lower().find(clean(title).lower()[:60]) if title else -1
 if pos>=0:text=text[pos:]
 low=text.lower();cuts=[low.find(m) for m in ["recently viewed","featured listings","similar businesses","related businesses","other businesses"] if low.find(m)>=0]
 return text[:min(cuts)] if cuts else text[:6500]
def enrich(session,item):
 try:
  r=session.get(item["url"],headers=HEADERS,timeout=TIMEOUT);r.raise_for_status();soup=BeautifulSoup(r.text,"html.parser")
  for tag in soup(["script","style","noscript","svg"]):tag.decompose()
  desc=extract_description(soup);evidence=clean(desc+" "+detail_lead_text(soup,item.get("name","")));item["description"]=desc
  price,raw,rng=extract_money(evidence,[r"Asking Price",r"Freehold Price",r"Leasehold Price",r"Price",r"Freehold",r"Leasehold"])
  if price is not None:item["price"],item["priceDisplay"],item["priceIsRange"]=price,raw,rng
  turn,raw,rng=extract_money(evidence,[r"Annual Turnover",r"Turnover",r"Revenue"])
  if turn is not None:item["turnover"],item["turnoverDisplay"],item["turnoverIsRange"]=turn,raw,rng
  profit,raw,rng,measure=infer_profit(evidence)
  if profit is not None:item["profit"],item["profitDisplay"],item["profitIsRange"],item["profitMeasure"]=profit,raw,rng,measure
  low=evidence.lower();flags=[]
  if any(w in low for w in ["owner run","owner-run","owner operated","owner-operated","hands-on operator"]):flags.append("Seller indicates material owner involvement.")
  if any(w in low for w in ["retirement sale","retiring","pending retirement"]):flags.append("Seller states retirement as a reason for sale.")
  if "stock at valuation" in low:flags.append("Stock is stated to be additional / at valuation.")
  if any(w in low for w in ["relocatable","home based","home-based"]):flags.append("Advert indicates a flexible or relocatable operating model.")
  if any(w in low for w in ["repeat customer","repeat business","recurring income","annual subscription"]):flags.append("Advert mentions repeat or recurring customer revenue.")
  franchise=any(w in low for w in ["franchise opportunity","franchise resale","franchise territory","franchise fee","become a franchisee","franchise package","franchise investment"])
  item["isFranchise"]=franchise
  if franchise:flags.append("Franchise / territory opportunity rather than a standalone acquisition.")
  item["sellerSignals"]=flags[:6];item["notes"]+=(" "+" ".join(flags)) if flags else ""
  price=item.get("price",0);item["acquisitionBand"]="Core" if price and price<=250000 else ("Stretch" if price and price<=350000 else ("Large" if price else "Unknown"))
  return item
 except Exception as e:item["notes"]+=f" Detail enrichment unavailable ({type(e).__name__}).";return item
def dedupe(items):
 by={x["url"].rstrip("/"):x for x in items};out={}
 for x in by.values():
  key=re.sub(r"[^a-z0-9]","",x["name"].lower())[:100];rich=sum(bool(x.get(k)) for k in ("price","turnover","profit","description"))
  if key not in out or rich>sum(bool(out[key].get(k)) for k in ("price","turnover","profit","description")):out[key]=x
 return list(out.values())
def load_previous():
 try:
  with open("market.json",encoding="utf-8") as f:return json.load(f).get("listings",[])
 except Exception:return []
def main():
 session=requests.Session();all_items=[];errors=[]
 for src in SOURCES:
  try:
   r=session.get(src["url"],headers=HEADERS,timeout=TIMEOUT);r.raise_for_status();found=parse_cards(r.text,src["url"],src["name"],src["parser"],src["parser"]=="intelligent");all_items+=found;print(f"{src['name']}: found {len(found)} listing candidates");time.sleep(.7)
  except Exception as e:errors.append(f"{src['name']}: {type(e).__name__}: {e}")
 items=dedupe(all_items)
 if not items:
  items=load_previous();errors.append("All live collectors returned no usable listings; retained previous feed.") if items else None
 enriched=[]
 for i,x in enumerate(items):enriched.append(enrich(session,x) if i<DETAIL_LIMIT else x);time.sleep(.25) if i<DETAIL_LIMIT else None
 enriched=[x for x in dedupe(enriched) if not x.get("isFranchise")]
 enriched.sort(key=lambda x:((x.get("acquisitionBand")!="Core"),-x.get("profit",0)))
 payload={"updated_at":datetime.now(timezone.utc).isoformat(),"region":"Cornwall","count":len(enriched),"sources":[x["name"] for x in SOURCES],"errors":errors,"collector_version":2.2,"acquisition_policy":{"core_max":250000,"stretch_max":350000,"franchises_excluded":True,"reserve_policy":"Keep 6-12 months personal living costs in reserve; assess usable cash, working capital and finance/vendor terms before affordability."},"listings":enriched}
 with open("market.json","w",encoding="utf-8") as f:json.dump(payload,f,indent=2,ensure_ascii=False)
 print(f"Wrote {len(enriched)} acquisition listings; errors={errors}");return 0 if enriched else 1
if __name__=="__main__":sys.exit(main())