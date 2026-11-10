# Vinted Scraper

A synchronous, typed, self-contained HTTP client for Vinted's undocumented internal API.

> The repository also contains a **full Discord bot** (alerts for new listings, price drops,
> item tracking) that can be deployed on Railway: see
> [discord_bot/README.md](discord_bot/README.md).

The library provides a stable interface for monitoring scripts, Discord bots and listing analysis tools. It handles session bootstrap, the CSRF token, retries, backoff, pagination, data normalization and authenticated endpoints.

> **Warning**
>
> The API used by this library is private, undocumented and may change without notice. Use it responsibly, and respect Vinted's terms of service and applicable law. Avoid excessive requests and do not circumvent the service's security mechanisms.

## Features

- Synchronous HTTP client built on [`cloudscraper`](https://github.com/VeNoMouS/cloudscraper).
- Support for many European Vinted domains.
- Automatic session bootstrap:
  - fetches anonymous cookies;
  - extracts the CSRF token from the home page;
  - adds `X-CSRF-Token` automatically.
- Centralized HTTP request handling with:
  - rate limiting;
  - retries;
  - exponential backoff with jitter;
  - handling of `429`, `401`, `403`, `404` and `5xx` errors;
  - automatic CSRF token refresh.
- Generic pagination with deduplication and automatic stop.
- Optional progress bar with `tqdm`.
- Multi-criteria search.
- Conversion of a Vinted search URL into `SearchFilters`.
- Normalization of listings and seller profiles.
- Access to similar items and price history.
- Access to a user's feedback, followers and following.
- Access to reference data: brands, categories, sizes, colors and materials.
- Optional support for authenticated endpoints with a session cookie.
- Download of an item's photos.
- Usable as a context manager.

## Installation

### From source

```bash
git clone https://github.com/noan-hbt/vintify
cd vintify

python -m venv .venv
source .venv/bin/activate
```

On Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

Install the dependencies:

```bash
pip install -r requirements.txt
```

### Dependencies

`requirements.txt` contains:

```txt
cloudscraper>=1.2.71
tqdm>=4.66.0
brotli
```

`tqdm` is optional at runtime. If the package is not installed, progress features are simply disabled.

## Recommended layout

```text
.
├── vinted_scraper.py
├── requirements.txt
├── README.md
└── examples/
    └── search.py
```

The library is self-contained: you can use it by simply copying `vinted_scraper.py` into your project.

## Quick start

```python
from vinted_scraper import Sort, VintedScraper

with VintedScraper(domain="fr") as vinted:
    items = vinted.search_items(
        "nike tn",
        price_to=60,
        order=Sort.NEWEST,
        pages=2,
        normalize=True,
    )

    for item in items:
        print(
            item["id"],
            item["title"],
            item["price"],
            item["currency"],
            item["url"],
        )
```

`search_items()` returns a list. To process results as they arrive and stop paginating early, use `iter_items()`:

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    for item in vinted.iter_items(
        search_text="carhartt",
        price_to=80,
        pages=3,
        normalize=True,
    ):
        print(item["title"])

        # Example:
        # if already_seen(item["id"]):
        #     break
```

## Searching with filters

### Direct filters

```python
from vinted_scraper import Sort, VintedScraper

with VintedScraper(domain="fr") as vinted:
    items = vinted.search_items(
        search_text="veste",
        brand_ids=[362],
        size_ids=[3, 4],
        price_from=20,
        price_to=80,
        currency="EUR",
        order=Sort.NEWEST,
        pages=3,
        normalize=True,
    )
```

Available search parameters include:

- `search_text`
- `catalog_ids`
- `brand_ids`
- `size_ids`
- `status_ids`
- `color_ids`
- `material_ids`
- `country_ids`
- `city_ids`
- `price_from`
- `price_to`
- `currency`
- `is_for_swap`
- `order`

ID lists are automatically converted to the CSV format the API expects.

### Using `SearchFilters`

```python
from vinted_scraper import SearchFilters, VintedScraper

filters = SearchFilters(
    search_text="carhartt",
    price_to=80,
    order="newest_first",
)

with VintedScraper(domain="fr") as vinted:
    for item in vinted.iter_items(
        filters=filters,
        pages=2,
        normalize=True,
    ):
        print(item["title"], item["price"])
```

### Converting a Vinted URL

```python
from vinted_scraper import SearchFilters, VintedScraper

filters = SearchFilters.from_url(
    "https://www.vinted.fr/catalog"
    "?search_text=carhartt"
    "&price_to=80"
    "&order=newest_first"
)

# Can be saved as JSON
payload = filters.to_dict()
print(payload)

with VintedScraper(domain="fr") as vinted:
    items = vinted.search_items(
        filters=filters,
        pages=2,
        normalize=True,
    )
```

Reload filters from a dictionary:

```python
import json

from vinted_scraper import SearchFilters

with open("monitor.json", encoding="utf-8") as file:
    data = json.load(file)

filters = SearchFilters.from_dict(data)
```

## Data normalization

The API can return prices in several formats. The normalization functions produce dictionaries that are more stable and easier to work with.

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    item = vinted.get_item(4_123_456_789, normalize=True)

print(item)
```

Example of a normalized result:

```python
{
    "id": 4123456789,
    "title": "Nike Air Max",
    "description": "...",
    "brand": "Nike",
    "brand_id": 53,
    "size": "42",
    "status": "Très bon état",
    "price": 55.0,
    "total_price": 59.5,
    "currency": "EUR",
    "url": "https://www.vinted.fr/items/4123456789",
    "photo": "https://...",
    "photo_count": 4,
    "favourites": 12,
    "views": 180,
    "user_id": 12345678,
    "user_login": "seller",
    "country": "FR",
    "created_at": "...",
    "updated_at": "...",
    "is_closed": False,
    "promoted": False,
}
```

You can also call the helpers directly:

```python
from vinted_scraper import normalize_item, normalize_user

normalized_item = normalize_item(raw_item)
normalized_user = normalize_user(raw_user)
```

## Items and price analysis

### Fetching an item

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    item = vinted.get_item(4_123_456_789)
    full_item = vinted.get_item_full(4_123_456_789)
```

> **Automatic fallback**: if the detail API returns `404` in anonymous mode (seen on some
> networks), `get_item()` automatically extracts the data from the `Product` JSON-LD block of
> the item's public page: title, description, price, currency, availability
> (`InStock`/`OutOfStock`), brand, image. Fields missing from the JSON-LD (size, seller,
> favourites) are then `None`.

### Similar items

```python
with VintedScraper(domain="fr") as vinted:
    similar_items = vinted.get_similar_items(
        4_123_456_789,
        limit=25,
        normalize=True,
    )

    prices = [
        item["price"]
        for item in similar_items
        if item.get("price") is not None
    ]

    if prices:
        median = sorted(prices)[len(prices) // 2]
        print(f"Median price: {median}")
```

### Price history

```python
with VintedScraper(domain="fr") as vinted:
    history = vinted.get_item_price_history(4_123_456_789)

    for change in history:
        print(change)
```

### Building an item URL

```python
with VintedScraper(domain="fr") as vinted:
    url = vinted.item_url(4_123_456_789, slug="nike-air-max")
    print(url)
```

## Users and sellers

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    user = vinted.get_user(12345678)
    stats = vinted.get_user_stats(12345678)

    items = vinted.get_user_items(
        12345678,
        pages=2,
        normalize=True,
    )

    feedback = vinted.get_user_feedback(
        12345678,
        pages=3,
    )
```

Other available methods:

```python
vinted.get_user_followers(user_id)
vinted.get_user_following(user_id)
vinted.search_users("username")
```

## Brands and reference data

Reference data is cached in memory after the first call.

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    brands = vinted.get_brands()
    categories = vinted.get_categories()
    sizes = vinted.get_sizes()
    colors = vinted.get_colors()
    materials = vinted.get_materials()
    statuses = vinted.get_statuses()

    brand = vinted.find_brand("Carhartt WIP")
    print(brand)
```

Force a cache refresh:

```python
with VintedScraper(domain="fr") as vinted:
    brands = vinted.get_brands(refresh=True)
```

## Authenticated endpoints

Private features, such as messages, require a valid Vinted session cookie.

> Never store your cookie in the code or in the Git repository. Use an environment variable or a secrets manager.

```python
import os

from vinted_scraper import VintedAuthError, VintedScraper

session_cookie = os.environ["VINTED_SESSION"]

with VintedScraper(
    domain="fr",
    session_cookie=session_cookie,
) as vinted:
    try:
        threads = vinted.get_messages(
            user_id=12345678,
            pages=3,
        )

        conversation = vinted.get_messages_with_user(
            me_id=12345678,
            other_user_id=87654321,
        )

    except VintedAuthError:
        print("Cookie expired or access denied.")
```

Setting up the environment:

```bash
export VINTED_SESSION="your_session_cookie"
```

In Windows PowerShell:

```powershell
$env:VINTED_SESSION = "your_session_cookie"
```

## Checkout and DataDome bypass

The checkout flow (build checkout, payment) and the `api.vinted.fr` gateway (pickup points) are
protected by **DataDome**: without a bypass they return `403` (a slider challenge as inline JSON
on `www.vinted.fr`, an HTML block page on `api.vinted.fr`).

### Solving DataDome with Hyper Solutions

The bypass is built in and automatic:

1. Install the Chrome-impersonating TLS client: `pip install curl_cffi`.
2. Pass your Hyper Solutions API key (<https://docs.hypersolutions.co>) to the constructor
   (`hypersolutions_api_key=` or the `HYPERSOLUTIONS_API_KEY` environment variable).
3. Your browser's `datadome` cookie (optional but recommended): required by the
   `api.vinted.fr` gateway. It goes in the `cookie_file`.

```python
from vinted_scraper import VintedScraper

with VintedScraper(
    domain="fr",
    cookie_file="vinted_session.json",      # session + JWT + browser datadome cookie
    hypersolutions_api_key=os.environ["HYPER_API_KEY"],
) as v:
    # 1. transaction (created automatically if needed)
    t = v.create_transaction([item_id], seller_id)
    # 2. build the checkout (solves the DataDome challenge automatically)
    co = v.build_checkout(t["id"], purchase_type="transaction")
    checkout_id = co["checkout"]["id"]
    # 3. pickup points around the address (api.vinted.fr gateway)
    pts = v.get_nearby_pickup_points(checkout_id, latitude, longitude)
    # 4. select a point (rate_uuid must match the carrier)
    v.select_pickup_point(
        checkout_id,
        rate_uuid=pts["shipping_rates"][0]["rate_uuid"],
        point_code=pts["shipping_points"][0]["point"]["code"],
        point_uuid=pts["shipping_points"][0]["point"]["uuid"],
    )
    # 5. payment — ⚠️ really charges the card: confirm=True is required
    co = v.get_checkout(checkout_id)["checkout"]
    v.pay_checkout(checkout_id, checksum=co["checksum"], card_id=card_id, confirm=True)
```

Things to watch:

- The `checksum` changes on every `update_checkout`: read the checkout again before paying.
- `point_code`/`point_uuid` must belong to the carrier of the chosen `rate_uuid` (a Chronopost
  point cannot be selected with a Mondial Relay rate).
- The browser's `datadome` cookie expires (about 30 min to 2 h): copy it again along with the
  session cookies when the gateway refuses (`403 access_denied`).
- The gateway rejects the `Authorization` (Bearer) header: `get_nearby_pickup_points` handles
  this automatically.

## Downloading photos

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    downloaded = vinted.download_item_images(
        item_id=4_123_456_789,
        directory="images",
        overwrite=False,
        progress=True,
    )

for path in downloaded:
    print(path)
```

Files are saved in this layout:

```text
images/
└── 4123456789/
    ├── 01.jpg
    ├── 02.jpg
    └── 03.jpg
```

## Progress bar

Progress is disabled by default:

```python
with VintedScraper(domain="fr") as vinted:
    items = vinted.search_items(
        "nike",
        pages=None,
        progress=True,
    )
```

If `tqdm` is not installed, `progress=True` does not raise an error.

## Supported domains

| Code | Domain | Locale |
|---|---|---|
| `fr` | `vinted.fr` | `fr-FR` |
| `be` | `vinted.be` | `fr-BE` |
| `nl` | `vinted.nl` | `nl-NL` |
| `de` | `vinted.de` | `de-DE` |
| `at` | `vinted.at` | `de-AT` |
| `es` | `vinted.es` | `es-ES` |
| `it` | `vinted.it` | `it-IT` |
| `pt` | `vinted.pt` | `pt-PT` |
| `pl` | `vinted.pl` | `pl-PL` |
| `cz` | `vinted.cz` | `cs-CZ` |
| `sk` | `vinted.sk` | `sk-SK` |
| `lt` | `vinted.lt` | `lt-LT` |
| `lv` | `vinted.lv` | `lv-LV` |
| `se` | `vinted.se` | `sv-SE` |
| `fi` | `vinted.fi` | `fi-FI` |
| `dk` | `vinted.dk` | `da-DK` |
| `ro` | `vinted.ro` | `ro-RO` |
| `hu` | `vinted.hu` | `hu-HU` |
| `gr` | `vinted.gr` | `el-GR` |
| `lu` | `vinted.lu` | `fr-LU` |
| `ie` | `vinted.ie` | `en-IE` |
| `co.uk` | `vinted.co.uk` | `en-GB` |
| `com` | `vinted.com` | `en-US` |

Example:

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="de") as vinted:
    items = vinted.search_items("jacke", pages=1)
```

## Client configuration

```python
from vinted_scraper import VintedScraper

vinted = VintedScraper(
    domain="fr",
    timeout=30,
    retries=5,
    min_request_interval=0.25,
    auto_refresh_csrf=True,
    verbose=True,
)
```

Main parameters:

| Parameter | Description | Default |
|---|---|---:|
| `domain` | Target Vinted domain | `fr` |
| `session_cookie` | Session cookie for private endpoints | `None` |
| `timeout` | HTTP timeout in seconds | `30` |
| `retries` | Number of extra attempts | `5` |
| `verbose` | Enables detailed logs | `False` |
| `proxies` | Proxy mapping passed to the HTTP session | `None` |
| `min_request_interval` | Minimum delay between two requests | `0.25` |
| `auto_refresh_csrf` | Refreshes the CSRF token automatically after a refusal | `True` |
| `bootstrap` | Initializes the session automatically | `True` |

To skip the automatic bootstrap:

```python
vinted = VintedScraper(
    domain="fr",
    bootstrap=False,
)

vinted.refresh_session()
```

## Pagination

Paginated methods accept a `pages` parameter.

```python
with VintedScraper(domain="fr") as vinted:
    first_page = vinted.search_items("nike", pages=1)
    three_pages = vinted.search_items("nike", pages=3)
    all_pages = vinted.search_items("nike", pages=None)
```

With `pages=None` or `pages=0`, pagination continues until results run out, with a safety limit of `200` pages.

`page_size` is automatically capped at `96`, the largest size the client supports.

## Error handling

The public exceptions are:

| Exception | When |
|---|---|
| `VintedError` | Generic error |
| `VintedAuthError` | Invalid cookie, expired CSRF token or access denied |
| `VintedNotFoundError` | Resource does not exist or was deleted |
| `VintedRateLimitError` | Rate limit hit after all retries |
| `VintedResponseError` | Non-JSON or unreadable JSON response |

Example:

```python
from vinted_scraper import (
    VintedAuthError,
    VintedNotFoundError,
    VintedRateLimitError,
    VintedScraper,
)

with VintedScraper(domain="fr") as vinted:
    try:
        item = vinted.get_item(4_123_456_789)

    except VintedNotFoundError:
        print("Item not found.")

    except VintedRateLimitError:
        print("Too many requests. Try again later.")

    except VintedAuthError:
        print("Access denied or session expired.")
```

All exceptions inherit from `VintedError` and also expose:

- `status`
- `url`
- `body`
- `message`

## Command line

The file can be run directly:

```bash
python vinted_scraper.py nike \
    --domain fr \
    --pages 2 \
    --price-to 60 \
    --verbose
```

Available options:

```text
usage: vinted_scraper.py [-h] [--domain DOMAIN] [--pages PAGES]
                         [--price-to PRICE_TO] [--verbose]
                         [query]

Quick search on Vinted.
```

## Logging

The library uses the `vinted_scraper` logger.

By default, no handler is added to the calling application:

```python
import logging

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("vinted_scraper")
logger.setLevel(logging.DEBUG)
```

Or turn on the built-in logs:

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr", verbose=True) as vinted:
    vinted.search_items("nike", pages=1)
```

## Public API

The main exported objects and functions are:

```python
from vinted_scraper import (
    KNOWN_DOMAINS,
    SearchFilters,
    Sort,
    VintedAuthError,
    VintedError,
    VintedNotFoundError,
    VintedRateLimitError,
    VintedResponseError,
    VintedScraper,
    normalize_item,
    normalize_user,
)
```

### `VintedScraper`

#### Catalog

- `iter_items()`
- `search_items()`
- `get_item()`
- `get_item_full()`
- `get_similar_items()`
- `get_item_price_history()`
- `item_url()`

#### Users

- `get_user()`
- `get_user_stats()`
- `iter_user_items()`
- `get_user_items()`
- `iter_user_feedback()`
- `get_user_feedback()`
- `get_user_followers()`
- `get_user_following()`
- `search_users()`

#### Reference data

- `get_catalog_initializers()`
- `get_categories()`
- `get_brands()`
- `get_sizes()`
- `get_colors()`
- `get_materials()`
- `get_statuses()`
- `find_brand()`

#### Messages

- `get_messages()`
- `get_thread()`
- `get_messages_with_user()`

#### Media and session

- `download_item_images()`
- `refresh_session()`
- `close()`

## Design notes

- `iter_*` methods are lazy and suited to monitoring.
- `get_*` methods usually return a complete list.
- Paginated results are deduplicated by ID when possible.
- Requests are spaced out by a global throttle.
- `429` errors honor the `Retry-After` header when it is present.
- Network errors and `5xx` responses are retried automatically.
- A single automatic CSRF refresh is attempted after a `401` or `403`.
- Catalog initialization data is cached in memory.
- The client can be used with `with` so the HTTP session is closed cleanly.

## Known limitations

- The Vinted API is not public and may change its endpoints or response formats.
- Some endpoints may be unavailable depending on the domain, the account or the IP address.
- Private endpoints require a valid session cookie.
- In anonymous mode, the item detail API (`/api/v2/items/{id}`) can return `404`: `get_item()`
  then falls back to the public page's JSON-LD (see "Fetching an item").
- Reference data endpoints (`initializers`, `catalogs`) can be blocked in anonymous mode:
  `get_brands()`, `get_categories()`, `get_sizes()`… raise an error in that case.
- The library is synchronous. In an async application such as `discord.py`, use for example `asyncio.to_thread()` to avoid blocking the event loop.
- `tqdm` is only needed to display progress bars.
- Anti-abuse mechanisms may throttle or block requests that are too frequent.

## Ideas for later

- On-disk cache for reference data, with expiry.
- Proxy rotation.
- Async client based on `httpx.AsyncClient`.
- Metrics on request count, `429` errors and latency.
- Test suite with mocked HTTP responses.
- Dedicated typed models for listings, users and feedback.

## License

MIT License
