#!/usr/bin/env python3
"""Competitor price and stock tracker.

Pulls product data from configured stores, stores a snapshot per run and
reports what changed since last time.

    python tracker.py --init
    python tracker.py --run
    python tracker.py --report
    python tracker.py --export-csv
    python tracker.py --export-sheets
"""

import argparse
import csv
import json
import logging
import os
import re
import smtplib
import sqlite3
import sys
import time
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse

BASE_DIR = Path(__file__).resolve().parent

# Everything we want to survive a redeploy lives under DATA_DIR.
# Relative paths resolve against the script so cron can run us from anywhere.
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
if not DATA_DIR.is_absolute():
    DATA_DIR = (BASE_DIR / DATA_DIR).resolve()

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH") or BASE_DIR / "config.json")
DB_PATH = DATA_DIR / "tracker.db"
LOG_DIR = DATA_DIR / "logs"
EXPORT_DIR = DATA_DIR / "exports"
REPORT_DIR = DATA_DIR / "reports"

TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
MAX_PAGES = 20
SHOPIFY_PAGE_LIMIT = 250

# Free gifts and service fees are priced at 0 and show up as 100% off,
# which pushes real deals out of the top list.
TOP_LIST_SIZE = 10
TOP_LIST_MIN_PRICE = 1.0
TOP_LIST_MAX_DISCOUNT = 100.0

# Telegram caps a message at 4096 chars.
TELEGRAM_LIMIT = 3900
TELEGRAM_CHUNK_DELAY = 0.5

# Credentials can come from the environment instead of the config file so
# they stay out of the image and out of version control.
ENV_OVERRIDES = {
    "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
    "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
    "SMTP_HOST": ("smtp", "host"),
    "SMTP_PORT": ("smtp", "port"),
    "SMTP_USER": ("smtp", "user"),
    "SMTP_PASSWORD": ("smtp", "password"),
    "SMTP_TO": ("smtp", "to"),
    "GOOGLE_CREDENTIALS_FILE": ("google_sheets", "credentials_file"),
    "GOOGLE_SPREADSHEET_ID": ("google_sheets", "spreadsheet_id"),
}

DEFAULTS = {
    "drop_threshold_percent": 5.0,
    "rise_threshold_percent": 10.0,
    "min_absolute_change": 1.0,
    "request_delay_seconds": 2.0,
    "user_agent": "PriceTracker/1.0",
}

KIND_LABELS = {
    "price_drop": "PRICE DROP",
    "price_rise": "PRICE RISE",
    "out_of_stock": "OUT OF STOCK",
    "back_in_stock": "BACK IN STOCK",
    "new_product": "NEW PRODUCT",
    "removed": "REMOVED",
    "on_sale": "ON SALE",
    "off_sale": "OFF SALE",
}

log = logging.getLogger("tracker")


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(LOG_DIR / "tracker.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))

    log.setLevel(logging.INFO)
    log.addHandler(file_handler)
    log.addHandler(console)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit(f"config not found: {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    for key, value in DEFAULTS.items():
        cfg.setdefault(key, value)
    cfg.setdefault("smtp", {})
    cfg.setdefault("telegram", {})
    cfg.setdefault("google_sheets", {})
    cfg.setdefault("stores", [])

    for name, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(name)
        if value:
            cfg[section][key] = value

    return cfg


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    type     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY,
    store_id    INTEGER NOT NULL REFERENCES stores(id),
    external_id TEXT NOT NULL,
    title       TEXT NOT NULL,
    product_title TEXT,
    variant_title TEXT,
    url         TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE (store_id, external_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id               INTEGER PRIMARY KEY,
    product_id       INTEGER NOT NULL REFERENCES products(id),
    price            REAL,
    compare_at_price REAL,
    currency         TEXT,
    in_stock         INTEGER,
    captured_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    kind       TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    created_at TEXT NOT NULL,
    notified   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_snapshots_product ON snapshots(product_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_alerts_notified   ON alerts(notified);
CREATE INDEX IF NOT EXISTS idx_products_store    ON products(store_id, last_seen);
"""

# Columns added after the first release, applied to existing databases.
MIGRATIONS = [
    ("snapshots", "compare_at_price", "REAL"),
    ("products", "product_title", "TEXT"),
    ("products", "variant_title", "TEXT"),
]


def ensure_schema(conn):
    with conn:
        conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            existing = {row["name"] for row in
                        conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                log.info("added column %s.%s", table, column)


def is_on_sale(price, compare_at_price):
    return (compare_at_price is not None and price is not None
            and compare_at_price > price)


def discount_percent(price, compare_at_price):
    if not is_on_sale(price, compare_at_price):
        return None
    return (compare_at_price - price) / compare_at_price * 100.0


def cmd_init():
    conn = connect()
    ensure_schema(conn)
    conn.close()
    for directory in (LOG_DIR, EXPORT_DIR, REPORT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    log.info("database ready: %s", DB_PATH)


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------

def fetch(client, url, user_agent):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.get(url, headers={"User-Agent": user_agent},
                                  timeout=TIMEOUT_SECONDS, follow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                wait = 2 ** (attempt - 1)
                log.warning("request failed (%d/%d) %s: %s, retrying in %ss",
                            attempt, MAX_RETRIES, url, exc, wait)
                time.sleep(wait)
    raise last_error


def robots_allows(client, base_url, path, user_agent):
    """Check robots.txt for the given path.

    We fetch the file ourselves instead of using RobotFileParser.read().
    That helper sends urllib's own user agent, gets a 403 from bot
    protection and then treats the 403 as "everything is disallowed",
    which silently skips stores that never blocked us in the first place.
    """
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        response = client.get(robots_url, headers={"User-Agent": user_agent},
                              timeout=TIMEOUT_SECONDS, follow_redirects=True)
    except Exception as exc:
        log.warning("could not read %s (%s), assuming allowed", robots_url, exc)
        return True

    if response.status_code in (401, 403):
        log.warning("robots.txt is not readable (HTTP %d): %s, skipping store",
                    response.status_code, robots_url)
        return False
    if response.status_code >= 400:
        return True

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(response.text.splitlines())
    return parser.can_fetch(user_agent, urljoin(base_url, path))


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

def parse_price(raw):
    """Turn '1.234,56 TL', '$1,234.56' or '19.90' into a float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = re.sub(r"[^\d.,]", "", str(raw))
    if not text:
        return None

    if "," in text and "." in text:
        # whichever separator comes last is the decimal one
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", "." if len(text.split(",")[-1]) == 2 else "")

    try:
        return float(text)
    except ValueError:
        return None


def shopify_items(client, store, user_agent, delay):
    """Walk /products.json and yield one record per variant."""
    base_url = store["base_url"].rstrip("/")
    currency = store.get("currency") or ""
    items = []
    seen_pages = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{base_url}/products.json?limit={SHOPIFY_PAGE_LIMIT}&page={page}"
        products = fetch(client, url, user_agent).json().get("products", [])
        if not products:
            break

        # Some stores have moved to cursor pagination and ignore ?page,
        # handing back the same variants forever.
        signature = frozenset(
            str(variant.get("id"))
            for product in products
            for variant in product.get("variants", [])
        )
        if signature in seen_pages:
            log.warning("%s: page %d repeats an earlier page, stopping",
                        store["name"], page)
            break
        seen_pages.add(signature)

        for product in products:
            handle = product.get("handle", "")
            product_title = (product.get("title") or "").strip()

            for variant in product.get("variants", []):
                variant_title = (variant.get("title") or "").strip()
                if variant_title and variant_title.lower() != "default title":
                    title = f"{product_title} - {variant_title}"
                else:
                    title = product_title

                items.append({
                    "external_id": str(variant.get("id")),
                    "title": title,
                    "product_title": product_title,
                    "variant_title": variant_title,
                    "url": f"{base_url}/products/{handle}?variant={variant.get('id')}",
                    "price": parse_price(variant.get("price")),
                    "compare_at_price": parse_price(variant.get("compare_at_price")),
                    "currency": currency,
                    "in_stock": 1 if variant.get("available") else 0,
                })

        if len(products) < SHOPIFY_PAGE_LIMIT:
            break
        time.sleep(delay)
    else:
        log.warning("%s: hit the %d page limit", store["name"], MAX_PAGES)

    return items


def html_items(client, store, user_agent, delay):
    """Scrape individual product pages using CSS selectors from the config."""
    from selectolax.parser import HTMLParser

    selectors = store.get("selectors", {})
    stock_keywords = [k.lower() for k in store.get(
        "in_stock_keywords", ["in stock", "available"])]
    currency = store.get("currency") or ""
    items = []

    for index, url in enumerate(store.get("product_urls", [])):
        if index:
            time.sleep(delay)
        try:
            tree = HTMLParser(fetch(client, url, user_agent).text)
        except Exception as exc:
            log.error("%s: could not fetch %s: %s", store["name"], url, exc)
            continue

        def text_of(selector):
            if not selector:
                return None
            node = tree.css_first(selector)
            return node.text(strip=True) if node else None

        title = text_of(selectors.get("title"))
        if not title:
            log.warning("%s: no title found, skipping %s", store["name"], url)
            continue

        stock_text = text_of(selectors.get("stock"))
        if stock_text is None:
            in_stock = None
        else:
            in_stock = 1 if any(k in stock_text.lower() for k in stock_keywords) else 0

        items.append({
            "external_id": url,
            "title": title,
            "product_title": title,
            "variant_title": "",
            "url": url,
            "price": parse_price(text_of(selectors.get("price"))),
            "compare_at_price": parse_price(text_of(selectors.get("compare_at_price"))),
            "currency": currency,
            "in_stock": in_stock,
        })

    return items


# ---------------------------------------------------------------------------
# change detection
# ---------------------------------------------------------------------------

def latest_snapshot(conn, product_id):
    return conn.execute(
        "SELECT price, compare_at_price, currency, in_stock FROM snapshots "
        "WHERE product_id = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
        (product_id,),
    ).fetchone()


def add_alert(conn, product_id, kind, old_value, new_value, run_ts):
    conn.execute(
        "INSERT INTO alerts (product_id, kind, old_value, new_value, created_at, "
        "notified) VALUES (?, ?, ?, ?, ?, 0)",
        (product_id, kind, old_value, new_value, run_ts),
    )


def detect_changes(conn, cfg, product_id, title, previous, current, run_ts):
    # Nothing to compare against on the first sighting, so stay quiet.
    if previous is None:
        return

    old_price, old_currency = previous["price"], previous["currency"]
    new_price, new_currency = current["price"], current["currency"]

    if old_price is not None and new_price is not None:
        if (old_currency or "") != (new_currency or ""):
            log.warning("currency changed (%s to %s), skipping price comparison "
                        "for %s", old_currency, new_currency, title)
        elif old_price > 0:
            delta = new_price - old_price
            percent = abs(delta) / old_price * 100.0
            # Both the percentage and the absolute threshold have to be met,
            # otherwise cheap items generate noise on every cent of movement.
            if abs(delta) >= float(cfg["min_absolute_change"]):
                if delta < 0 and percent >= float(cfg["drop_threshold_percent"]):
                    add_alert(conn, product_id, "price_drop",
                              f"{old_price:.2f}", f"{new_price:.2f}", run_ts)
                elif delta > 0 and percent >= float(cfg["rise_threshold_percent"]):
                    add_alert(conn, product_id, "price_rise",
                              f"{old_price:.2f}", f"{new_price:.2f}", run_ts)

    old_stock, new_stock = previous["in_stock"], current["in_stock"]
    if old_stock is not None and new_stock is not None and old_stock != new_stock:
        kind = "back_in_stock" if new_stock else "out_of_stock"
        add_alert(conn, product_id, kind, str(old_stock), str(new_stock), run_ts)

    was_on_sale = is_on_sale(old_price, previous["compare_at_price"])
    now_on_sale = is_on_sale(new_price, current["compare_at_price"])
    if was_on_sale != now_on_sale:
        if now_on_sale:
            # Store the list price as the old value so the report can show
            # the discount rather than the day-over-day move.
            add_alert(conn, product_id, "on_sale",
                      f"{current['compare_at_price']:.2f}", f"{new_price:.2f}", run_ts)
        elif old_price is not None and new_price is not None:
            add_alert(conn, product_id, "off_sale",
                      f"{old_price:.2f}", f"{new_price:.2f}", run_ts)


def compile_exclusions(store):
    patterns = []
    for pattern in store.get("exclude_patterns", []):
        try:
            patterns.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            log.error("%s: ignoring invalid exclude pattern %r (%s)",
                      store.get("name", "?"), pattern, exc)
    return patterns


def matches_exclusion(patterns, title, url):
    haystack = f"{title} {url or ''}"
    return any(pattern.search(haystack) for pattern in patterns)


def purge_excluded(conn, store_id, patterns):
    """Delete stored products that match the store's exclude patterns.

    Some stores publish internal SKUs through the same catalogue endpoint,
    for example a per-order replacement fee carrying a timestamp in its
    variant title. Those churn daily and drown out the real changes.

    Purging matters as much as the filtering itself: without it, adding a
    pattern would make every already stored match look like it vanished
    from the catalogue and fire a removal alert for each one.
    """
    if not patterns:
        return 0

    rows = conn.execute(
        "SELECT id, title, url FROM products WHERE store_id = ?", (store_id,)
    ).fetchall()
    ids = [(row["id"],) for row in rows
           if matches_exclusion(patterns, row["title"], row["url"])]
    if not ids:
        return 0

    conn.executemany("DELETE FROM alerts WHERE product_id = ?", ids)
    conn.executemany("DELETE FROM snapshots WHERE product_id = ?", ids)
    conn.executemany("DELETE FROM products WHERE id = ?", ids)
    return len(ids)


def flag_removed_products(conn, store_id, run_ts):
    """Alert on products the current run did not see.

    Only fires once per disappearance. If the product comes back its
    last_seen is refreshed, so a later removal can trigger again.
    """
    rows = conn.execute(
        "SELECT id, title, last_seen FROM products "
        "WHERE store_id = ? AND last_seen != ?",
        (store_id, run_ts),
    ).fetchall()

    count = 0
    for row in rows:
        already_reported = conn.execute(
            "SELECT 1 FROM alerts WHERE product_id = ? AND kind = 'removed' "
            "AND created_at >= ? LIMIT 1",
            (row["id"], row["last_seen"]),
        ).fetchone()
        if already_reported:
            continue
        add_alert(conn, row["id"], "removed", row["title"], None, run_ts)
        count += 1
    return count


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def upsert_store(conn, store):
    conn.execute(
        "INSERT INTO stores (name, base_url, type) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET base_url = excluded.base_url, "
        "type = excluded.type",
        (store["name"], store.get("base_url", ""), store.get("type", "shopify")),
    )
    return conn.execute("SELECT id FROM stores WHERE name = ?",
                        (store["name"],)).fetchone()["id"]


def process_store(conn, client, cfg, store, run_ts):
    name = store["name"]
    store_type = store.get("type", "shopify")
    user_agent = cfg["user_agent"]
    delay = float(cfg["request_delay_seconds"])

    base_url = store.get("base_url") or ""
    if not base_url and store.get("product_urls"):
        parsed = urlparse(store["product_urls"][0])
        base_url = f"{parsed.scheme}://{parsed.netloc}"

    probe_path = "/products.json" if store_type == "shopify" else "/"
    if base_url and not robots_allows(client, base_url, probe_path, user_agent):
        log.warning("%s: disallowed by robots.txt, skipped", name)
        return {"store": name, "skipped": True, "items": 0, "removed": 0, "alerts": 0}

    store_id = upsert_store(conn, dict(store, base_url=base_url))

    exclusions = compile_exclusions(store)
    purged = purge_excluded(conn, store_id, exclusions)
    if purged:
        log.info("%s: dropped %d stored items matching exclude_patterns",
                 name, purged)

    had_previous_run = conn.execute(
        "SELECT COUNT(*) AS c FROM products WHERE store_id = ?", (store_id,)
    ).fetchone()["c"] > 0

    if store_type == "shopify":
        items = shopify_items(client, dict(store, base_url=base_url), user_agent, delay)
    elif store_type == "html":
        items = html_items(client, store, user_agent, delay)
    else:
        raise ValueError(f"unknown store type: {store_type}")

    if exclusions:
        kept = [item for item in items
                if not matches_exclusion(exclusions, item["title"], item["url"])]
        if len(kept) != len(items):
            log.info("%s: skipped %d fetched items matching exclude_patterns",
                     name, len(items) - len(kept))
        items = kept

    alerts_before = conn.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"]

    for item in items:
        row = conn.execute(
            "SELECT id FROM products WHERE store_id = ? AND external_id = ?",
            (store_id, item["external_id"]),
        ).fetchone()

        if row is None:
            cursor = conn.execute(
                "INSERT INTO products (store_id, external_id, title, "
                "product_title, variant_title, url, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (store_id, item["external_id"], item["title"],
                 item.get("product_title"), item.get("variant_title"),
                 item["url"], run_ts, run_ts),
            )
            product_id = cursor.lastrowid
            previous = None
            # On a store's first run the whole catalogue is "new", so only
            # report new products once we have something to compare against.
            if had_previous_run:
                add_alert(conn, product_id, "new_product", None, item["title"], run_ts)
        else:
            product_id = row["id"]
            previous = latest_snapshot(conn, product_id)
            conn.execute(
                "UPDATE products SET title = ?, product_title = ?, "
                "variant_title = ?, url = ?, last_seen = ? WHERE id = ?",
                (item["title"], item.get("product_title"),
                 item.get("variant_title"), item["url"], run_ts, product_id),
            )

        conn.execute(
            "INSERT INTO snapshots (product_id, price, compare_at_price, currency, "
            "in_stock, captured_at) VALUES (?, ?, ?, ?, ?, ?)",
            (product_id, item["price"], item.get("compare_at_price"),
             item["currency"], item["in_stock"], run_ts),
        )
        detect_changes(conn, cfg, product_id, item["title"], previous, item, run_ts)

    removed = flag_removed_products(conn, store_id, run_ts)
    alerts_after = conn.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"]

    return {
        "store": name,
        "skipped": False,
        "items": len(items),
        "removed": removed,
        "alerts": alerts_after - alerts_before,
    }


def cmd_run():
    try:
        import httpx
    except ImportError:
        raise SystemExit("httpx is required: pip install httpx")

    cfg = load_config()
    conn = connect()
    ensure_schema(conn)

    run_ts = now_iso()
    delay = float(cfg["request_delay_seconds"])
    log.info("run started at %s, %d stores", run_ts, len(cfg["stores"]))

    results = []
    with httpx.Client() as client:
        for index, store in enumerate(cfg["stores"]):
            if index:
                time.sleep(delay)
            try:
                # One transaction per store, so a failure halfway through
                # does not leave a partial snapshot behind.
                with conn:
                    result = process_store(conn, client, cfg, store, run_ts)
                results.append(result)
                if not result["skipped"]:
                    log.info("%s: %d products, %d alerts (%d removed)",
                             result["store"], result["items"], result["alerts"],
                             result["removed"])
            except Exception as exc:
                log.exception("%s: failed, continuing with the next store (%s)",
                              store.get("name", "?"), exc)

    log.info("run finished, %d new alerts", sum(r["alerts"] for r in results))

    # A sheets failure should not fail the run, the data is already stored.
    try:
        push_to_sheets(cfg, conn)
    except Exception as exc:
        log.error("google sheets step raised: %s", exc)

    conn.close()


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------

def split_message(text, limit=TELEGRAM_LIMIT):
    """Split text on line boundaries, hard-wrapping any oversized line."""
    chunks, current = [], ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_mail(cfg, subject, body):
    smtp = cfg.get("smtp", {})
    if not smtp.get("host") or not smtp.get("to"):
        log.info("no smtp config, skipping mail")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp.get("user") or f"tracker@{smtp['host']}"
    message["To"] = smtp["to"]
    message.set_content(body)

    port = int(smtp.get("port", 587))
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(smtp["host"], port, timeout=TIMEOUT_SECONDS)
        else:
            server = smtplib.SMTP(smtp["host"], port, timeout=TIMEOUT_SECONDS)
        with server:
            if port != 465:
                server.starttls()
            if smtp.get("user"):
                server.login(smtp["user"], smtp.get("password", ""))
            server.send_message(message)
    except Exception as exc:
        log.error("could not send mail: %s", exc)
        return False

    log.info("mail sent to %s", smtp["to"])
    return True


def send_telegram(cfg, text):
    telegram = cfg.get("telegram", {})
    token, chat_id = telegram.get("bot_token"), telegram.get("chat_id")
    if not token or not chat_id:
        log.info("no telegram config, skipping")
        return False

    try:
        import httpx
    except ImportError:
        log.error("httpx is required for telegram, skipping")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = split_message(text)
    try:
        with httpx.Client() as client:
            for index, chunk in enumerate(chunks, 1):
                if index > 1:
                    time.sleep(TELEGRAM_CHUNK_DELAY)
                response = client.post(url, timeout=TIMEOUT_SECONDS, json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                })
                if response.status_code != 200:
                    log.error("telegram %d/%d failed (HTTP %d): %s",
                              index, len(chunks), response.status_code,
                              response.text[:200])
                    return False
    except Exception as exc:
        log.error("could not send telegram message: %s", exc)
        return False

    log.info("telegram sent to chat %s in %d message(s)", chat_id, len(chunks))
    return True


ALERT_HISTORY_DAYS = 30

PRICE_SHEET_HEADER = ["Store", "Product", "Variant", "Price", "Compare At",
                      "Discount %", "In Stock", "Last Updated", "URL"]
ALERT_SHEET_HEADER = ["Date", "Store", "Product", "Variant", "Change",
                      "From", "To", "URL"]


def price_sheet_rows(conn):
    rows = conn.execute(
        "SELECT s.name AS store, p.title, p.product_title, p.variant_title, "
        "       p.url, p.last_seen, "
        "       sn.price, sn.compare_at_price, sn.in_stock "
        "FROM products p "
        "JOIN stores s ON s.id = p.store_id "
        "LEFT JOIN snapshots sn ON sn.id = ("
        "    SELECT id FROM snapshots WHERE product_id = p.id "
        "    ORDER BY captured_at DESC, id DESC LIMIT 1) "
        "ORDER BY s.name, p.title"
    ).fetchall()

    values = [PRICE_SHEET_HEADER]
    for row in rows:
        percent = discount_percent(row["price"], row["compare_at_price"])
        in_stock = "" if row["in_stock"] is None else (
            "Yes" if row["in_stock"] else "No")
        values.append([
            row["store"],
            # product_title is empty for rows stored before the column existed
            row["product_title"] or row["title"],
            row["variant_title"] or "",
            row["price"] if row["price"] is not None else "",
            row["compare_at_price"] if row["compare_at_price"] is not None else "",
            round(percent, 1) if percent is not None else "",
            in_stock,
            row["last_seen"],
            row["url"] or "",
        ])
    return values


def alert_sheet_rows(conn, days=ALERT_HISTORY_DAYS):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds")
    rows = conn.execute(
        "SELECT a.created_at, a.kind, a.old_value, a.new_value, "
        "       s.name AS store, p.title, p.product_title, p.variant_title, p.url "
        "FROM alerts a "
        "JOIN products p ON p.id = a.product_id "
        "JOIN stores   s ON s.id = p.store_id "
        "WHERE a.created_at >= ? "
        "ORDER BY a.created_at DESC, a.id DESC",
        (cutoff,),
    ).fetchall()

    values = [ALERT_SHEET_HEADER]
    for row in rows:
        values.append([
            row["created_at"],
            row["store"],
            row["product_title"] or row["title"],
            row["variant_title"] or "",
            KIND_LABELS.get(row["kind"], row["kind"]),
            row["old_value"] or "",
            row["new_value"] or "",
            row["url"] or "",
        ])
    return values


def write_worksheet(spreadsheet, name, values):
    """Replace a worksheet's contents. Rewritten in full every run so the
    sheet is a mirror of the database rather than an ever growing log."""
    import gspread

    columns = max(len(row) for row in values)
    try:
        worksheet = spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=name, rows=len(values) + 100,
                                              cols=columns)
    worksheet.clear()
    # clear() empties the cells but keeps the grid, which can be too small
    worksheet.resize(rows=max(len(values), 2), cols=columns)
    worksheet.update(values=values, range_name="A1")
    worksheet.freeze(rows=1)
    worksheet.format(f"A1:{chr(ord('A') + columns - 1)}1",
                     {"textFormat": {"bold": True}})
    return len(values) - 1


def push_to_sheets(cfg, conn):
    settings = cfg.get("google_sheets", {})
    credentials_file = settings.get("credentials_file")
    spreadsheet_id = settings.get("spreadsheet_id")

    if not credentials_file or not spreadsheet_id:
        log.info("no google sheets config, skipping")
        return False

    if not Path(credentials_file).exists():
        log.error("service account file not found: %s", credentials_file)
        return False

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        log.error("gspread and google-auth are required for sheets, skipping")
        return False

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        credentials = Credentials.from_service_account_file(
            credentials_file, scopes=scopes)
        spreadsheet = gspread.authorize(credentials).open_by_key(spreadsheet_id)

        prices = write_worksheet(spreadsheet, settings.get("worksheet_name", "Prices"),
                                 price_sheet_rows(conn))
        alerts = write_worksheet(spreadsheet, "Alerts", alert_sheet_rows(conn))
    except Exception as exc:
        log.error("could not update google sheets: %s", exc)
        return False

    log.info("google sheets updated: %d products, %d alerts from the last %d days",
             prices, alerts, ALERT_HISTORY_DAYS)
    return True


def notify(cfg, subject, report):
    """Send through every configured channel, one failure at a time."""
    channels = (
        ("mail", lambda: send_mail(cfg, subject, report)),
        ("telegram", lambda: send_telegram(cfg, report)),
    )
    for name, send in channels:
        try:
            send()
        except Exception as exc:
            log.error("%s channel raised: %s", name, exc)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def current_state(conn):
    """Latest snapshot per product, limited to each store's most recent run."""
    return conn.execute(
        "SELECT s.name AS store, p.title, p.url, "
        "       sn.price, sn.compare_at_price, sn.currency "
        "FROM products p "
        "JOIN stores s ON s.id = p.store_id "
        "LEFT JOIN snapshots sn ON sn.id = ("
        "    SELECT id FROM snapshots WHERE product_id = p.id "
        "    ORDER BY captured_at DESC, id DESC LIMIT 1) "
        "WHERE p.last_seen = ("
        "    SELECT MAX(last_seen) FROM products WHERE store_id = p.store_id) "
        "ORDER BY s.name, p.title"
    ).fetchall()


def is_promotional_sku(price, percent):
    return price < TOP_LIST_MIN_PRICE or percent >= TOP_LIST_MAX_DISCOUNT


def build_summary(rows):
    lines = ["CURRENT STATE", "-" * 64]
    if not rows:
        lines += ["  No products yet, run 'python tracker.py --run' first.", ""]
        return lines

    by_store = {}
    for row in rows:
        by_store.setdefault(row["store"], []).append(row)

    lines.append(f"  {'Store':<28}{'Items':>7}{'On sale':>12}{'Share':>8}{'Avg off':>10}")

    on_sale_all = []
    for store_name, store_rows in by_store.items():
        discounts = [d for d in (discount_percent(r["price"], r["compare_at_price"])
                                 for r in store_rows) if d is not None]
        share = len(discounts) / len(store_rows) * 100 if store_rows else 0
        average = sum(discounts) / len(discounts) if discounts else 0
        lines.append(f"  {store_name[:27]:<28}{len(store_rows):>7}{len(discounts):>12}"
                     f"{share:>7.1f}%{average:>9.1f}%")
        on_sale_all += [
            (discount_percent(r["price"], r["compare_at_price"]), r)
            for r in store_rows
            if is_on_sale(r["price"], r["compare_at_price"])
        ]

    all_discounts = [percent for percent, _ in on_sale_all]
    total_share = len(all_discounts) / len(rows) * 100 if rows else 0
    total_average = sum(all_discounts) / len(all_discounts) if all_discounts else 0
    lines.append(f"  {'TOTAL':<28}{len(rows):>7}{len(all_discounts):>12}"
                 f"{total_share:>7.1f}%{total_average:>9.1f}%")
    lines.append("")

    # The store level numbers above stay unfiltered, the ranking does not.
    listed = [pair for pair in on_sale_all
              if not is_promotional_sku(pair[1]["price"], pair[0])]
    excluded = len(on_sale_all) - len(listed)

    if listed:
        listed.sort(key=lambda pair: pair[0], reverse=True)
        top = listed[:TOP_LIST_SIZE]
        lines.append(f"  BIGGEST DISCOUNTS ({len(top)})")
        for percent, row in top:
            currency = f" {row['currency']}" if row["currency"] else ""
            lines.append(f"    -{percent:<5.1f}% {row['title'][:46]}")
            lines.append(f"           {row['compare_at_price']:.2f} -> "
                         f"{row['price']:.2f}{currency}   [{row['store']}]")
            if row["url"]:
                lines.append(f"           {row['url']}")
    elif on_sale_all:
        lines.append("  Every discounted item was filtered out as promotional.")
    else:
        lines.append("  Nothing on sale right now.")

    if excluded:
        lines.append(f"  ({excluded} promotional SKU excluded)")
    lines.append("")
    return lines


def build_report(rows, state_rows):
    lines = [
        "COMPETITOR PRICE AND STOCK REPORT",
        f"Generated: {now_iso()}",
        "=" * 64,
        "",
    ]
    lines += build_summary(state_rows)
    lines += ["=" * 64, f"UNREPORTED CHANGES ({len(rows)})", ""]

    if not rows:
        lines += ["  Nothing new.", ""]
        return "\n".join(lines)

    by_store = {}
    for row in rows:
        by_store.setdefault(row["store_name"], []).append(row)

    for store_name, store_rows in by_store.items():
        lines.append(f"## {store_name}  ({len(store_rows)})")
        for row in store_rows:
            label = KIND_LABELS.get(row["kind"], row["kind"])
            detail = ""
            if row["kind"] in ("price_drop", "price_rise", "on_sale", "off_sale"):
                old_price = float(row["old_value"])
                new_price = float(row["new_value"])
                percent = (new_price - old_price) / old_price * 100 if old_price else 0
                detail = f"{old_price:.2f} -> {new_price:.2f} ({percent:+.1f}%)"
            lines.append(f"  [{label}] {row['title']}")
            if detail:
                lines.append(f"      {detail}")
            if row["url"]:
                lines.append(f"      {row['url']}")
        lines.append("")

    return "\n".join(lines)


def cmd_report():
    cfg = load_config()
    conn = connect()
    rows = conn.execute(
        "SELECT a.id, a.kind, a.old_value, a.new_value, a.created_at, "
        "       p.title, p.url, s.name AS store_name "
        "FROM alerts a "
        "JOIN products p ON p.id = a.product_id "
        "JOIN stores   s ON s.id = p.store_id "
        "WHERE a.notified = 0 "
        "ORDER BY s.name, a.kind, a.id"
    ).fetchall()

    # The summary is always current, only the change list depends on notified.
    report = build_report(rows, current_state(conn))
    print(report)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.txt"
    with report_path.open("w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    log.info("report written to %s", report_path)

    if not rows:
        log.info("no unreported changes, nothing sent")
        conn.close()
        return

    notify(cfg, f"Price and stock changes: {len(rows)} alerts", report)

    with conn:
        conn.executemany("UPDATE alerts SET notified = 1 WHERE id = ?",
                         [(row["id"],) for row in rows])
    log.info("marked %d alerts as notified", len(rows))
    conn.close()


def cmd_export_csv():
    conn = connect()
    rows = conn.execute(
        "SELECT s.name AS store, p.external_id, p.title, p.url, "
        "       p.first_seen, p.last_seen, "
        "       sn.price, sn.compare_at_price, sn.currency, sn.in_stock, "
        "       sn.captured_at "
        "FROM products p "
        "JOIN stores s ON s.id = p.store_id "
        "LEFT JOIN snapshots sn ON sn.id = ("
        "    SELECT id FROM snapshots WHERE product_id = p.id "
        "    ORDER BY captured_at DESC, id DESC LIMIT 1) "
        "ORDER BY s.name, p.title"
    ).fetchall()
    conn.close()

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EXPORT_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.csv"
    columns = ["store", "external_id", "title", "url", "price", "compare_at_price",
               "discount_percent", "currency", "in_stock", "captured_at",
               "first_seen", "last_seen"]

    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            percent = discount_percent(row["price"], row["compare_at_price"])
            values = {column: row[column] for column in columns
                      if column != "discount_percent"}
            values["discount_percent"] = f"{percent:.1f}" if percent is not None else ""
            writer.writerow([values[column] for column in columns])

    log.info("csv written to %s (%d rows)", out_path, len(rows))


def cmd_export_sheets():
    conn = connect()
    push_to_sheets(load_config(), conn)
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Competitor price and stock tracker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true",
                       help="create the database")
    group.add_argument("--run", action="store_true",
                       help="fetch every store and take a snapshot")
    group.add_argument("--report", action="store_true",
                       help="report unnotified changes and send them out")
    group.add_argument("--export-csv", action="store_true", dest="export_csv",
                       help="dump the current state to csv")
    group.add_argument("--export-sheets", action="store_true", dest="export_sheets",
                       help="push the current state to google sheets")
    args = parser.parse_args()

    setup_logging()
    if args.init:
        cmd_init()
    elif args.run:
        cmd_run()
    elif args.report:
        cmd_report()
    elif args.export_csv:
        cmd_export_csv()
    elif args.export_sheets:
        cmd_export_sheets()


if __name__ == "__main__":
    main()
