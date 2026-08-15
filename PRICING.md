# Pricing & Currency

This document explains how the `pricing` block in the Phase 4 output
(`data/output/<category>_final.jsonl`) is produced, and why the numbers look
the way they do.

## TL;DR

- **Prices are kept in each distributor's native currency** (USD / GBP / SGD),
  **exactly as datasheets.com's pricing API returns them.**
- **No currency conversion is performed.** We do not generate INR (or any other
  display currency) ourselves.
- **Price breaks are kept as the API returns them.** The only rows we drop are
  the API's dummy padding rows (quantity `0` and/or price `0.00000`).

## Where the prices come from

For every product, Phase 4 calls the site's own pricing endpoint:

```
https://www.datasheets.com/api/part-pricing?pn=<partNumber>&manufacturer=<manufacturer>
```

Each offer in that response carries:

- a `currency` field — the distributor's native currency, e.g. `GBP` (Farnell),
  `USD` (Newark / Avnet), `SGD` (Element14);
- a `pricing` array of quantity tiers, e.g. `{"break": "100", "price": "0.244"}`.

We store those values **as-is**. The `unitPrice` and `currency` you see in the
output are the distributor's own numbers — not something we computed.

Example (part `PMEG3020EP,115`, Nexperia):

```jsonc
{
  "distributorName": "Farnell",
  "priceBreaks": [
    { "quantity": 100,  "unitPrice": 0.244, "currency": "GBP" },
    { "quantity": 500,  "unitPrice": 0.182, "currency": "GBP" },
    { "quantity": 1000, "unitPrice": 0.163, "currency": "GBP" }
  ]
}
```

## Why we do NOT convert to INR

The datasheets.com UI has a currency picker (e.g. India → INR) that converts the
native prices for display. That conversion is done **by the site, with the
site's own FX rate, rounding, and timing** — it is not part of the underlying
data.

An earlier version of this scraper reproduced that behaviour by fetching live FX
rates from a third-party API and converting every price to INR. That produced
two problems:

1. **The INR values never matched the site.** We used a different FX source at a
   different instant than datasheets.com, so our converted numbers drifted from
   what the site's INR toggle showed — even when the native price was identical.
2. **The numbers were self-generated**, i.e. not something the website actually
   gives us. That is exactly what we want to avoid in the output.

So the FX-conversion step has been removed entirely. If a consumer of this data
needs INR, they should convert the native prices themselves with a rate and
timestamp they control.

## Why there can be more price breaks than the website shows

The **visible** price table on a product page usually shows only a few tiers
(e.g. `100 / 500 / 1000`). The **pricing API** behind that page often returns
*more* quantity tiers than the page renders (e.g. Newark's `250 / 2500 / 5000`,
or Avnet's full `3000 → 48000` ladder).

We intentionally **keep every real tier the API returns**, because those tiers
are genuine distributor data — the site's UI simply hides some of them. So the
output may legitimately contain more price breaks than you see on the page.

## What we drop

The API pads offers with dummy rows — quantity `0`, or price `0.00000`. These
are filtered out (`_clean_price_breaks` in `phase4_pdp.py`) with the rule:

```
drop any break where quantity < 1 or unitPrice <= 0
```

This is why, for example, an offer whose API tiers are all `0.00000`
(e.g. "Avnet Asia" above) comes through with an empty `priceBreaks` list, and
why Avnet stops at `48000` even though the API listed higher quantities at
price `0`.

## Fields, at a glance

Each entry in `pricing[]`:

| field                | source                                          |
| -------------------- | ----------------------------------------------- |
| `distributorName`    | pricing API / on-page table                     |
| `distributorPartUrl` | pricing API / on-page table                     |
| `stock`              | pricing API / on-page table                     |
| `minimumOrderQuantity` | pricing API / on-page table                   |
| `packageType`        | pricing API / on-page table                     |
| `priceBreaks[].quantity`  | pricing API (native), dummy rows removed   |
| `priceBreaks[].unitPrice` | pricing API (native), **not converted**    |
| `priceBreaks[].currency`  | pricing API (distributor's native currency) |
