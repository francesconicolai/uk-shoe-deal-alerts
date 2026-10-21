"""UK running-shoe deal scanner.

Scans UK shops (and marketplaces) for watched running shoes that are in stock in
UK size 8.5 or 9 at a significant discount, and emails a digest of new offers.

Run:  python scanner.py            scan, email new offers, update state
      python scanner.py --dry-run  scan and print offers, no email, no state change
      python scanner.py --report   print every watched shoe in size, any discount

Email settings come from environment variables (GitHub secrets):
  SMTP_USER, SMTP_PASSWORD (Gmail app password), ALERT_TO, optional SMTP_HOST/SMTP_PORT.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText

import requests

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

SIZES_UK = {8.5, 9.0}
MIN_DISCOUNT = 0.40          # shops: alert when price <= 60% of the reference price
MARKETPLACE_MIN_DISCOUNT = 0.40   # Vinted/eBay: same threshold, and only for new shoes
MARKETPLACE_NEW_CONDITION = r"^(brand )?new(?!.*defect)"   # Vinted "new with/without tags", eBay "new ..."
EXCLUDE_WOMENS = True        # skip products labelled women's (unisex is kept)
REALERT_DROP = 0.03          # re-alert an item when its price falls by 3% more
BACK_IN_STOCK_HOURS = 48     # re-alert if an item was unavailable this long

# Watched models: (name, regex on normalised title, approximate UK RRP in GBP or None).
# Order matters: more specific patterns first. The reference price used for the
# discount is the highest of the RRP below, the shop's own "was" price, and the
# highest full price seen for the model at any scanned shop.
MODELS = [
    # adidas
    ("adidas Adizero Adios Pro Evo 2", r"adios pro evo 2", 450),
    ("adidas Adizero Adios Pro 4", r"adios pro 4\b", 220),
    ("adidas Adizero Evo SL ATR", r"evo sl atr", 140),
    ("adidas Adizero Evo SL", r"(?<!pro )evo sl\b", 130),
    ("adidas Adizero SL 2", r"(?<!evo )(?<!duramo )\bsl ?2\b", 110),
    ("adidas Hyperboost Edge", r"hyperboost edge", None),
    ("adidas Supernova Rise 2", r"supernova rise 2\b", 130),
    ("adidas Supernova Rise 3", r"supernova rise 3\b", 130),
    ("adidas Boston 13", r"boston 13\b", 160),
    # Nike
    ("Nike Vomero Plus", r"vomero plus", 170),
    ("Nike Vomero 18", r"vomero 18\b", 140),
    ("Nike Pegasus Premium", r"pegasus premium", 190),
    ("Nike Pegasus Plus", r"pegasus plus", 155),
    ("Nike Pegasus 42", r"pegasus 42\b", 130),
    ("Nike Zoom Fly 6", r"zoom ?fly 6\b", 155),
    ("Nike Alphafly 3", r"alphafly (next% )?3\b", 285),
    ("Nike Vaporfly 4", r"vaporfly (next% )?4\b", 240),
    # ASICS
    ("ASICS Metaspeed Sky Tokyo", r"metaspeed sky tokyo", 240),
    ("ASICS Metaspeed Edge Tokyo", r"metaspeed edge tokyo", 240),
    ("ASICS Metaspeed Ray", r"metaspeed ray", None),
    ("ASICS Novablast 6", r"novablast 6\b", 140),
    ("ASICS Megablast", r"megablast", None),
    ("ASICS Superblast 3", r"superblast 3\b", 200),
    ("ASICS Sonicblast 2", r"sonicblast 2\b", None),
    ("ASICS Blazeblast", r"blazeblast", None),
    ("ASICS Gel-Nimbus 28", r"nimbus 28\b", 180),
    ("ASICS Gel-Cumulus 28", r"cumulus 28\b", 135),
    ("ASICS Gel-Kayano 33", r"kayano 33\b", 180),
]

# Other super shoes (any version) and the whole On range are also watched.
SUPER_SHOES = (r"alphafly|vaporfly|adios pro|metaspeed|endorphin (elite|pro)|deviate nitro elite|fast-?r nitro elite"
               r"|rocket ?x|cielo x|hyperion elite|wave rebellion pro|fuelcell (sc|supercomp) elite|cloudboom"
               r"|prime x|takumi sen|sc elite|elite v\d")
ON_MODELS = r"\bcloud ?(boom|monster|surfer|flow|eclipse|runner|stratus|ultra|vista|go|tilt|spark|swift|zone|x \d)"
EXCLUDE_TITLE =r"\btrail\b|spike|\b(md|ld)\b|\bxc\b|cross country|\bkid|junior|\bjnr\b|youth|\bgs\b|infant|toddler|child|sock|insole|sandal|slide"
WOMENS = r"\bwomen|\bwomens\b|\bwmns\b|\bladies\b|\bw's\b|\bwomen's\b"

# Shopify shops: domain -> collections to crawl (None = full catalogue).
SHOPIFY_SHOPS = {
    "www.startfitness.co.uk": ["mens-road-running-footwear", "carbon-plated-running-shoes"],
    "www.prodirectsport.com": ["running-mens-road-shoes-sale", "running-mens-carbon-shoes", "running-on-shoes"],
    "run4it.com": None,
    "achillesheel.co.uk": None,
    "upandrunning.co.uk": None,
    "therunningoutlet.co.uk": None,
}
SHOP_NAMES = {
    "www.startfitness.co.uk": "Start Fitness", "www.prodirectsport.com": "Pro:Direct Running",
    "run4it.com": "Run4It", "achillesheel.co.uk": "Achilles Heel", "upandrunning.co.uk": "Up & Running",
    "therunningoutlet.co.uk": "The Running Outlet",
}

STATE_DIR = os.environ.get("STATE_DIR", "state")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36", "Accept-Language": "en-GB,en;q=0.9"}


# ----------------------------------------------------------------------------
# Matching helpers
# ----------------------------------------------------------------------------

def normalise(title):
    t = title.lower().replace(chr(0x2019), "'")
    t = re.sub(r"[-_/|]+", " ", t)
    t = re.sub(r"\bgel ", "", t)
    return re.sub(r"\s+", " ", t)


def classify(title, vendor=""):
    """Return (model name, RRP) for a watched product, or None."""
    t = normalise(title)
    if re.search(EXCLUDE_TITLE, t):
        return None
    if EXCLUDE_WOMENS and re.search(WOMENS, t):
        return None
    for name, rx, rrp in MODELS:
        if re.search(rx, t):
            return name, rrp
    v = (vendor or "").strip().lower()
    if v in ("on", "on running", "on-running") or t.startswith("on ") or re.search(ON_MODELS, t):
        return "On (" + re.sub(r"^on\s+", "", title.strip(), flags=re.I)[:40] + ")", None
    m = re.search(SUPER_SHOES, t)
    if m:
        return "Super shoe: " + title.strip()[:60], None
    return None


NUM = r"(\d{1,2})(?:\.(0|5)|\s*(1/2))?"


def parse_uk_size(value, option_name="", assume_uk=True):
    """Parse a UK adult shoe size from a size label. Returns a float or None.

    Strict: a label that names only a US/EU/cm size is rejected, and a bare
    number is read as UK only when the option or shop is known to use UK sizes.
    """
    if value is None:
        return None
    v = str(value).upper().replace(chr(0xBD), ".5")  # 8½ -> 8.5
    m = re.search(r"\bUK\s*" + NUM + r"(?![\d.])", v) or re.search(r"(?<![\d.])" + NUM + r"\s*UK\b", v)
    if m:
        return float(m.group(1)) + (0.5 if (m.group(2) == "5" or m.group(3)) else 0.0)
    if re.search(r"\b(US|EU|EUR|FR|CM|JP|MX)\b", v):
        return None
    if "UK" in option_name.upper() or assume_uk:
        m = re.fullmatch(r"\s*" + NUM + r"\s*", v)
        if m:
            return float(m.group(1)) + (0.5 if (m.group(2) == "5" or m.group(3)) else 0.0)
    return None


# ----------------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------------

def get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=30)
        except requests.RequestException:
            time.sleep(5 * (i + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(int(r.headers.get("Retry-After", 0) or 0) or 30 * 2 ** i)
            continue
        return r
    return None


def shopify_products(domain, collections):
    paths = [f"/collections/{c}/products.json" for c in collections] if collections else ["/products.json"]
    seen = set()
    for path in paths:
        for page in range(1, 120):
            r = get(f"https://{domain}{path}", {"limit": 250, "page": page})
            if r is None or r.status_code != 200:
                print(f"  {domain}{path} page {page}: {'no response' if r is None else r.status_code}", file=sys.stderr)
                break
            prods = r.json().get("products", [])
            if not prods:
                break
            for p in prods:
                if p["id"] not in seen:
                    seen.add(p["id"])
                    yield p
            time.sleep(0.7)


def scan_shopify(domain, collections):
    offers = []
    n = 0
    for p in shopify_products(domain, collections):
        n += 1
        cls = classify(p["title"], p.get("vendor"))
        if not cls:
            continue
        model, rrp = cls
        opts = [o["name"] for o in p.get("options", [])]
        size_idx = [i for i, o in enumerate(opts) if re.search(r"size", o, re.I)]
        if not size_idx:
            continue
        si = size_idx[0]
        for v in p["variants"]:
            offers.append(make_offer(
                SHOP_NAMES.get(domain, domain), (model, rrp), p["title"],
                f"https://{domain}/products/{p['handle']}?variant={v['id']}", f"{domain}|{v['id']}",
                parse_uk_size(v.get(f"option{si + 1}"), opts[si]), v.get("available"),
                v["price"], v.get("compare_at_price")))
    print(f"{domain}: {n} products scanned, {len(offers)} watched variants", file=sys.stderr)
    return offers


def make_offer(source, cls, title, url, key, size_uk, available, price, was=None,
               condition="new", listing="shop"):
    price = float(price)
    was = float(was) if was else None
    return {"source": source, "model": cls[0], "rrp": cls[1], "title": title, "url": url, "key": key,
            "size_uk": size_uk, "available": bool(available), "price": price,
            "was": was if was and was > price else None, "condition": condition, "listing": listing}


def uk_label(label):
    """Size from a field the source labels as UK ('UK 8.5', '8.5', '6 (EU 39)')."""
    return parse_uk_size(re.sub(r"\(.*?\)", "", str(label or "")).replace("UK", "").strip())


def scan_nike(path="/gb/w/mens-running-shoes-37v7jznik1zy7ok"):
    """Nike UK: product wall for the listing, then the product feed for per-size stock.
    Uses the GB 'localizedSize' (UK size); 'nikeSize' is the US size and is never used."""
    ch = "d9a5bc42-4b9c-4976-858a-f159cf99c647"
    h = dict(UA, **{"nike-api-caller-id": "com.nike.commerce.nikedotcom.web", "Accept": "application/json"})
    wall = f"https://api.nike.com/discover/product_wall/v1/marketplace/GB/language/en-GB/consumerChannelId/{ch}"
    prods, anchor = {}, 0
    while anchor < 2000:
        j = requests.get(wall, headers=h, timeout=30,
                         params={"path": path, "queryType": "PRODUCTS", "anchor": anchor, "count": 100}).json()
        for g in j.get("productGroupings") or []:
            for p in g.get("products") or []:
                prods[p["productCode"]] = p
        if not (j.get("pages") or {}).get("next"):
            break
        anchor += 100
        time.sleep(0.5)
    watched = {c: p for c, p in prods.items() if classify("Nike " + ((p.get("copy") or {}).get("title") or ""), "Nike")}
    offers, codes = [], list(watched)
    for i in range(0, len(codes), 20):
        url = ("https://api.nike.com/product_feed/threads/v2?filter=marketplace(GB)&filter=language(en-GB)"
               f"&filter=channelId({ch})&filter=productInfo.merchProduct.styleColor({','.join(codes[i:i + 20])})")
        for obj in requests.get(url, headers=h, timeout=30).json().get("objects", []):
            for pi in obj.get("productInfo", []):
                sc = pi["merchProduct"]["styleColor"]
                p = watched.get(sc, {})
                title = "Nike " + ((p.get("copy") or {}).get("title") or sc)
                cls = classify(title, "Nike")
                if not cls:
                    continue
                avail = {a["skuId"]: a.get("available", False) for a in pi.get("availableSkus", [])}
                mp = pi.get("merchPrice", {})
                for sku in pi.get("skus", []):
                    gb = next((c for c in sku.get("countrySpecifications", []) if c.get("country") == "GB"), None)
                    if not gb or gb.get("localizedSizePrefix", "UK") != "UK":
                        continue
                    offers.append(make_offer("Nike UK", cls, f"{title} ({sc})", (p.get("pdpUrl") or {}).get("url", ""),
                                             f"nike|{sku['id']}", uk_label(gb.get("localizedSize")),
                                             avail.get(sku["id"], False), mp.get("currentPrice"), mp.get("fullPrice")))
        time.sleep(0.5)
    print(f"Nike UK: {len(prods)} products, {len(offers)} watched variants", file=sys.stderr)
    return offers


def scan_sportsshoes(path="/products/mens/running/shoes"):
    """SportsShoes.com: listing pages (__NEXT_DATA__), then the inventory service.
    Variant SKUs look like ADI19486_8-5_1328513, where the middle token is the UK size."""
    s = requests.Session()
    s.headers.update(UA)
    prods, page = {}, 1
    while page < 80:
        r = s.get("https://www.sportsshoes.com" + path, params={"page": page}, timeout=30)
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
        if not m:
            break
        ps = json.loads(m.group(1))["props"]["pageProps"]["productsData"]["products"]
        if not ps:
            break
        for p in ps:
            if classify(f"{p.get('brand', '')} {p['title']}", p.get("brand")):
                prods[p["sku"]] = p
        page += 1
        time.sleep(1)
    offers, skus = [], list(prods)
    for i in range(0, len(skus), 40):
        inv = s.get("https://inv.sportsshoes.com/query", params={"sku:in": ",".join(skus[i:i + 40])}, timeout=30).json()
        for rec in inv:
            p = prods.get(rec.get("sku"))
            if not p:
                continue
            title = f"{p.get('brand', '')} {p['title']}"
            cls = classify(title, p.get("brand"))
            for v in rec.get("variants", []):
                parts = v["sku"].split("_")
                size = uk_label(parts[1].replace("-", ".")) if len(parts) >= 3 else None
                offers.append(make_offer("SportsShoes", cls, title, "https://www.sportsshoes.com" + p["url"],
                                         f"sportsshoes|{v['sku']}", size, (v.get("inventory_level") or 0) > 0,
                                         p["price"], p.get("rrp")))
        time.sleep(0.5)
    print(f"SportsShoes: {page - 1} pages, {len(offers)} watched variants", file=sys.stderr)
    return offers


def scan_runnersneed():
    """Runners Need: site search JSON. Only in-stock sizes are listed, as 'UK 8.5'."""
    s = requests.Session()
    s.headers.update(dict(UA, Accept="application/json"))
    base = {"mainWebShop": "runnersneed", "fictiveWebShop": "212", "anaLang": "en", "locale": "en",
            "market": "uk", "platform": "public_site", "query": "mens running shoes", "size": 48,
            "filter": ["uk_fh_gender:(mens;unisex)", "uk_rn_product_type:(shoes)"]}
    offers, page = [], 0
    while page < 30:
        j = s.get("https://www.runnersneed.com/api/aem/search", params=dict(base, page=page), timeout=30).json()
        for it in j.get("items", []):
            for col in it.get("colours", []):
                title = f"{it.get('brand', '')} {it['title']} - {col.get('description', '')}"
                cls = classify(title, it.get("brand"))
                if not cls:
                    continue
                prices = col.get("prices", {})
                url = f"https://www.runnersneed.com/p{it['seoUrl']}.html?colour={col['colourId']}"
                for sz in col.get("sizes", []):
                    code = sz.get("code") or ""
                    if not code.upper().startswith("UK"):
                        continue
                    offers.append(make_offer("Runners Need", cls, title, url, f"runnersneed|{sz.get('sku') or url + code}",
                                             uk_label(code), sz.get("active") and sz.get("availability", 0) > 0,
                                             prices.get("SELL", {}).get("original"), prices.get("RRP", {}).get("original")))
        page += 1
        if page >= j.get("totalPages", 0):
            break
        time.sleep(1)
    print(f"Runners Need: {page} pages, {len(offers)} watched variants", file=sys.stderr)
    return offers


def scan_northernrunner(path="/shoes-c133"):
    """Northern Runner: listing pages, then a per-product options call with stock per 'UK x' size."""
    s = requests.Session()
    s.headers.update(UA)
    cands, page = {}, 1
    while page < 60:
        h = s.get("https://www.northernrunner.com" + path, params={"page": page}, timeout=30).text
        found = re.findall(r'href="(/[^"#]+-p(\d+))"', h)
        new = [(href, pid) for href, pid in found if pid not in cands]
        if not new:
            break
        for href, pid in new:
            slug = href.rsplit("/", 1)[-1]
            if re.search(r"-mens-|-men-|unisex", slug) and classify(slug.replace("-", " ")):
                cands[pid] = href
        page += 1
        time.sleep(1)
    offers = []
    for pid, href in cands.items():
        url = "https://www.northernrunner.com" + href
        j = s.get(f"https://www.northernrunner.com/ajax/get_product_options/{pid}",
                  headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}, timeout=30).json()
        pg = s.get(url, timeout=30).text
        ld = re.search(r'"name":"([^"]+)".*?"price":"([\d.]+)"', pg, re.S)
        rrp = re.search(r'(?:RRP|Was)[^\d£]*£\s*([\d.]+)', pg)
        title = html.unescape(ld.group(1)) if ld else href.rsplit("/", 1)[-1].replace("-", " ")
        cls = classify(title) or classify(href.rsplit("/", 1)[-1].replace("-", " "))
        if not (cls and ld):
            continue
        for att in j.get("attributes", []):
            if att.get("name", "").lower() != "size":
                continue
            for v in att.get("values", []):
                if not str(v.get("value", "")).upper().startswith("UK"):
                    continue
                offers.append(make_offer("Northern Runner", cls, title, url, f"northernrunner|{pid}|{v['value']}",
                                         uk_label(v["value"]), v.get("has_stock") and (v.get("stock_level") or 0) > 0,
                                         ld.group(2), rrp.group(1) if rrp else None))
        time.sleep(1)
    print(f"Northern Runner: {len(cands)} watched products, {len(offers)} variants", file=sys.stderr)
    return offers


VINTED_QUERIES = ["vaporfly", "alphafly", "adios pro", "metaspeed", "evo sl", "boston 13", "superblast",
                  "novablast", "megablast", "sonicblast", "blazeblast", "pegasus", "vomero", "zoom fly",
                  "supernova rise", "hyperboost", "nimbus", "cumulus", "kayano", "on cloud", "endorphin"]
VINTED_SIZE_IDS = {8.5: 785, 9.0: 786}   # Men > Shoes (catalog 1231), UK sizes


def scan_vinted():
    """Vinted UK (second-hand): catalog pages filtered to men's shoes in UK 8.5/9.
    The size is what the seller entered, so these offers are flagged as marketplace listings."""
    s = requests.Session()
    s.headers.update(UA)
    s.get("https://www.vinted.co.uk/", timeout=30)
    offers, seen = [], set()
    for q in VINTED_QUERIES:
        params = [("search_text", q), ("catalog[]", 1231), ("order", "newest_first"), ("page", 1)]
        params += [("size_ids[]", VINTED_SIZE_IDS[z]) for z in SIZES_UK if z in VINTED_SIZE_IDS]
        params += [("status_ids[]", 6), ("status_ids[]", 1)]   # new with tags, new without tags
        r = s.get("https://www.vinted.co.uk/catalog", params=params, timeout=30)
        if r.status_code != 200:
            print(f"Vinted '{q}': HTTP {r.status_code}", file=sys.stderr)
            break
        for alt, iid in re.findall(r'alt="([^"]*)"[^>]*data-testid="product-item-id-(\d+)--image--img"', r.text):
            if iid in seen:
                continue
            seen.add(iid)
            m = re.match(r"(?P<title>.*?), (?:Brand: (?P<brand>[^,]*), )?Condition: (?P<cond>[^,]*), "
                         r"Size: (?P<size>[^,]*), (?P<price>[\d.,]+) £(?:, (?P<total>[\d.,]+) £)?",
                         html.unescape(alt))
            if not m:
                continue
            title = f"{m['brand'] or ''} {m['title']}".strip()
            cls = classify(title, m["brand"])
            if not cls:
                continue
            link = re.search(r'href="(/items/%s-[^"?]*)' % iid, r.text)
            price = (m["total"] or m["price"]).replace(",", "")   # total includes Buyer Protection
            offers.append(make_offer("Vinted", cls, title, "https://www.vinted.co.uk" + (link.group(1) if link else f"/items/{iid}"),
                                     f"vinted|{iid}", uk_label(m["size"]), True, price,
                                     condition=m["cond"].lower(), listing="marketplace"))
        time.sleep(2)
    print(f"Vinted: {len(seen)} listings seen, {len(offers)} watched", file=sys.stderr)
    return offers


def scan_ebay():
    """eBay UK via the official Browse API (free developer keys in EBAY_CLIENT_ID / EBAY_CLIENT_SECRET).
    Search results are filtered on the 'UK Shoe Size' aspect; each candidate is then re-checked
    with getItem so the listed UK size is confirmed. Includes auctions."""
    cid, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if not (cid and secret):
        return []
    import base64
    api = "https://api.ebay.com"
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    tok = requests.post(f"{api}/identity/v1/oauth2/token", timeout=30,
                        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
                        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})
    tok.raise_for_status()
    h = {"Authorization": f"Bearer {tok.json()['access_token']}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"}
    sizes = "|".join(f"{z:g}" for z in sorted(SIZES_UK))
    offers, seen = [], set()
    for q in VINTED_QUERIES:
        r = requests.get(f"{api}/buy/browse/v1/item_summary/search", headers=h, timeout=30, params={
            "q": q, "category_ids": "15709", "limit": 100, "sort": "newlyListed",
            "aspect_filter": f"categoryId:15709,UK Shoe Size:{{{sizes}}}",
            "filter": "buyingOptions:{AUCTION|FIXED_PRICE},conditions:{NEW},itemLocationCountry:GB,priceCurrency:GBP"})
        if r.status_code != 200:
            print(f"eBay '{q}': HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
            continue
        for it in r.json().get("itemSummaries", []):
            if it["itemId"] in seen:
                continue
            seen.add(it["itemId"])
            cls = classify(it["title"])
            if not cls:
                continue
            auction = "AUCTION" in it.get("buyingOptions", [])
            price = ((it.get("currentBidPrice") if auction else None) or it.get("price") or {}).get("value")
            if price is None:
                continue
            ref = cls[1] or 0
            if ref and float(price) > ref * (1 - MARKETPLACE_MIN_DISCOUNT):
                continue   # only fetch item details for candidates that could qualify
            d = requests.get(f"{api}/buy/browse/v1/item/{it['itemId']}", headers=h, timeout=30).json()
            asp = {a["name"]: a["value"] for a in d.get("localizedAspects", [])}
            offers.append(make_offer("eBay", cls, it["title"], it["itemWebUrl"], f"ebay|{it['itemId']}",
                                     uk_label(asp.get("UK Shoe Size")), True, price,
                                     condition=it.get("condition", "").lower(),
                                     listing="auction (current bid)" if auction else "marketplace"))
        time.sleep(0.5)
    print(f"eBay: {len(seen)} listings seen, {len(offers)} candidates", file=sys.stderr)
    return offers


# Non-Shopify sources, run in parallel with the Shopify crawl. Remove an entry to disable it.
EXTRA_SOURCES = [scan_nike, scan_sportsshoes, scan_runnersneed, scan_northernrunner, scan_vinted, scan_ebay]


# ----------------------------------------------------------------------------
# State, pricing, email
# ----------------------------------------------------------------------------

def load_state():
    try:
        with open(os.path.join(STATE_DIR, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"alerts": {}, "last_available": {}, "ref_price": {}}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def update_reference_prices(offers, state):
    """Track the highest full (undiscounted) shop price seen for each watched model."""
    ref = state.setdefault("ref_price", {})
    for o in offers:
        if o["listing"] != "shop" or o["model"].startswith(("On (", "Super shoe")):
            continue
        full = o["was"] or o["price"]
        ref[o["model"]] = max(ref.get(o["model"], 0), full)


def reference(o, state):
    """Reference (full) price: the configured RRP or the shop's was-price, whichever is higher.
    For models without a configured RRP, fall back to the highest full price seen for the model
    (special editions can inflate that, so the RRP is preferred when known)."""
    cands = [o["rrp"], o["was"]]
    if not o["rrp"] and not o["model"].startswith(("On (", "Super shoe")):
        cands.append(state.get("ref_price", {}).get(o["model"]))
    cands = [c for c in cands if c]
    return max(cands) if cands else None


def title_size_conflict(title, size):
    """Marketplace sellers sometimes file a listing under the wrong size. Reject the listing if
    its title states a different UK size, or states a US/EU size without any UK size."""
    t = title.lower().replace(chr(0xBD), ".5")
    uk = [float(a or b) for a, b in re.findall(r"\buk\s*(\d{1,2}(?:\.5)?)\b|\b(\d{1,2}(?:\.5)?)\s*uk\b", t)]
    if uk:
        return size not in uk
    return bool(re.search(r"\b(us|eu|eur)\s*\d|\d(\.5)?\s*(us|eu|eur)\b", t))


def select_offers(offers, state):
    hits = []
    for o in offers:
        if not o["available"] or o["size_uk"] not in SIZES_UK:
            continue
        ref = reference(o, state)
        if not ref:
            continue
        o["ref"] = ref
        o["discount"] = 1 - o["price"] / ref
        if o["listing"] == "shop":
            if o["discount"] >= MIN_DISCOUNT:
                hits.append(o)
        elif (o["discount"] >= MARKETPLACE_MIN_DISCOUNT and re.match(MARKETPLACE_NEW_CONDITION, o["condition"])
              and not title_size_conflict(o["title"], o["size_uk"])):
            hits.append(o)
    return sorted(hits, key=lambda o: -o["discount"])


def new_alerts(hits, state, now):
    out = []
    for o in hits:
        prev = state["alerts"].get(o["key"])
        last_av = state["last_available"].get(o["key"])
        back = last_av and (now - dt.datetime.fromisoformat(last_av)).total_seconds() > BACK_IN_STOCK_HOURS * 3600
        if prev is None or o["price"] <= prev["price"] * (1 - REALERT_DROP) or back:
            out.append(o)
    return out


def group_alerts(alerts):
    """Merge the UK 8.5 and UK 9 rows of the same listing at the same price."""
    groups = {}
    for o in alerts:
        g = groups.setdefault((o["source"], o["title"], o["price"]), dict(o, sizes=[]))
        g["sizes"].append(o["size_uk"])
    return sorted(groups.values(), key=lambda g: -g["discount"])


def email_body(alerts):
    groups = group_alerts(alerts)
    rows = "".join(
        f"<tr><td>{o['discount']:.0%}</td><td><b>£{o['price']:.2f}</b><br><s>£{o['ref']:.0f}</s></td>"
        f"<td>UK {', '.join(f'{s:g}' for s in sorted(o['sizes']))}</td>"
        f"<td><a href=\"{html.escape(o['url'])}\">{html.escape(o['title'])}</a>"
        f"<br><small>{html.escape(o['model'])} | {html.escape(o['source'])} | {o['condition']} {o['listing']}</small></td></tr>"
        for o in groups)
    return (f"<p>{len(groups)} new running-shoe offer(s) in UK 8.5/9, at least {MIN_DISCOUNT:.0%} below the reference price.</p>"
            f"<table border=1 cellpadding=5 cellspacing=0 style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>"
            f"<tr><th>Off</th><th>Price</th><th>Size</th><th>Item</th></tr>{rows}</table>"
            f"<p><small>Reference price = the higher of the model's RRP and the shop's was-price. "
            f"Size and stock were read from the shop's per-size stock data at scan time. "
            f"Vinted/eBay rows are private sellers listing new shoes: the size is what the seller entered, "
            f"and very cheap 'new' listings can be counterfeit, so check the seller and photos.</small></p>")


def send_email(alerts):
    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"), os.environ.get("ALERT_TO")
    if not (user and pw and to):
        print("SMTP settings missing; not sending.", file=sys.stderr)
        return False
    flag = os.path.join(STATE_DIR, "SMTP_DISABLED")
    if os.path.exists(flag):
        print(f"{flag} exists after an earlier login failure; delete it to resume email.", file=sys.stderr)
        return False
    best = alerts[0]
    msg = MIMEText(email_body(alerts), "html", "utf-8")
    msg["Subject"] = f"Shoe deal: {best['model']} £{best['price']:.0f} ({best['discount']:.0%} off)" + \
                     (f" +{len(alerts) - 1} more" if len(alerts) > 1 else "")
    msg["From"], msg["To"] = user, to
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
    try:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    except smtplib.SMTPAuthenticationError:
        # One attempt only: block further logins until someone fixes the credentials.
        os.makedirs(STATE_DIR, exist_ok=True)
        open(flag, "w").write(dt.datetime.utcnow().isoformat())
        print("SMTP login failed; email disabled until SMTP_DISABLED is removed.", file=sys.stderr)
        return False
    return True


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--shops", nargs="*", help="limit to these Shopify domains")
    ap.add_argument("--only", nargs="*", help="run only these sources: shopify nike sportsshoes runnersneed "
                                              "northernrunner vinted ebay")
    args = ap.parse_args()

    shops = {d: c for d, c in SHOPIFY_SHOPS.items() if not args.shops or d in args.shops}

    def all_shopify():
        # Shopify rate-limits by client IP across all its stores, so crawl shops one at a time.
        out = []
        for d, c in shops.items():
            try:
                out += scan_shopify(d, c)
            except Exception as e:
                print(f"{d} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return out

    extra = [f for f in EXTRA_SOURCES if not args.only or f.__name__.removeprefix("scan_") in args.only]
    jobs = ([all_shopify] if not args.only or "shopify" in args.only else []) + ([] if args.shops else extra)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(f) for f in jobs]
        offers = []
        for f in futs:
            try:
                offers += f.result()
            except Exception as e:  # one broken source must not stop the others
                print(f"source failed: {type(e).__name__}: {e}", file=sys.stderr)

    offers = list({o["key"]: o for o in offers}.values())   # some listings repeat across pages
    state = load_state()
    update_reference_prices(offers, state)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    if args.report:
        for o in sorted(offers, key=lambda o: (o["model"], o["price"])):
            if o["available"] and o["size_uk"] in SIZES_UK:
                ref = reference(o, state)
                disc = f"{1 - o['price'] / ref:4.0%}" if ref else "   ?"
                print(f"{disc} £{o['price']:7.2f} UK{o['size_uk']:<4g} {o['model'][:38]:38s} {o['source'][:14]:14s} {o['title'][:60]}")
        return

    hits = select_offers(offers, state)
    alerts = new_alerts(hits, state, now)
    print(f"{len(offers)} watched variants, {len(hits)} qualifying offers, {len(alerts)} new", file=sys.stderr)
    for o in alerts:
        print(f"{o['discount']:4.0%} £{o['price']:.2f} (ref £{o['ref']:.0f}) UK{o['size_uk']:g} {o['source']}: {o['title']} {o['url']}")
    if args.dry_run:
        return

    sent = send_email(alerts) if alerts else False
    if sent:
        for o in alerts:
            state["alerts"][o["key"]] = {"price": o["price"], "ts": now.isoformat(timespec="seconds")}
    for o in offers:
        if o["available"] and o["size_uk"] in SIZES_UK:
            state["last_available"][o["key"]] = now.isoformat(timespec="seconds")
    save_state(state)


if __name__ == "__main__":
    main()
