# UK running-shoe deal alerts

`scanner.py` checks UK running shops for watched shoes that are **new, in stock in UK 8.5 or
UK 9, and at least 40% below the reference price**, and emails a digest of new offers.
GitHub Actions runs it every 3 hours between 06:00 and 22:00 UTC (`.github/workflows/scan.yml`).
A run takes about 4 minutes.

## Sources

| Source | How per-size stock is read |
|---|---|
| Start Fitness, Pro:Direct Running, Run4It, Achilles Heel, Up & Running, The Running Outlet | Shopify `products.json`: one variant per size with an `available` flag |
| Nike UK | Nike product feed: GB `localizedSize` (UK) per SKU plus `availableSkus` |
| SportsShoes.com | Listing data plus `inv.sportsshoes.com` stock count per UK-size SKU |
| Runners Need | Site search JSON; only in-stock sizes are listed, as `UK 8.5` |
| Northern Runner | Per-product options call with `has_stock` / `stock_level` per `UK x` size |
| Vinted UK | Men's shoes filtered to UK 8.5/9 and "new with/without tags" |
| eBay UK (incl. auctions) | Browse API, only when `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` secrets exist (untested) |

Not covered because of bot protection: adidas.co.uk, asics.com, on.com, JD Sports,
Sports Direct, Decathlon.

## What it watches

- The models in `MODELS` (adidas, Nike, ASICS list), each with an approximate UK RRP.
- Every On shoe.
- Other super shoes by name (`SUPER_SHOES`: Alphafly, Vaporfly, Adios Pro, Metaspeed, Endorphin
  Elite/Pro, Deviate Nitro Elite, Rocket X, Cielo X, Hyperion Elite, Rebellion Pro, ...).
- Women's-labelled, kids', trail and spike products are skipped (`EXCLUDE_WOMENS`, `EXCLUDE_TITLE`).

## How the size check works

The scanner reads the size of every individual size variant and accepts only labels that parse
as UK 8.5 or UK 9 (`UK 8.5`, `8.5 UK`, `9.0`, `US 10 / UK 9`, ...). A label that gives only a
US, EU or cm size is rejected, so a US 9 is never taken for a UK 9 (Nike's US `nikeSize` is
never used). An offer is reported only if that exact size is in stock.

For Vinted and eBay the size is the seller's own entry. Listings whose title states a different
UK size, or only a US/EU size ("US 8.5", "9US"), are dropped.

## Discount

Reference price = the higher of the configured RRP and the shop's own "was" price. For models
with no RRP configured (and On or other super shoes), the shop's was-price is used, and for listed
models without an RRP the highest full price seen at any scanned shop. Discount = 1 - price /
reference. Settings: `MIN_DISCOUNT` (shops) and `MARKETPLACE_MIN_DISCOUNT` (Vinted/eBay).

## Repeat alerts

An item (shop + size) is emailed once. It is emailed again only if its price falls by a further
3%, or it comes back in stock after being unavailable for 48 hours. State is stored in
`state/state.json` and committed by the workflow. The first email lists everything that
currently qualifies, so it is longer than later ones.

## Email

Repository secrets: `SMTP_USER` (Gmail address), `SMTP_PASSWORD` (Gmail app password),
`ALERT_TO` (recipient). The scanner makes one login attempt per run. If Gmail rejects the login
it writes `state/SMTP_DISABLED` and sends nothing until that file is deleted, so a wrong password
cannot cause repeated login attempts.

## Running locally

    pip install -r requirements.txt
    python scanner.py --report                 # every watched shoe in stock in UK 8.5/9
    python scanner.py --dry-run                # offers that would be emailed; nothing sent or saved
    python scanner.py --report --only nike vinted

## Adding a shop

Shopify shops: add the domain to `SHOPIFY_SHOPS` (check that `https://<domain>/products.json`
returns JSON), optionally with collection handles to limit the crawl. Shopify rate-limits by IP
across all its stores, so shops are crawled one after another.
