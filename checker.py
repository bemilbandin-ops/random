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
TRADERA_CATEGORY_ID = os.getenv("TRADERA_CATEGORY_ID", "").strip()
FORCE_RUN = os.getenv("FORCE_RUN", "") == "1"
STATE_PATH = Path("seen.json")
UA = {"User-Agent": "display-listing-checker/2.0"}

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
        json.dumps(
            {"updated_at": datetime.now(TZ).isoformat(timespec="seconds"), "seen": sorted(seen), "latest_results": latest},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
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
    return {
        "checked_at": checked_at,
        "notification_id": digest,
        "max_price_sek": MAX_PRICE,
        "total_count": len(all_items),
        "new_count": len(new_items),
        "items": new_items,
    }


def blocket_results() -> dict[str, dict]:
    results = {}
    raw = request_json(
        "GET",
        "https://blocket-api.se/v1/search",
        params={"category": BLOCKET_CATEGORY, "sort_order": "PUBLISHED_DESC"},
    )
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


def tradera_results() -> dict[str, dict]:
    if not TRADERA_CATEGORY_ID:
        log("Tradera skipped: no TRADERA_CATEGORY_ID configured. This prevents keyword junk from Tradera.")
        return {}
    log("Tradera category mode is configured but not enabled in this script version.")
    return {}


def build_email_body(new_items: list[dict]) -> str:
    lines = [
        f"{len(new_items)} new datorskärm-category listings under {MAX_PRICE} SEK.",
        "Showing all category-filtered matches.",
        f"Checked: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M %Z')}",
        "",
    ]
    for index, item in enumerate(new_items, 1):
        title = str(item["title"])[:140]
        lines += [f"{index}. [{item['source']}] {title}", f"   {item['price']} SEK", f"   {item['url']}", ""]
    return "\n".join(lines)


def send_email(new_items: list[dict]) -> None:
    if not NTFY_TOKEN:
        log("NTFY_TOKEN missing; cannot send email.")
        return
    response = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=build_email_body(new_items).encode("utf-8"),
        headers={
            **UA,
            "Authorization": f"Bearer {NTFY_TOKEN}",
            "Title": f"{len(new_items)} datorskärm listings",
            "Email": EMAIL_TO,
            "Priority": "default",
        },
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

    new_items = [item for key, item in all_items.items() if key not in seen]
    new_items.sort(key=lambda item: (item["source"], item["price"], item["title"].lower()))

    for key in all_items:
        seen.add(key)
    latest = build_latest_payload(all_items, new_items)
    save_state(seen, latest)
    log(f"Category-filtered matches: {latest['total_count']} total, {latest['new_count']} new")

    if new_items:
        send_email(new_items)


if __name__ == "__main__":
    main()
