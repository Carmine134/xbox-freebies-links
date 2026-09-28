"""Refresh the shared price database.

Runs on GitHub Actions, not on anyone's PC. Prices come from Microsoft's public
catalog API, so no account, no token and no trust in contributors is involved.
"""

import csv
import io
import json
import pathlib
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
LINKS = ROOT / "links.json"
DB = ROOT / "catalog.json"
CSV_FILE = ROOT / "catalog.csv"
CATALOG_URL = ("https://displaycatalog.mp.microsoft.com/v7.0/products"
               "?bigIds={ids}&market={market}&languages=en-US")
MARKETS = ["US", "GB", "NO", "DE"]


def parse_dt(value):
    if not value:
        return None
    try:  # e.g. "9998-12-30T00:00:00.0000000Z"
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch(ids, market):
    """Current price of each product in one store."""
    out = {}
    now = datetime.now(timezone.utc)
    today = datetime.now().strftime("%Y-%m-%d")
    for start in range(0, len(ids), 20):
        batch = ids[start:start + 20]
        url = CATALOG_URL.format(ids=",".join(batch), market=market)
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                data = json.load(r)
        except Exception as e:
            print(f"  batch at {start} failed: {type(e).__name__}", flush=True)
            time.sleep(3)
            continue
        for prod in data.get("Products", []):
            pid = prod["ProductId"].upper()
            title = (prod.get("LocalizedProperties") or [{}])[0].get("ProductTitle", "")
            free, amount, currency = False, None, ""
            for dsa in prod.get("DisplaySkuAvailabilities", []):
                for av in dsa.get("Availabilities", []):
                    if "Purchase" not in av.get("Actions", []):
                        continue
                    cond = av.get("Conditions") or {}
                    begins, ends = parse_dt(cond.get("StartDate")), parse_dt(cond.get("EndDate"))
                    if (begins and begins > now) or (ends and ends < now):
                        continue
                    price = (av.get("OrderManagementData") or {}).get("Price") or {}
                    listed = price.get("ListPrice")
                    currency = price.get("CurrencyCode") or currency
                    if listed is not None and (amount is None or listed < amount):
                        amount = listed  # cheapest way to get it
                    if listed == 0:
                        free = True
            out[pid] = {"title": title, "price": amount, "currency": currency,
                        "free": free, "checked": today}
        if (start // 20) % 25 == 0:
            print(f"  {market}: {min(start + 20, len(ids))}/{len(ids)}", flush=True)
        time.sleep(0.5)
    return out


def write_csv(db):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Product ID", "Name", "Price", "Currency", "Free", "Store", "Checked"])
    rows = []
    for market, items in db.get("markets", {}).items():
        for pid, e in items.items():
            price = e.get("price")
            rows.append([pid, e.get("title", ""), "" if price is None else f"{price:.2f}",
                         e.get("currency", ""), "yes" if e.get("free") else "no",
                         market, e.get("checked", "")])
    for row in sorted(rows, key=lambda r: (r[1].lower(), r[5])):
        w.writerow(row)
    CSV_FILE.write_text(buf.getvalue(), encoding="utf-8")


def main():
    ids = sorted({str(i).upper() for i in json.loads(LINKS.read_text())["productIds"]})
    print(f"{len(ids)} product IDs, markets: {', '.join(MARKETS)}", flush=True)
    db = json.loads(DB.read_text(encoding="utf-8")) if DB.exists() else {"updated": "", "markets": {}}
    for market in MARKETS:
        print(f"{market}...", flush=True)
        db.setdefault("markets", {}).setdefault(market, {}).update(fetch(ids, market))
    db["updated"] = datetime.now().isoformat(timespec="seconds")
    DB.write_text(json.dumps(db, indent=1, sort_keys=True), encoding="utf-8")
    write_csv(db)
    total = sum(len(v) for v in db["markets"].values())
    free = sum(1 for e in db["markets"].get("US", {}).values() if e.get("free"))
    print(f"done: {total} entries across {len(db['markets'])} stores, {free} free in the US store")


if __name__ == "__main__":
    sys.exit(main())
