# Competitor Price Tracker

Tracks prices and stock levels across competitor storefronts, keeps the full
history in SQLite and reports what changed since the last run.

The value is in the history. A single scrape tells you what a competitor
charges today; a few months of snapshots tell you how often they discount, how
deep they go and which products they never touch.

## How it works

Most Shopify stores expose `/products.json`, a public endpoint that returns the
full catalogue with variants, prices, `compare_at_price` and stock availability.
No HTML parsing, no anti-bot measures. Stores that are not on Shopify can be
configured with CSS selectors instead.

Each run writes a new snapshot row per variant and never deletes anything.
Changes are found by comparing the new snapshot against the previous one:

- price drops and rises
- items going out of stock or coming back
- products added to or removed from the catalogue
- items going on sale or off sale

A product only produces alerts once there is something to compare it to, so the
first run against a store is silent instead of dumping the entire catalogue into
your inbox.

## Setup

```
pip install httpx
python tracker.py --init
```

Edit `config.json` to point at the stores you care about, then:

```
python tracker.py --run            # fetch everything, take a snapshot
python tracker.py --report         # report changes, send notifications
python tracker.py --export-csv     # dump current state to csv
python tracker.py --export-sheets  # push current state to google sheets
```

There is no scheduler in the code. Use cron, Task Scheduler or whatever your
host provides:

```
0  6 * * *  cd /opt/tracker && python tracker.py --run
15 6 * * *  cd /opt/tracker && python tracker.py --report
```

Run `--report` after `--run`, otherwise the day's changes wait for tomorrow.

## Configuration

```json
{
  "drop_threshold_percent": 5,
  "rise_threshold_percent": 10,
  "min_absolute_change": 1.0,
  "request_delay_seconds": 2,
  "stores": [
    { "name": "Some Brand", "type": "shopify",
      "base_url": "https://somebrand.com", "currency": "USD" }
  ]
}
```

An alert needs both thresholds satisfied: the move has to be large enough as a
percentage *and* in absolute terms. Without the absolute floor a $2 accessory
generates an alert every time it moves by ten cents.

Drops and rises get separate thresholds because they are not equally
interesting. A competitor cutting prices matters more than one raising them.

### Filtering out internal SKUs

Some stores publish more than their catalogue through `/products.json`. Shady
Rays, for example, creates a "Automated Replacement - Processing Fee" product
for every replacement request, with a timestamp in the variant title. Those
churn constantly: dozens appear and disappear daily. Left alone they accounted
for 77% of that store's product count and drowned the change report in
meaningless additions and removals.

`exclude_patterns` takes case-insensitive regular expressions matched against
the product title and URL:

```json
{
  "name": "Shady Rays",
  "type": "shopify",
  "base_url": "https://shadyrays.com",
  "currency": "USD",
  "exclude_patterns": ["^Automated Replacement"]
}
```

Matching items are never stored, so they stay out of alerts and out of the
per-store counts. Adding a pattern also deletes anything already stored that
matches it, which is what keeps the change afterwards from reading as a mass
removal.

Non-Shopify stores use selectors:

```json
{
  "name": "Some Shop", "type": "html",
  "base_url": "https://someshop.com",
  "product_urls": ["https://someshop.com/product/1"],
  "selectors": {
    "title": "h1.product-title",
    "price": ".price",
    "compare_at_price": ".price--compare",
    "stock": ".availability"
  }
}
```

This path needs `pip install selectolax`.

### Notifications

Mail and Telegram are both optional and independent. Leave `smtp.host` or
`telegram.bot_token` empty and that channel is skipped; if one fails the other
still goes out. Reports longer than Telegram's message limit are split across
several messages.

Credentials can come from the environment instead of the config file, which is
what you want when the config is committed or baked into an image:

```
TELEGRAM_BOT_TOKEN   TELEGRAM_CHAT_ID
SMTP_HOST   SMTP_PORT   SMTP_USER   SMTP_PASSWORD   SMTP_TO
```

Anything set here overrides the matching field in `config.json`. Empty values
are ignored, so an unset variable falls back to the file.

### Google Sheets

`--run` can mirror the database into a spreadsheet, which is a more convenient
place to sort and filter than a CSV in a container:

```json
"google_sheets": {
  "credentials_file": "credentials.json",
  "spreadsheet_id": "1AbC...xyz",
  "worksheet_name": "Prices"
}
```

Two worksheets get written. `Prices` holds every tracked product with its
current price, list price, discount, stock and last seen timestamp. `Alerts`
holds the last 30 days of detected changes. Both are cleared and rewritten on
every run, so the sheet is a mirror of the database rather than a log that
grows forever.

Leave `credentials_file` empty and the step is skipped, same as the other
channels. `--export-sheets` pushes without running a fetch, which is useful for
checking the setup.

Getting a service account:

1. In the Google Cloud console create a project and enable the Google Sheets
   API for it.
2. Create a service account, then create a JSON key for it and download the
   file. This file is a credential, keep it out of the repository.
3. Open the target spreadsheet and share it with the service account's email
   address (it looks like `name@project.iam.gserviceaccount.com`) with edit
   permission. Without this step every call comes back as not found, because
   the service account genuinely cannot see the file.
4. The `spreadsheet_id` is the long identifier in the sheet's URL, between
   `/d/` and `/edit`.

Needs `pip install gspread google-auth`.

## Output

Everything lands under `DATA_DIR` (defaults to the script directory):

```
tracker.db                  full history
exports/YYYY-MM-DD.csv      current state of every product
reports/YYYY-MM-DD.txt      change report
logs/tracker.log            errors and warnings
```

A report has two halves. The top is a current snapshot of the market: product
counts per store, what share of each catalogue is discounted, average discount
depth and the ten deepest cuts. The bottom lists changes that have not been
reported yet, which is what actually gets emailed.

```
CURRENT STATE
----------------------------------------------------------------
  Store                         Items     On sale   Share   Avg off
  Blenders Eyewear               2218          83    3.7%     32.9%
  Knockaround                     802         177   22.1%     39.0%
  Shady Rays                      997          26    2.6%     34.9%
  goodr                           606           0    0.0%      0.0%
  TOTAL                          4623         286    6.2%     36.9%

  BIGGEST DISCOUNTS (10)
    -64.3 % Exclusive Offer - Men's Mystery Polarized Pair
           70.00 -> 25.00 USD   [Shady Rays]
  ...
  (2 promotional SKU excluded)

================================================================
UNREPORTED CHANGES (4)

## Blenders Eyewear  (1)
  [OUT OF STOCK] Pretty Gangsta
      https://www.blenderseyewear.com/products/romeo-pretty-gangsta?variant=29389887963205

## goodr  (2)
  [BACK IN STOCK] Incognito Plant Parent - G00448-CG-01
      https://goodr.com/products/incognito-plant-parent?variant=41171712409658
  ...
```

Free gifts and service fees are priced at zero, which reads as 100% off and
crowds out real deals, so they are dropped from the ranking. The per-store
numbers stay unfiltered.

## Notes on scraping politely

- `robots.txt` is checked before every store and the fetch uses the configured
  user agent. `RobotFileParser.read()` is deliberately not used: it sends
  urllib's own user agent, collects a 403 from bot protection and then reports
  the entire site as disallowed, which silently skips stores that never blocked
  anything.
- Requests are spaced by `request_delay_seconds` and retried three times with
  exponential backoff on failure.
- Pagination stops at 20 pages. Some stores have moved to cursor pagination and
  ignore `?page`, returning the same variants forever; that is detected by
  comparing variant IDs between pages.
- A store that fails does not stop the others. Each store runs in its own
  transaction, so a mid-run failure leaves no partial snapshot.

Login-protected pages, personal data and sites that forbid scraping in their
terms are out of scope.

## Deployment

The container writes to `DATA_DIR=/data`. **Mount a volume there.** Without it
the database is recreated on every deploy, the accumulated history is gone for
good, and the next run silently becomes a baseline that reports nothing.

`docker-compose.yml` defines `tracker_data:/data` for this. The service itself
runs `sleep infinity` and does no work; the actual jobs are scheduled tasks that
exec into the running container:

```
python tracker.py --run
python tracker.py --report
```

The `config.json` in this repository is committed without secrets because the
Dockerfile copies it at build time. Supply the real credentials as environment
variables instead:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
GOOGLE_CREDENTIALS_FILE=/data/credentials.json
GOOGLE_SPREADSHEET_ID=...
```

The service account key is a file rather than a value, so put it on the volume
next to the database and point `GOOGLE_CREDENTIALS_FILE` at it.

If you would rather keep the whole config outside the image, put a filled-in
copy on the volume at `/data/config.json` and set `CONFIG_PATH=/data/config.json`.

Standalone, without an orchestrator:

```
docker build -t price-tracker .
docker run --rm -v tracker_data:/data price-tracker python tracker.py --run
```
