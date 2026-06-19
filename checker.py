import hashlib
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
EMAIL_TO = os.getenv("ALERT_EMAIL", "chatgpt.idiot.stupid@gmail.com").strip()
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "display-deals-55r0v7m6k2q8ps").strip()
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "").strip()
TRADERA_APP_ID = os.getenv("TRADERA_APP_ID", "").strip()
TRADERA_APP_KEY = os.getenv("TRADERA_APP_KEY", "").strip()
TRADERA_CATEGORY_ID = os.getenv("TRADERA_CATEGORY_ID", "301824").strip()
FORCE_RUN = os.getenv("FORCE_RUN", "") == "1"
STATE_PATH = Path("seen.json")
UA = {"User-Agent": "display-listing-checker/2.2"}

BLOCKET_CATEGORY = os.getenv("BLOCKET_CATEGORY", "datorskärm").strip()
CATEGORY_TERMS = ["datorskärm", "datorskärmar", "dataskärm", "dataskärmar", "bildskärm", "bildskärmar"]
CATEGORY_KEYS = ("category", "categories", "categoryName", "category_name", "categoryPath", "category_path", "breadcrumbs", "breadcrumb")


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


def save_state(seen: set[str], latest: dict) -> None:
    STATE_PATH.write_text(
        json.dumps({"updated_at": datetime.now(TZ).isoformat(timespec="seconds"), "seen": sorted(seen), "latest_results": latest}, ensure_ascii=False, indent=2) + "\n",
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
    for key in ("docs", "items", "listings", "results", "searchResults", "ads", "data", "itemList"):
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


def category_text(value) -> str:
    parts = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).lower()
            if any(w.lower() in key_lower for w in CATEGORY_KEYS):
                parts.append(category_text(item))
            elif isinstance(item, (dict, list)):
                nested = category_text(item)
                if nested:
                    parts.append(nested)
    elif isinstance(value, list):
        for item in value:
            nested = category_text(item)
            if nested:
                parts.append(nested)
    elif isinstance(value, str):
        parts.append(value)
    return " ".join(p for p in parts if p).lower()


def is_datorskarm_category(item: dict) -> bool:
    text = category_text(item)
    return any(term in text for term in CATEGORY_TERMS)


def build_latest_payload(all_items: dict[str, dict], new_items: list[dict]) -> dict:
    checked_at = datetime.now(TZ).isoformat(timespec="seconds")
    new_ids = sorted(item["id"] for item in new_items)
    digest = hashlib.sha256((checked_at + "|" + "|".join(new_ids)).encode("utf-8")).hexdigest()[:16]
    return {"checked_at": checked_at, "notification_id": digest, "max_price_sek": MAX_PRICE, "total_count": len(all_items), "new_count": len(new_items), "items": list(all_items.values())}


def blocket_results() -> dict[str, dict]:
    results = {}
    raw = request_json("GET", "https://blocket-api.se/v1/search", params={"category": BLOCKET_CATEGORY, "sort_order": "PUBLISHED_DESC"})
    found = flatten_items(raw)
    log(f"Blocket category {BLOCKET_CATEGORY!r}: {len(found)} raw")
    rejected_category = 0
    for item in found[:100]:
        item_id = str(first(item, ("id", "ad_id", "adId")))
        if not item_id:
            continue
        details = request_json("GET", "https://blocket-api.se/v1/ad/recommerce", params={"id": item_id}) or {}
        try:
            details = details["loaderData"]["item-recommerce"]["itemData"]
        except Exception:
            pass
        merged = {**item, **(details if isinstance(details, dict) else {})}
        if not is_datorskarm_category(merged):
            rejected_category += 1
            continue
        price = parse_money(first(merged, ("price", "listPrice", "priceAmount")))
        if price is None or price > MAX_PRICE:
            continue
        title = str(first(merged, ("title", "heading", "subject", "name"), "Blocket listing"))
        url = link_from(first(item, ("canonical_url", "canonicalUrl", "url", "shareUrl")), f"https://www.blocket.se/recommerce/forsale/item/{item_id}")
        results[f"blocket:{item_id}"] = {"source": "Blocket", "id": f"blocket:{item_id}", "title": title, "price": price, "url": url}
        time.sleep(0.2)
    log(f"Blocket rejected because category was not datorskärm/bildskärm: {rejected_category}")
    return results


def is_tradera_auction(item: dict) -> bool:
    text = " ".join(str(item.get(key, "")).lower() for key in ("itemType", "itemTypeName", "listingType", "sellingType", "type", "format", "expoItemType"))
    if any(item.get(key) is True for key in ("isAuction", "auction", "isBidding", "hasBids")):
        return True
    if any(word in text for word in ("auction", "auktion", "bidding")):
        return True
    has_bid_price = any(item.get(key) is not None for key in ("currentBid", "currentBidAmount", "startPrice", "minimumBid"))
    has_fixed_price = any(item.get(key) is not None for key in ("buyNowPrice", "buyItNowPrice", "fixedPrice", "price", "salesPrice"))
    return has_bid_price and not has_fixed_price


def tradera_price(item: dict):
    for key in ("buyNowPrice", "buyItNowPrice", "fixedPrice", "price", "salesPrice", "amount"):
        price = parse_money(first(item, (key,)))
        if price is not None:
            return price
    return None


def tradera_results() -> dict[str, dict]:
    results = {}
    if not TRADERA_CATEGORY_ID:
        log("Tradera skipped: no category id configured.")
        return results
    if not TRADERA_APP_ID or not TRADERA_APP_KEY:
        log("Tradera skipped: credentials missing.")
        return results
    headers = {"X-App-Id": TRADERA_APP_ID, "X-App-Key": TRADERA_APP_KEY, "Accept": "application/json", "Content-Type": "application/json"}
    attempts = [
        lambda: request_json("GET", "https://api.tradera.com/v4/search", headers=headers, params={"categoryId": TRADERA_CATEGORY_ID, "page": 1, "pageSize": 100}),
        lambda: request_json("GET", "https://api.tradera.com/v4/search", headers=headers, params={"categoryIds": TRADERA_CATEGORY_ID, "page": 1, "pageSize": 100}),
        lambda: request_json("POST", "https://api.tradera.com/v4/search/advanced", headers=headers, json={"categoryId": int(TRADERA_CATEGORY_ID), "page": 1, "pageSize": 100, "maxPrice": MAX_PRICE}),
        lambda: request_json("POST", "https://api.tradera.com/v4/search/advanced", headers=headers, json={"categoryIds": [int(TRADERA_CATEGORY_ID)], "page": 1, "pageSize": 100, "maxPrice": MAX_PRICE}),
    ]
    found = []
    for attempt in attempts:
        found = flatten_items(attempt())
        if found:
            break
    log(f"Tradera category {TRADERA_CATEGORY_ID}: {len(found)} raw")
    for item in found[:100]:
        if is_tradera_auction(item):
            continue
        item_id = str(first(item, ("itemId", "id", "listingId", "item_id", "auctionId")))
        if not item_id:
            continue
        price = tradera_price(item)
        if price is None or price > MAX_PRICE:
            continue
        title = str(first(item, ("title", "heading", "name", "shortDescription"), "Tradera listing"))
        url = link_from(first(item, ("url", "itemUrl", "canonicalUrl", "shareUrl")), f"https://www.tradera.com/item/{item_id}")
        results[f"tradera:{item_id}"] = {"source": "Tradera", "id": f"tradera:{item_id}", "title": title, "price": price, "url": url}
    return results


def build_email_body(current_items: list[dict], new_ids: set[str]) -> str:
    blocket = [item for item in current_items if item["source"] == "Blocket"]
    tradera = [item for item in current_items if item["source"] == "Tradera"]
    lines = [
        f"{len(current_items)} current category listings under {MAX_PRICE} SEK.",
        f"New since last run: {len(new_ids)}.",
        f"Blocket: {len(blocket)} current. Tradera: {len(tradera)} current.",
        f"Checked: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M %Z')}",
        "",
    ]
    for source_name, items in (("Blocket", blocket), ("Tradera", tradera)):
        lines.append(f"== {source_name} ==")
        if not items:
            lines += ["0 current matches", ""]
            continue
        for index, item in enumerate(items, 1):
            marker = "NEW " if item["id"] in new_ids else ""
            title = str(item["title"])[:140]
            lines += [f"{index}. {marker}{title}", f"   {item['price']} SEK", f"   {item['url']}", ""]
    return "\n".join(lines)


def send_email(current_items: list[dict], new_ids: set[str]) -> None:
    if not NTFY_TOKEN:
        log("NTFY_TOKEN missing; cannot send email.")
        return
    response = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=build_email_body(current_items, new_ids).encode("utf-8"),
        headers={**UA, "Authorization": f"Bearer {NTFY_TOKEN}", "Title": f"{len(current_items)} current monitor listings", "Email": EMAIL_TO, "Priority": "default"},
        timeout=25,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"ntfy email failed HTTP {response.status_code}: {response.text[:500]}")
    log("Email request accepted by ntfy.")


def main() -> None:
    now = datetime.now(TZ)
    if not FORCE_RUN and now.hour not in (9, 21):
        log("Not 09:00/21:00 Stockholm time; skipping.")
        return
    seen = load_seen()
    all_items = {}
    all_items.update(blocket_results())
    all_items.update(tradera_results())
    current_items = sorted(all_items.values(), key=lambda item: (item["source"], item["price"], item["title"].lower()))
    new_ids = {key for key in all_items if key not in seen}
    for key in all_items:
        seen.add(key)
    latest = build_latest_payload(all_items, [item for item in current_items if item["id"] in new_ids])
    save_state(seen, latest)
    log(f"Current matches: {len(current_items)} total, {len(new_ids)} new")
    if current_items and (new_ids or FORCE_RUN):
        send_email(current_items, new_ids)


if __name__ == "__main__":
    main()
