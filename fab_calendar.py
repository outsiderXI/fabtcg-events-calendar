from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "state.json"
DOCS = ROOT / "docs"
FEEDS = DOCS / "feeds"

DATE_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"([A-Za-z]{3,9})"
    r"(?:\s+(\d{4}))?"
    r"(?:,\s*([0-9]{1,2}(?::[0-9]{2})?\s*(?:AM|PM|am|pm)))?"
)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

@dataclass
class FabEvent:
    uid: str
    title: str
    event_type: str
    event_date: str
    local_time: str
    format: str
    source_url: str
    location: str
    store_name: str
    store_id: str
    status: str = "CONFIRMED"
    last_seen: str = ""

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def folded(value: str) -> str:
    return normalize(value).casefold()

def ascii_slug(value: str, max_len: int = 52) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = value[:max_len].strip("-")
    return value or "store"

def short_hash(value: str, n: int = 8) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]

def store_id_for(name: str, source_url: str) -> str:
    # FAB's event-search "Details" link points to the store's locator page:
    # /en/locator/<stable-store-slug>/. Prefer that identifier because it
    # survives address formatting changes and avoids same-name store collisions.
    try:
        path = urlparse(source_url).path
        match = re.search(r"/locator/([^/]+)/?", path, re.IGNORECASE)
        if match:
            locator_slug = ascii_slug(match.group(1), 72)
            return locator_slug
    except Exception:
        pass

    return f"{ascii_slug(name)}-{short_hash(folded(name))}"

def event_uid(
    source_url: str,
    title: str,
    event_date: str,
    local_time: str,
    event_format: str,
) -> str:
    # The FAB Details link identifies the store, not an individual recurring
    # tournament. Date/time/format are therefore required to distinguish weekly
    # Armories and multiple events at one shop on the same day.
    digest = short_hash(
        f"{source_url}|{folded(title)}|{event_date}|{folded(local_time)}|{folded(event_format)}",
        24,
    )
    return f"{digest}@fab-community-calendar"

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    })
    return session

def fetch_soup(session: requests.Session, url: str) -> BeautifulSoup:
    response = session.get(url, timeout=35)
    if not response.ok:
        preview = normalize(response.text[:500])
        raise RuntimeError(
            f"FAB request failed: HTTP {response.status_code} for {response.url}. "
            f"Response preview: {preview!r}"
        )
    return BeautifulSoup(response.text, "html.parser")

def discover_event_types(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """
    Discover FAB's current Tournament Type labels and option values from the
    legacy server-rendered event-search form.
    """
    best: list[tuple[str, str]] = []

    for select in soup.find_all("select"):
        options: list[tuple[str, str]] = []
        labels: list[str] = []

        for option in select.find_all("option"):
            label = normalize(option.get_text(" ", strip=True))
            value = normalize(option.get("value", ""))
            if label:
                labels.append(label)
            if (
                label
                and value
                and folded(label) not in {"any", "any type", "tournament type"}
            ):
                options.append((label, value))

        joined = " ".join(labels).casefold()
        if "armory" in joined and (
            "calling" in joined
            or "pro quest" in joined
            or "world premiere" in joined
        ):
            if len(options) > len(best):
                best = options

    seen = set()
    result: list[tuple[str, str]] = []
    for label, value in best:
        key = (label.casefold(), value)
        if key not in seen:
            seen.add(key)
            result.append((label, value))
    return result

def split_heading(title: str, event_types: list[str]) -> tuple[str, str]:
    """
    Event search headings are normally:
       'Armory Event STORE NAME'
       'Skirmish Season 15 STORE NAME'
       'Calling CITY'
    Longest-prefix matching avoids confusing 'Pro Quest' and 'Pro Quest+'.
    """
    t = normalize(title)
    ft = folded(t)
    for event_type in event_types:
        fe = folded(event_type)
        if ft == fe:
            return event_type, ""
        if ft.startswith(fe + " ") or ft.startswith(fe + ":") or ft.startswith(fe + "-"):
            remainder = t[len(event_type):].strip(" \t:-–—")
            return event_type, remainder

    # Fallback for a newly introduced type before the selector parser catches up.
    return "", t

def parse_date_and_time(text: str, today: date) -> tuple[date | None, str]:
    match = DATE_RE.search(text)
    if not match:
        return None, ""

    day_num = int(match.group(1))
    month_word = match.group(2).casefold()
    month = MONTHS.get(month_word[:4]) or MONTHS.get(month_word[:3])
    if not month:
        return None, ""

    if match.group(3):
        year = int(match.group(3))
    else:
        year = today.year
        try:
            candidate = date(year, month, day_num)
            # FAB's search is an upcoming-events list. A month/day well behind
            # today is therefore most likely in the next calendar year.
            if candidate < today - timedelta(days=45):
                year += 1
        except ValueError:
            return None, ""

    try:
        parsed = date(year, month, day_num)
    except ValueError:
        return None, ""

    return parsed, normalize(match.group(4) or "")

def event_container(heading):
    node = heading
    for _ in range(8):
        parent = node.parent
        if parent is None:
            break
        text = normalize(parent.get_text("\n", strip=True))
        headings = parent.find_all(["h2", "h3"])
        if DATE_RE.search(text) and len(headings) <= 2 and len(text) <= 3200:
            return parent
        node = parent
    return heading.parent or heading

def extract_event_parts(block, title: str) -> tuple[str, str, str]:
    """
    Returns (format, location, details_url).
    """
    raw_lines = [normalize(x) for x in block.get_text("\n", strip=True).splitlines()]
    lines = [x for x in raw_lines if x and x != title]

    event_date_line_idx = None
    event_format = ""
    location = ""

    for idx, line in enumerate(lines):
        if DATE_RE.search(line):
            event_date_line_idx = idx
            # FAB typically prints the format on the same line after the time.
            m = DATE_RE.search(line)
            if m:
                trailing = normalize(line[m.end():])
                event_format = trailing
            break

    if event_date_line_idx is not None:
        for candidate in lines[event_date_line_idx + 1:]:
            low = folded(candidate)
            if low in {"details", "event link", "join event", "public event listing"}:
                continue
            if len(candidate) >= 5 and not DATE_RE.search(candidate):
                location = candidate
                break

    details_url = ""
    detail_link = next(
        (
            a for a in block.find_all("a", href=True)
            if "detail" in folded(a.get_text(" ", strip=True))
        ),
        None,
    )
    if detail_link:
        details_url = detail_link["href"]

    return event_format[:180], location[:600], details_url

def is_global_event(event: FabEvent, config: dict) -> bool:
    hay = f"{folded(event.event_type)} {folded(event.title)}"
    return any(pattern in hay for pattern in config["global_event_patterns"])

def is_store_candidate(event_type: str, remainder: str, config: dict) -> bool:
    if not remainder:
        return False
    et = folded(event_type)
    if any(pattern in et for pattern in config["non_store_event_patterns"]):
        return False
    # This deliberately includes Armories, Skirmishes, prereleases, social play,
    # learn-to-play, Super Armory, ProQuest, RTN, Battlegrounds, etc.
    return True

def parse_page(
    soup: BeautifulSoup,
    page_url: str,
    event_types: list[str],
    config: dict,
) -> list[FabEvent]:
    today = datetime.now(timezone.utc).date()
    events: dict[str, FabEvent] = {}

    for heading in soup.find_all(["h2", "h3"]):
        title = normalize(heading.get_text(" ", strip=True))
        if not title:
            continue

        block = event_container(heading)
        block_text = normalize(block.get_text("\n", strip=True))
        event_date, local_time = parse_date_and_time(block_text, today)
        if not event_date:
            continue

        event_type, remainder = split_heading(title, event_types)
        if not event_type:
            # The H2 may be a page heading rather than an event heading.
            continue

        fmt, location, detail_href = extract_event_parts(block, title)
        source_url = urljoin(page_url, detail_href) if detail_href else page_url

        store_name = remainder if is_store_candidate(event_type, remainder, config) else ""
        store_id = store_id_for(store_name, source_url) if store_name else ""

        item = FabEvent(
            uid=event_uid(source_url, title, event_date.isoformat(), local_time, fmt),
            title=title,
            event_type=event_type,
            event_date=event_date.isoformat(),
            local_time=local_time,
            format=fmt,
            source_url=source_url,
            location=location,
            store_name=store_name,
            store_id=store_id,
            last_seen=now_iso(),
        )
        events[item.uid] = item

    return list(events.values())

def scrape(config: dict) -> tuple[list[FabEvent], list[str]]:
    """
    Scrape FAB's official legacy event search using explicit query parameters.

    The bare legacy URL can fail, while seeded event-search requests remain
    server-rendered. We use Armory (type=2) only to discover the current
    Tournament Type selector, then crawl every discovered event type.
    """
    session = build_session()
    base = config["source_url"]
    mode = config.get("source_mode", "event")
    seed_type = str(config.get("seed_event_type", "2"))
    delay = float(config.get("request_delay_seconds", 0.30))
    max_pages = int(config.get("max_pages_per_type", 150))

    seed_params = {
        "format": "",
        "type": seed_type,
        "query": "",
        "mode": mode,
        "page": 1,
    }
    seed_url = f"{base}?{urlencode(seed_params)}"
    print(f"Loading FAB event-search seed: {seed_url}", flush=True)
    seed_soup = fetch_soup(session, seed_url)

    discovered = discover_event_types(seed_soup)
    if not discovered:
        raise RuntimeError(
            "Could not discover FAB Tournament Type options from the seeded "
            "legacy event search."
        )

    event_type_labels = [label for label, _ in discovered]

    print("Discovered FAB event types:", flush=True)
    for label, value in discovered:
        print(f"  - {label} => {value}", flush=True)

    all_events: dict[str, FabEvent] = {}

    for label, type_value in discovered:
        print(f"Scraping event type: {label}", flush=True)
        seen_page_fingerprints: set[str] = set()

        for page_num in range(1, max_pages + 1):
            params = {
                "format": "",
                "type": type_value,
                "query": "",
                "mode": mode,
                "page": page_num,
            }
            url = f"{base}?{urlencode(params)}"

            if type_value == seed_type and page_num == 1:
                soup = seed_soup
            else:
                soup = fetch_soup(session, url)

            page_events = parse_page(
                soup,
                url,
                event_type_labels,
                config,
            )

            if not page_events:
                print(
                    f"  page {page_num}: no matching events; stopping this type.",
                    flush=True,
                )
                break

            fingerprint = short_hash(
                "|".join(sorted(event.uid for event in page_events)),
                32,
            )
            if fingerprint in seen_page_fingerprints:
                print(
                    f"  page {page_num}: repeated page; stopping this type.",
                    flush=True,
                )
                break
            seen_page_fingerprints.add(fingerprint)

            before = len(all_events)
            for event in page_events:
                all_events[event.uid] = event
            added = len(all_events) - before

            print(
                f"  page {page_num}: {len(page_events)} parsed, {added} new",
                flush=True,
            )
            time.sleep(delay)
        else:
            raise RuntimeError(
                f"Reached max_pages_per_type={max_pages} while scraping "
                f"{label!r}."
            )

    events = sorted(
        all_events.values(),
        key=lambda event: (event.event_date, event.title.casefold()),
    )
    return events, event_type_labels

def load_previous_state() -> tuple[dict[str, FabEvent], list[str]]:
    if not STATE_PATH.exists():
        return {}, []
    raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    events = {
        item["uid"]: FabEvent(**item)
        for item in raw.get("events", [])
    }
    return events, raw.get("event_types", [])

def safety_check(current: list[FabEvent], previous: dict[str, FabEvent], config: dict):
    minimum = int(config.get("minimum_expected_events", 1))
    if len(current) < minimum:
        raise RuntimeError(
            f"Only {len(current)} event(s) found. Refusing to replace published calendars."
        )

    today = datetime.now(timezone.utc).date()
    previous_future = [
        e for e in previous.values()
        if e.status != "CANCELLED" and date.fromisoformat(e.event_date) >= today
    ]
    if not previous_future:
        return

    drop_fraction = 1 - (len(current) / len(previous_future))
    maximum = float(config.get("maximum_drop_fraction", 0.75))
    if drop_fraction > maximum:
        raise RuntimeError(
            f"Event count dropped {drop_fraction:.0%} in one run "
            f"({len(previous_future)} -> {len(current)}). "
            "Likely a FAB markup/source issue; refusing to publish."
        )

def merge_cancellations(
    current: list[FabEvent],
    previous: dict[str, FabEvent],
    config: dict,
) -> list[FabEvent]:
    today = datetime.now(timezone.utc).date()
    retention = int(config.get("cancelled_retention_days", 30))
    merged: dict[str, FabEvent] = {}
    comparable_fields = (
        "title", "event_type", "event_date", "local_time", "format",
        "source_url", "location", "store_name", "store_id", "status"
    )

    for event in current:
        old = previous.get(event.uid)
        if old and all(getattr(old, field) == getattr(event, field) for field in comparable_fields):
            # Keep DTSTAMP/LAST-MODIFIED stable when nothing actually changed.
            event.last_seen = old.last_seen or event.last_seen
        merged[event.uid] = event

    current_ids = set(merged)

    for uid, old in previous.items():
        old_date = date.fromisoformat(old.event_date)

        if (
            uid not in current_ids
            and old.status != "CANCELLED"
            and old_date >= today
        ):
            old.status = "CANCELLED"
            old.last_seen = now_iso()
            merged[uid] = old

        elif (
            uid not in merged
            and old.status == "CANCELLED"
            and old_date >= today - timedelta(days=retention)
        ):
            merged[uid] = old

    return sorted(merged.values(), key=lambda e: (e.event_date, e.title.casefold()))

def calendar_bytes(events: list[FabEvent], calendar_name: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//FAB Community Event Calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", calendar_name)
    cal.add("x-published-ttl", "PT6H")
    cal.add("refresh-interval", "PT6H")

    for item in events:
        ve = Event()
        ve.add("uid", item.uid)
        try:
            revision = datetime.fromisoformat(item.last_seen)
            if revision.tzinfo is None:
                revision = revision.replace(tzinfo=timezone.utc)
        except Exception:
            revision = datetime(2000, 1, 1, tzinfo=timezone.utc)
        ve.add("dtstamp", revision)
        ve.add("last-modified", revision)
        ve.add("summary", item.title)

        # Worldwide FAB pages expose local clock times but not always a reliable
        # IANA timezone. Date-only entries prevent incorrect timezone conversion.
        event_day = date.fromisoformat(item.event_date)
        ve.add("dtstart", event_day)
        ve.add("dtend", event_day + timedelta(days=1))

        ve.add("status", item.status)
        ve.add("transp", "TRANSPARENT")
        if item.location:
            ve.add("location", item.location)

        detail_lines = [
            f"Event type: {item.event_type}" if item.event_type else "",
            f"Local start time: {item.local_time}" if item.local_time else "",
            f"Format: {item.format}" if item.format else "",
            f"Store / organizer: {item.store_name}" if item.store_name else "",
            "",
            f"Official FAB listing: {item.source_url}",
        ]
        ve.add("description", "\n".join(x for x in detail_lines if x or x == ""))
        ve.add("url", item.source_url)
        cal.add_component(ve)

    return cal.to_ical()

def match_category(event: FabEvent, patterns: list[str]) -> bool:
    hay = f"{folded(event.event_type)} {folded(event.title)}"
    return any(pattern in hay for pattern in patterns)

def load_previous_stores() -> list[dict]:
    path = DOCS / "stores.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("stores", [])
    except Exception:
        return []

def build_store_index(events: list[FabEvent], previous_stores: list[dict] | None = None) -> list[dict]:
    """
    Keep previously discovered stores in the directory even when they currently
    have zero upcoming events. This keeps existing subscription URLs alive and
    lets the same feed begin filling again when FAB lists a future event.
    """
    active = [e for e in events if e.status != "CANCELLED" and e.store_id and e.store_name]
    grouped: dict[str, list[FabEvent]] = defaultdict(list)
    for event in active:
        grouped[event.store_id].append(event)

    previous_by_id = {
        store["id"]: store
        for store in (previous_stores or [])
        if store.get("id") and store.get("name")
    }

    store_ids = set(grouped) | set(previous_by_id)
    stores: list[dict] = []

    for store_id in store_ids:
        store_events = grouped.get(store_id, [])
        previous = previous_by_id.get(store_id, {})

        if store_events:
            names = Counter(e.store_name for e in store_events)
            locations = Counter(e.location for e in store_events if e.location)
            official_urls = Counter(
                e.source_url for e in store_events
                if "/locator/" in e.source_url
            )
            name = names.most_common(1)[0][0]
            location = (
                locations.most_common(1)[0][0]
                if locations
                else previous.get("location", "")
            )
            official_url = (
                official_urls.most_common(1)[0][0]
                if official_urls
                else previous.get("official_url", "")
            )
            next_date = min(e.event_date for e in store_events)
        else:
            name = previous.get("name", store_id)
            location = previous.get("location", "")
            official_url = previous.get("official_url", "")
            next_date = ""

        stores.append({
            "id": store_id,
            "name": name,
            "location": location,
            "official_url": official_url,
            "event_count": len(store_events),
            "next_event_date": next_date,
            "combined_feed": f"feeds/stores/{store_id}.ics",
            "store_only_feed": f"feeds/stores-only/{store_id}.ics",
        })

    return sorted(stores, key=lambda s: (s["name"].casefold(), s["location"].casefold()))

def write_feed(path: Path, events: list[FabEvent], name: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(calendar_bytes(events, name))

def clean_old_store_feeds(valid_store_ids: set[str]):
    for directory in [FEEDS / "stores", FEEDS / "stores-only"]:
        directory.mkdir(parents=True, exist_ok=True)
        for file in directory.glob("*.ics"):
            if file.stem not in valid_store_ids:
                file.unlink()

def write_outputs(events: list[FabEvent], config: dict):
    FEEDS.mkdir(parents=True, exist_ok=True)

    global_events = [e for e in events if is_global_event(e, config)]
    write_feed(FEEDS / "all.ics", global_events, config["calendar_name"])
    # Root compatibility URL for easy manual subscription on phones.
    write_feed(DOCS / "all.ics", global_events, config["calendar_name"])

    for category, patterns in config["global_feed_categories"].items():
        subset = [e for e in global_events if match_category(e, patterns)]
        label = {
            "rtn": "FAB Road to Nationals",
            "proquest": "FAB ProQuest",
        }.get(category, f"FAB {category.title()}")
        write_feed(FEEDS / f"{category}.ics", subset, label)

    previous_stores = load_previous_stores()
    stores = build_store_index(events, previous_stores)
    valid_ids = {s["id"] for s in stores}
    clean_old_store_feeds(valid_ids)

    by_store: dict[str, list[FabEvent]] = defaultdict(list)
    for event in events:
        if event.store_id:
            by_store[event.store_id].append(event)

    global_by_uid = {e.uid: e for e in global_events}

    for store in stores:
        store_events = by_store[store["id"]]
        store_name = store["name"]

        write_feed(
            FEEDS / "stores-only" / f"{store['id']}.ics",
            store_events,
            f"FAB at {store_name}",
        )

        combined = dict(global_by_uid)
        for event in store_events:
            combined[event.uid] = event
        combined_events = sorted(
            combined.values(),
            key=lambda e: (e.event_date, e.title.casefold())
        )
        write_feed(
            FEEDS / "stores" / f"{store['id']}.ics",
            combined_events,
            f"FAB Events + {store_name}",
        )

    revisions = [e.last_seen for e in events if e.last_seen]
    generated = max(revisions) if revisions else now_iso()
    (DOCS / "stores.json").write_text(
        json.dumps({"generated_at": generated, "stores": stores}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (DOCS / "status.json").write_text(
        json.dumps({
            "generated_at": generated,
            "event_count": len([e for e in events if e.status != "CANCELLED"]),
            "global_event_count": len([e for e in global_events if e.status != "CANCELLED"]),
            "store_count": len(stores),
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Wrote global feed with {len(global_events)} events.")
    print(f"Wrote {len(stores)} store feed pairs.")

def save_state(events: list[FabEvent], event_types: list[str]):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    revisions = [e.last_seen for e in events if e.last_seen]
    generated = max(revisions) if revisions else now_iso()
    STATE_PATH.write_text(
        json.dumps({
            "generated_at": generated,
            "event_types": event_types,
            "events": [asdict(event) for event in events],
        }, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape and validate without replacing generated files.",
    )
    args = parser.parse_args()

    config = load_config()
    previous, previous_types = load_previous_state()

    current, event_types = scrape(config)
    safety_check(current, previous, config)
    merged = merge_cancellations(current, previous, config)

    print(f"Current scrape: {len(current)} events")
    print(f"After cancellation merge: {len(merged)} events")
    print(f"Stores known/discovered: {len(build_store_index(merged, load_previous_stores()))}")

    if args.dry_run:
        return

    save_state(merged, event_types or previous_types)
    write_outputs(merged, config)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
