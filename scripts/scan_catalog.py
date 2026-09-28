"""Scan the Xbox store for free items.

Microsoft publishes every product page in its sitemaps, and prices come from the
public catalog API, so this needs no account and no secrets. It runs on GitHub
Actions, not on anyone's PC.

  python scripts/scan_catalog.py --market US --mode full     # price the whole store
  python scripts/scan_catalog.py --market US --mode daily    # re-check what matters

Outputs, per market:
  free-<MARKET>.json         every product that is free right now
  newly-free-<MARKET>.json   products that turned free in the last 21 days

The full price list is kept in .cache/ between runs (GitHub Actions cache), because
it is large and changes constantly; only the small useful lists are committed.
"""

import argparse
import gzip
import json
import pathlib
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
SITEMAP_INDEX = "https://www.xbox.com/sitemap.xml"
CATALOG_URL = ("https://displaycatalog.mp.microsoft.com/v7.0/products"
               "?bigIds={ids}&market={market}&languages=en-US")
UA = {"User-Agent": "CarminesXboxFreebies catalog scanner (+https://github.com/Carmine134/xbox-freebies-links)"}
NEWLY_FREE_DAYS = 21
PRODUCT_RE = re.compile(r"/store/[^/]+/([A-Za-z0-9]{12})(?:[/?#]|</)")


def fetch_url(url, timeout=60, retries=3):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"    retry after {type(e).__name__}", flush=True)
            time.sleep(3 * (attempt + 1))


def all_product_ids(locale="en-US"):
    """Every product in the store, from Microsoft's own sitemaps."""
    index = fetch_url(SITEMAP_INDEX).decode("utf-8", "replace")
    maps = [u for u in re.findall(r"<loc>([^<]+)</loc>", index) if f"pdp-{locale}-" in u]
    print(f"{len(maps)} product sitemaps for {locale}", flush=True)
    ids = set()
    for n, url in enumerate(maps, 1):
        body = gzip.decompress(fetch_url(url)).decode("utf-8", "replace")
        found = {m.upper() for m in PRODUCT_RE.findall(body)}
        ids |= found
        print(f"  sitemap {n}/{len(maps)}: +{len(found)} (total {len(ids)})", flush=True)
    return sorted(ids)


def parse_dt(value):
    if not value:
        return None
    try:  # e.g. "9998-12-30T00:00:00.0000000Z"
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def price_products(ids, market, label=""):
    """{pid: {title, price, currency, free}} straight from the catalog."""
    out = {}
    now = datetime.now(timezone.utc)
    started = time.time()
    for start in range(0, len(ids), 20):
        batch = ids[start:start + 20]
        try:
            data = json.loads(fetch_url(CATALOG_URL.format(ids=",".join(batch), market=market),
                                        timeout=45).decode("utf-8", "replace"))
        except Exception as e:
            print(f"    batch at {start} gave up: {type(e).__name__}", flush=True)
            continue
        for prod in data.get("Products", []):
            pid = prod["ProductId"].upper()
            title = (prod.get("LocalizedProperties") or [{}])[0].get("ProductTitle", "")
            market_props = (prod.get("MarketProperties") or [{}])[0]
            released = (market_props.get("OriginalReleaseDate") or "")[:10]
            # Players' own score, all-time rather than this week's handful of votes
            all_time = next((u for u in (market_props.get("UsageData") or [])
                             if u.get("AggregateTimeSpan") == "AllTime"), {})
            rating = all_time.get("AverageRating") or None
            ratings = int(all_time.get("RatingCount") or 0)
            # DLC points at the game it belongs to, a bundle at what it contains
            parent = next((r.get("RelatedProductId") for r in market_props.get("RelatedProducts") or []
                           if r.get("RelationshipType") in ("addOnParent", "Parent")), "")
            platforms = set()
            for dsa in prod.get("DisplaySkuAvailabilities", []):
                sku_props = (dsa.get("Sku") or {}).get("Properties") or {}
                for pkg in sku_props.get("Packages") or []:
                    for dep in pkg.get("PlatformDependencies") or []:
                        name = dep.get("PlatformName") or ""
                        if "Xbox" in name:
                            platforms.add("xbox")
                        elif "Desktop" in name:
                            platforms.add("pc")
            free, amount, currency, msrp = False, None, "", None
            sub_only, trial, ends_at = False, False, None
            for dsa in prod.get("DisplaySkuAvailabilities", []):
                sku = dsa.get("Sku") or {}
                # A trial costs nothing but is not the game: "ENDLESS Legend 2" sells for
                # 49.99 with a trial sku at zero beside it. Worth recording rather than
                # ignoring, so the app can say "free trial" instead of saying nothing.
                is_trial = (sku.get("SkuType") == "trial"
                            or (sku.get("Properties") or {}).get("IsTrial"))
                for av in dsa.get("Availabilities", []):
                    if "Purchase" not in av.get("Actions", []):
                        continue
                    cond = av.get("Conditions") or {}
                    begins, ends = parse_dt(cond.get("StartDate")), parse_dt(cond.get("EndDate"))
                    if (begins and begins > now) or (ends and ends < now):
                        continue
                    price = (av.get("OrderManagementData") or {}).get("Price") or {}
                    if av.get("RemediationRequired"):
                        # Free only through Game Pass, Ubisoft+ and the like: not a price
                        if price.get("ListPrice") == 0:
                            sub_only = True
                        continue
                    listed = price.get("ListPrice")
                    if is_trial:
                        trial = trial or listed == 0
                        continue
                    currency = price.get("CurrencyCode") or currency
                    if listed is not None and (amount is None or listed < amount):
                        amount = listed  # cheapest way to get it
                        msrp = price.get("MSRP")
                        ends_at = ends   # when this particular offer stops
                    if listed == 0:
                        free = True
            # A sale has a real end date. Offers that run to 2029 or 9998 are just how
            # the store writes "no end", and saying "ends in 1200 days" would be silly.
            sale_end = ""
            if amount is not None and msrp and amount < msrp and ends_at:
                if ends_at < now + timedelta(days=400):
                    sale_end = ends_at.strftime("%Y-%m-%d")
            out[pid] = {"title": title, "price": amount, "currency": currency, "free": free,
                        "msrp": msrp, "released": released, "subscription": sub_only,
                        "trial": trial, "platform": "+".join(sorted(platforms)),
                        "sale_end": sale_end, "rating": rating, "ratings": ratings,
                        "parent": parent}
        done = min(start + 20, len(ids))
        if (start // 20) % 100 == 0 or done == len(ids):
            rate = done / max(1, time.time() - started)
            left = (len(ids) - done) / max(rate, 0.01) / 60
            print(f"  {label}{done}/{len(ids)} ({rate:.0f}/s, ~{left:.0f} min left)", flush=True)
    return out


def load_prices(market):
    path = CACHE / f"prices-{market}.json.gz"
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_prices(market, prices):
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"prices-{market}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(prices, f, separators=(",", ":"), sort_keys=True)
    print(f"price history: {len(prices)} products ({path.stat().st_size // 1024} KB)")


def merge(prices, found, today):
    """Fold new readings into the history and note when something turned free.

    Only a product we have priced before can be said to have changed. A first sighting
    is recorded with no "free since" date, whether that is the baseline scan or a
    product that appeared in the store later.
    """
    turned_free = []
    for pid, info in found.items():
        before = prices.get(pid)
        entry = {"t": info["title"], "p": info["price"], "c": info["currency"],
                 "f": info["free"], "d": today, "m": info.get("msrp"),
                 "r": info.get("released", ""), "pl": info.get("platform", ""),
                 "s": bool(info.get("subscription")), "tl": bool(info.get("trial")),
                 "se": info.get("sale_end", ""), "ra": info.get("rating"),
                 "rc": info.get("ratings") or 0, "pa": info.get("parent", "")}
        # Keep every price change, so the app can draw a history. Unchanged prices add
        # nothing, which is why this stays small.
        history = (before or {}).get("h") or []
        if info["price"] is not None and (not history or history[-1][1] != info["price"]):
            history = history[-39:] + [[today, info["price"]]]
        if history:
            entry["h"] = history
        if info["free"]:
            if not before:
                entry["free_since"] = ""            # never seen it priced: nothing to compare
            elif before.get("f"):
                entry["free_since"] = before.get("free_since", "")
            else:
                entry["free_since"] = today         # watched it go from paid to free
                turned_free.append(pid)
        prices[pid] = entry
    return turned_free


def write_index(market, prices):
    """Every product in the store, for the app's Xbox Store tab.

    Compact on purpose: one array per product rather than named fields, gzipped. Box art
    is left out - the app fetches that for the tiles it is showing.
    """
    items = {pid: [e.get("t", ""), e.get("p"), 1 if e.get("f") else 0,
                   e.get("m"), e.get("r", ""), e.get("pl", ""),
                   1 if e.get("s") else 0, e.get("h") or [], 1 if e.get("tl") else 0,
                   e.get("se", ""), e.get("ra"), e.get("rc") or 0, e.get("pa", "")]
             for pid, e in prices.items()}
    path = ROOT / f"index-{market}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"updated": datetime.now().isoformat(timespec="seconds"), "market": market,
                   "currency": next((e.get("c") for e in prices.values() if e.get("c")), ""),
                   "count": len(items), "items": items}, f, separators=(",", ":"))
    print(f"{market}: index of {len(items)} products ({path.stat().st_size // 1024} KB)")


def write_outputs(market, prices):
    today = datetime.now()
    cutoff = (today - timedelta(days=NEWLY_FREE_DAYS)).strftime("%Y-%m-%d")

    free = {pid: {"title": e.get("t", ""), "currency": e.get("c", ""),
                  "free_since": e.get("free_since", "")}
            for pid, e in prices.items() if e.get("f")}
    (ROOT / f"free-{market}.json").write_text(json.dumps(
        {"updated": today.isoformat(timespec="seconds"), "market": market,
         "count": len(free), "items": free}, indent=0, sort_keys=True), encoding="utf-8")

    recent = {pid: e for pid, e in free.items() if e.get("free_since", "") >= cutoff}
    (ROOT / f"newly-free-{market}.json").write_text(json.dumps(
        {"updated": today.isoformat(timespec="seconds"), "market": market,
         "days": NEWLY_FREE_DAYS, "count": len(recent), "items": recent},
        indent=0, sort_keys=True), encoding="utf-8")
    print(f"{market}: {len(free)} free, {len(recent)} turned free in {NEWLY_FREE_DAYS} days")
    write_index(market, prices)


def daily_slice(prices, all_ids, today):
    """What to re-check on a normal day.

    Everything currently free (offers end), everything checked long ago, and a
    rotating seventh of the store so the whole catalog turns over every week.
    """
    stale = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    picked = {pid for pid, e in prices.items() if e.get("f") or e.get("d", "") < stale}
    day = datetime.now().timetuple().tm_yday % 7
    picked |= {pid for i, pid in enumerate(all_ids) if i % 7 == day}
    return sorted(picked)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="US")
    ap.add_argument("--mode", choices=["full", "daily"], default="daily")
    ap.add_argument("--limit", type=int, default=0, help="Only price this many products (testing)")
    args = ap.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")
    prices = load_prices(args.market)
    print(f"{args.market}: {len(prices)} products known", flush=True)

    ids = all_product_ids()
    if args.mode == "full" or not prices:
        todo = ids
        print(f"full scan: {len(todo)} products", flush=True)
    else:
        todo = daily_slice(prices, ids, today)
        print(f"daily scan: {len(todo)} of {len(ids)} products", flush=True)
    if args.limit:
        todo = todo[:args.limit]

    found = price_products(todo, args.market, label=f"{args.market} ")
    turned_free = merge(prices, found, today)
    save_prices(args.market, prices)
    write_outputs(args.market, prices)
    if turned_free:
        print(f"turned free today: {len(turned_free)}")
        for pid in turned_free[:10]:
            print(f"  {pid} {prices[pid].get('t', '')[:50]}")


if __name__ == "__main__":
    sys.exit(main())
