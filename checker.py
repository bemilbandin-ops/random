import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Stockholm")
MAX_PRICE = int(os.getenv("MAX_PRICE_SEK", "500"))
EMAIL_TO = os.getenv("ALERT_EMAIL", "").strip()
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "display-deals-55r0v7m6k2q8ps").strip() or "display-deals-55r0v7m6k2q8ps"
TRADERA_APP_ID = os.getenv("TRADERA_APP_ID", "").strip()
TRADERA_APP_KEY = os.getenv("TRADERA_APP_KEY", "").strip()
FORCE_RUN = os.getenv("FORCE_RUN", "") == "1"
STATE_PATH = Path("seen.json")
QUERIES = ["datorskärm", "dataskärm", "bildskärm", "skärm", "pc skärm", "gaming skärm"]
UA = {"User-Agent": "display-listing-checker/1.1"}


def log(message: str) -> None:
    print(f"[{datetime.now(TZ).isoformat(timespec='seconds')}] {message}", flush=True)


def load_seen() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return set(payload.get("seen", []))
        if isinstance(payload, list):
            return set(payload)
    except Exception as exc:
        log(f"Could not read seen.json: {exc}")
    return set()


def save_seen(seen: set[str]) -> None:
    STATE_PATH.write_text(
        json.dumps({"updated_at": datetime.now(TZ).isoformat(timespec="seconds"), "seen": sorted(seen)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_money(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for key in ("amount", "value", "price", "buyNowPrice", "fixedPrice", "salesPrice"):
            parsed = parse_money(value.get(key))
            if parsed is not None:
                return parsed
    if isinstance(value, str):
        match = re.search(r"(\d[\d\s.,]*)", value)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            return int(digits) if digits else None
    return None


def flatten_items(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("docs", "items", "listings", "results", "searchResults", "ads", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = flatten_items(value)
            if nested:
                return nested
    for value in payload.values():
        nested = flatten_items(value)
        if nested:
            return nested
    return []


def request_json(method: str, url: str, **kwargs):
    try:
        headers = {**UA, **kwargs.pop("headers", {})}
        response = requests.request(method, url, headers=headers, timeout=25, **kwargs)
        if response.status_code >= 400:
            log(f"{method} {url} HTTP {response.status_code}: {response.text[:300]}")
            return None
        return response.json()
    except Exception as exc:
        log(f"{method} {url} failed: {exc}")
        return None


def first(mapping, keys, default=""):
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def link_from(value, fallback: str) -> str:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, dict):
        return link_from(first(value, ("url", "href", "canonicalUrl")), fallback)
    return fallback


def blocket_results() -> dict[str, dict]:
    results = {}
    for query in QUERIES:
        raw = request_json("GET", "https://blocket-api.se/v1/search", params={"query": query, "sort_order": "PUBLISHED_DESC"})
        found = flatten_items(raw)
        log(f"Blocket {query!r}: {len(found)} raw")
        for item in found[:25]:
            item_id = str(first(item, ("id", "ad_id", "adId")))
            if not item_id:
                continue
            details = request_json("GET", "https://blocket-api.se/v1/ad/recommerce", params={"id": item_id}) or {}
            try:
                details = details["loaderData"]["item-recommerce"]["itemData"]
            except Exception:
                pass
            merged = {**item, **(details if isinstance(details, dict) else {})}
            price = parse_money(first(merged, ("price", "listPrice", "priceAmount")))
            if price is None or price > MAX_PRICE:
                continue
            title = str(first(merged, ("title", "heading", "subject", "name"), "Blocket listing"))
            url = link_from(first(item, ("canonical_url", "canonicalUrl", "url", "shareUrl")), f"https://www.blocket.se/recommerce/forsale/item/{item_id}")
            results[f"blocket:{item_id}"] = {"source": "Blocket", "id": f"blocket:{item_id}", "title": title, "price": price, "url": url}
        time.sleep(0.3)
    return results


def is_tradera_auction(item: dict) -> bool:
    if any(item.get(key) is True for key in ("isAuction", "auction", "isBidding", "hasBids")):
        return True
    text = " ".join(str(item.get(key, "")).lower() for key in ("itemType", "itemTypeName", "listingType", "sellingType", "type", "format", "expoItemType"))
    if any(word in text for word in ("auction", "auktion", "bidding")):
        return True
    has_bid_price = any(item.get(key) is not None for key in ("currentBid", "currentBidAmount", "startPrice", "minimumBid"))
    has_fixed_price = any(item.get(key) is not None for key in ("buyNowPrice", "buyItNowPrice", "fixedPrice", "price", "salesPrice"))
    return has_bid_price and not has_fixed_price


def tradera_results() -> dict[str, dict]:
    results = {}
    if not TRADERA_APP_ID or not TRADERA_APP_KEY:
        log("Tradera credentials missing; skipping Tradera.")
        return results
    headers = {"X-App-Id": TRADERA_APP_ID, "X-App-Key": TRADERA_APP_KEY, "Accept": "application/json", "Content-Type": "application/json"}
    for query in QUERIES:
        attempts = [
            lambda: request_json("GET", "https://api.tradera.com/v4/search", headers=headers, params={"query": query, "page": 1, "pageSize": 50}),
            lambda: request_json("GET", "https://api.tradera.com/v4/search", headers=headers, params={"q": query, "page": 1, "pageSize": 50}),
            lambda: request_json("POST", "https://api.tradera.com/v4/search/advanced", headers=headers, json={"query": query, "page": 1, "pageSize": 50, "maxPrice": MAX_PRICE}),
            lambda: request_json("POST", "https://api.tradera.com/v4/search/advanced", headers=headers, json={"searchText": query, "page": 1, "pageSize": 50, "priceMax": MAX_PRICE}),
        ]
        found = []
        for attempt in attempts:
            found = flatten_items(attempt())
            if found:
                break
        log(f"Tradera {query!r}: {len(found)} raw")
        for item in found[:50]:
            if is_tradera_auction(item):
                continue
            item_id = str(first(item, ("itemId", "id", "listingId", "item_id", "auctionId")))
            if not item_id:
                continue
            price = None
            for key in ("buyNowPrice", "buyItNowPrice", "fixedPrice", "price", "salesPrice", "amount"):
                price = parse_money(first(item, (key,)))
                if price is not None:
                    break
            if price is None or price > MAX_PRICE:
                continue
            title = str(first(item, ("title", "heading", "name", "shortDescription"), "Tradera listing"))
            url = link_from(first(item, ("url", "itemUrl", "canonicalUrl", "shareUrl")), f"https://www.tradera.com/item/{item_id}")
            results[f"tradera:{item_id}"] = {"source": "Tradera", "id": f"tradera:{item_id}", "title": title, "price": price, "url": url}
        time.sleep(0.5)
    return results


def build_email_body(new_items: list[dict]) -> str:
    shown = min(len(new_items), 8)
    lines = [
        f"{len(new_items)} new display listings under {MAX_PRICE} SEK.",
        f"Showing first {shown}. The rest are marked seen so you do not get spammed forever.",
        f"Checked: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M %Z')}",
        "",
    ]
    for index, item in enumerate(new_items[:shown], 1):
        title = str(item['title'])[:120]
        lines += [f"{index}. [{item['source']}] {title}", f"   {item['price']} SEK", f"   {item['url']}", ""]
    body = "\n".join(lines)
    encoded = body.encode("utf-8")
    if len(encoded) > 3000:
        body = encoded[:3000].decode("utf-8", errors="ignore") + "\n\n[truncated]"
    return body


def send_email(new_items: list[dict]) -> None:
    if not EMAIL_TO:
        log("ALERT_EMAIL is missing; not sending email.")
        return
    body = build_email_body(new_items)
    try:
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={**UA, "Title": f"{len(new_items)} new display listings", "Email": EMAIL_TO, "Priority": "default"},
            timeout=25,
        )
        if response.status_code >= 400:
            log(f"Email request failed but workflow will continue: HTTP {response.status_code}: {response.text[:500]}")
            return
        log("Email request accepted.")
    except Exception as exc:
        log(f"Email request failed but workflow will continue: {exc}")


def main() -> None:
    now = datetime.now(TZ)
    if not FORCE_RUN and now.hour not in (9, 21):
        log("Not 09:00/21:00 Stockholm time; skipping.")
        return
    seen = load_seen()
    all_items = {}
    all_items.update(blocket_results())
    all_items.update(tradera_results())
    new_items = [item for key, item in all_items.items() if key not in seen]
    for key in all_items:
        seen.add(key)
    save_seen(seen)
    new_items.sort(key=lambda item: (item["source"], item["price"], item["title"].lower()))
    log(f"Matches: {len(all_items)} total, {len(new_items)} new")
    if new_items:
        send_email(new_items)


if __name__ == "__main__":
    main()
