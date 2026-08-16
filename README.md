# Game Collection Pipeline

Tools for turning photos of physical game cases into a durable game inventory.

The workflow is intentionally review-friendly:

1. Lay cases out on the floor and take one or more high-resolution photos.
2. Extract candidate titles from the photo.
3. Match candidates to a game metadata provider.
4. Review uncertain matches.
5. Import confirmed games into the local collection database.
6. Track play status, keep history for sold games, and avoid rebuying games you already finished.

## Metadata Providers

The pipeline keeps your personal ownership/play/sale state locally in SQLite and treats public databases as replaceable metadata sources.

Recommended starting point:

- `thegamesdb`: open API with game metadata and artwork. Requires `THEGAMESDB_API_KEY`.
- `igdb`: rich metadata and cover art through Twitch client-credentials auth. Requires `IGDB_CLIENT_ID` and `IGDB_CLIENT_SECRET`.
- `rawg`: broad game metadata and images. Requires `RAWG_API_KEY`; personal/hobby use requires attribution.

Other candidates worth knowing about:

- LaunchBox has strong community metadata and media inside LaunchBox, but currently does not expose an official public Games Database API.

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

game-collection init
game-collection add-manual "Demon's Souls" --platform "PlayStation 5" --status owned --played completed
game-collection list
```

Start the browsable library interface:

```bash
game-collection serve
```

Then open http://127.0.0.1:8765.

To use the app from an iPhone on the same network, bind the server to your LAN interface:

```bash
game-collection serve --host 0.0.0.0
```

Find the Mac's Ethernet or Wi-Fi IP address, then open `http://<your-ip>:8765` on the phone. In Safari, use Share -> Add to Home Screen. The web UI includes iOS Home Screen metadata, touch icons, safe-area spacing, mobile table labels, and a thumb-friendly bottom navigation bar when opened on a small screen.

For metadata lookup:

```bash
export THEGAMESDB_API_KEY=...
game-collection search "Demon's Souls" --platform "PlayStation 5" --provider thegamesdb
```

For IGDB:

```bash
cp .env.example .env
# Edit .env and set IGDB_CLIENT_ID / IGDB_CLIENT_SECRET.
game-collection credentials check --provider igdb
game-collection search "Demon's Souls" --platform "PlayStation 5" --provider igdb
```

### Twitch / IGDB Credential Setup

IGDB uses Twitch developer credentials through the client-credentials flow.

1. Sign in at the Twitch Developer Console.
2. Enable two-factor authentication on the Twitch account if it is not already enabled.
3. Register a new application.
4. Use a name like `personal-game-collection`.
5. Set the OAuth Redirect URL to `localhost`; IGDB does not use it for this flow.
6. Set Client Type to `Confidential`.
7. Create the app, open Manage, and copy the Client ID.
8. Generate a new Client Secret and put it in `.env`.

Your `.env` should look like this:

```bash
IGDB_CLIENT_ID=your_client_id_here
IGDB_CLIENT_SECRET=your_client_secret_here
```

Do not commit `.env`; it is ignored by this repo.

## Adding New Games From Photos

### 1. Take the photos

Lay the physical cases flat on the floor or a table with the front covers visible.

You can put multiple boxes in a single image. The workflow is designed for this, and a good target is 12 to 30 cases per photo. Use more photos rather than one crowded image when:

- cases overlap,
- titles are too small to read when zoomed in,
- glossy covers create glare,
- the camera angle makes the boxes look heavily skewed.

Best results:

- Use bright, even light.
- Keep all cases fully visible.
- Leave a little space between cases.
- Hold the camera as parallel to the floor/table as possible.
- Take the highest-resolution image your phone makes practical.
- If a spine or cover is hard to read, take a second closer photo of that group.

### 2. Open the local web UI

Install the project dependencies:

```bash
python -m pip install -e .
```

Start the local interface:

```bash
game-collection serve
```

Open http://127.0.0.1:8765 and choose `Scan`.

### 3. Upload and ingest

Use the upload form to choose one back-cover barcode photo. On iOS, the native picker lets you select from the photo library or take a new picture with the camera. The web ingest workflow assumes one physical game per scan. You do not need to place files in `photos/incoming/` or manage review files yourself.

Set:

- platform from the cached IGDB platform picklist.

The web upload workflow uses IGDB metadata caches internally, so there is no provider selector on the upload form.

The importer scans uploaded photos for barcodes and UPC/EAN codes. Exact matches come from a local ignored CSV at `review/barcodes/catalog.csv`. Use `examples/barcode-catalog.example.csv` as the format reference.

Then tap `Scan`.

Prioritized platform presets:

- `PlayStation 5`
- `PlayStation 4`
- `Xbox One`
- `Xbox Series X|S`

Use `Xbox Series X|S` for Xbox Series X games because that is the IGDB platform name used for cached metadata.

The upload platform selector only shows platforms that already have local cached metadata. Use `Cache Settings` to choose platforms and build complete local indexes for title autocomplete and cover art display.

If a newer game imports before IGDB has cover art, keep the game in the library and refresh it later from `Cache Settings` -> `Library Art` -> `Refresh`. That action re-queries IGDB for library games with missing cover URLs, downloads newly available cover art into the local platform cache, and updates the game metadata in `collection.sqlite3`.

When the server starts, it caches the IGDB platform list and starts prebuilding complete cover-art indexes for the prioritized systems in the background. You can force a refresh with:

```bash
game-collection serve --refresh-platform-cache --refresh-cover-indexes
```

Or skip startup prebuilding for a quick launch:

```bash
game-collection serve --skip-cover-prebuild
```

The web UI handles the choreography:

- stores uploaded photos in an ignored local run folder,
- scans uploaded photos for barcodes,
- matches decoded codes against a local platform barcode cache,
- falls back to live no-key barcode lookups when the local cache misses,
- shows database cover art for barcode/title matches when cached metadata is available,
- lets you correct the matched title from cached platform metadata,
- imports only rows you manually accept.

Barcode matches are treated as exact code matches. The web workflow checks the local cache first, then tries public live lookup sources for the scanned UPC/EAN, including PriceCharting's public barcode redirect, upc.dev product lookup, and Open Products Facts. If no live source resolves the code, the browser review page prompts for title and platform.

### Barcode caches

Barcode identity is cached separately from IGDB metadata because IGDB is not a complete UPC database. The cache is CSV based so you can import known UPC/SKU exports from other sources without committing private library data.

The cache can cover any gaming platform that appears in the source barcode data. Modern retail platforms usually use UPC-A/GTIN-12 in North America and EAN/JAN/GTIN-13 internationally; older platforms may have no barcode, inconsistent regional labels, or incomplete public records.

The committed example is:

```text
examples/barcode-catalog.example.csv
```

Local caches live under ignored paths:

```text
review/barcodes/catalog.csv
review/barcodes/<platform-slug>/catalog.csv
```

Build or rebuild the cache from one or more local CSV files or folders:

```bash
game-collection build-barcode-cache --source examples/barcode-catalog.example.csv
```

CSV URLs are also accepted:

```bash
game-collection build-barcode-cache --source "https://example.com/video-game-barcodes.csv"
```

Limit output to specific platforms:

```bash
game-collection build-barcode-cache --source data/my-barcodes/ --platform "Nintendo Switch" --platform "PlayStation 5"
```

PriceCharting-style exports are supported when you have access to a CSV download:

```bash
game-collection build-barcode-cache --source data/pricecharting.csv --source-provider pricecharting
```

No-subscription public connectors are available for sources that expose a public API or download endpoint:

```bash
# Open-data export from Wikidata video game records that have GTINs.
game-collection download-barcode-source wikidata-video-games --out review/barcode-sources/wikidata-video-games.csv

# Public product search from upc.dev.
game-collection download-barcode-source upcdev-search --query "Nintendo Switch game" --out review/barcode-sources/upcdev-switch.csv

# Public lookup of known barcodes from upc.dev.
game-collection download-barcode-source upcdev-product --barcode 045496905651 --out review/barcode-sources/upcdev-known.csv

# Public lookup of known barcodes from Open Products Facts.
game-collection download-barcode-source open-products-facts --barcode 045496905651 --out review/barcode-sources/open-products-facts-known.csv

# Normalize a public CSV URL.
game-collection download-barcode-source csv-url --url "https://example.com/video-game-barcodes.csv" --out review/barcode-sources/example.csv
```

Then build the cache from those downloaded CSVs:

```bash
game-collection build-barcode-cache --source review/barcode-sources/
```

Use `--incremental` to merge newly downloaded rows into an existing source CSV. For Wikidata, `--limit` and `--offset` can be used for page-sized refreshes.

The browser `Cache Settings` page also has a `Barcode Sources` form that downloads these public sources into `review/barcode-sources/` and rebuilds local barcode caches in one action.

CSV columns are:

```text
barcode,title,platform,provider,provider_game_id,release_date,developer,publisher,description,cover_url
```

The importer also recognizes common external column names such as `upc`, `ean`, `gtin`, `product-name`, `console-name`, and `release-date`.

The scanner accepts valid GS1 GTIN-8, GTIN-12/UPC-A, GTIN-13/EAN/JAN, and GTIN-14 values. Platform hints rank common publisher/manufacturer prefixes first, such as Nintendo `045496`/`4902370`, PlayStation `711719`/`4948872`, Xbox `885370`/`889842`, Sega `010086`/`4974365`, Capcom `013388`, Electronic Arts `014633`, Activision `047875`, Ubisoft `008888`, Square Enix `662248`, Take-Two `710425`, Warner `883929`, and Limited Run `812303`, but exact importing still requires a cached barcode row.

Known source constraints:

- Wikidata can be queried without an API key and its structured data is CC0, but it only contains barcode rows that contributors have added. The connector intentionally uses a direct `instance of video game` query so the public SPARQL service does not time out; subclass edge cases may need CSV import or manual rows.
- PriceCharting exposes UPC lookup and paid CSV downloads that can seed broad multi-platform caches.
- MobyGames has a game API, but product identifiers are not part of the hobbyist tier; use only if your subscription allows product-code export.
- upc.dev can perform no-key product lookup/search for basic data, but search is not a complete video-game platform export.
- Open Products Facts can perform no-key barcode lookup for non-food products, but coverage of video games is expected to be sparse.
- UPCDatabase has an API but requires account/API-token setup.
- LaunchBox has useful local metadata exports, but no official public Games Database API for this use case.
- Do not scrape websites into the public project unless their terms explicitly allow it.

### Backend validation with local sample photos

For detector/matcher tuning, keep real sample photos in ignored folders such as `photos/incoming/` and create a local expectations file:

```bash
game-collection validate-samples --write-template
```

Edit `review/sample-expectations.json` so each photo has:

- `photo`: local image path,
- `platform`: IGDB platform name,
- `expected_titles`: titles visible in the image.

Then run:

```bash
game-collection validate-samples
```

The command runs the backend ingestion path without the web UI, using barcode scanning and local catalog matches. It writes:

- `review/sample-validation/report.csv`: expected-title pass/fail rows,
- `review/sample-validation/suggestions.csv`: every barcode-derived suggestion,
- `review/sample-validation/crops/`: retained compatibility output directory.

Those files are ignored because they can reveal your real library. Commit only generic examples such as `examples/sample-expectations.example.json`.

### 4. Review the scanned match

After upload, the browser redirects to an ingest results page.

Check:

- uploaded photo thumbnails,
- matched database cover thumbnail,
- matched title,
- platform.

No game is imported automatically from photo upload.

### 5. Accept, change, or reject the match

The review page is optimized for one game:

- tap `Accept` to import the matched game,
- tap `Edit` to focus the title field, then type and choose from cached metadata autocomplete,
- tap `Reject` to keep the scan in the audit log without importing it.

Accepted games are imported as owned and unplayed by default. You can edit ownership and play history later from the game detail page.

The web UI preserves an internal audit CSV under `review/web-ingests/`, but that folder is ignored and is not part of the normal workflow.

## Manual Review Fallback

Use this when you want a CLI batch workflow, image matching is unavailable, or a photo is too messy.

Put new source images here:

```text
photos/incoming/
```

### 1. Create an intake review file

For each source photo, create a review CSV:

```bash
game-collection new-intake photos/incoming/2026-08-03-gamecube-floor.jpg --out review/2026-08-03-gamecube-floor.csv
```

Open the CSV and add one row per visible game with:

- `candidate_title`
- `platform`

Leave the provider/match columns blank before matching.

### 2. Match candidate titles against metadata

Use IGDB:

```bash
game-collection match-review review/2026-08-03-gamecube-floor.csv --provider igdb --out review/2026-08-03-gamecube-floor.matched.csv
```

Or use TheGamesDB:

```bash
game-collection match-review review/2026-08-03-gamecube-floor.csv --provider thegamesdb --out review/2026-08-03-gamecube-floor.matched.csv
```

The matcher writes suggested metadata for review. Importing still depends on manually setting `decision=accept`.

### 3. Verify the matched CSV

Open the `.matched.csv` before importing.

For each row:

- Confirm `matched_title` is the actual game.
- Confirm `platform` is correct.
- Set `decision=accept` for rows you want to import.
- Keep `decision=review` or blank for uncertain rows.
- Fix `provider_game_id` only if you have manually found the correct provider record.

This review step is deliberate: it prevents a blurry cover or ambiguous title from polluting the library.

### 4. Import accepted rows

Import only accepted rows:

```bash
game-collection import-review review/2026-08-03-gamecube-floor.matched.csv --status owned --played unplayed
```

If the whole batch is already completed:

```bash
game-collection import-review review/2026-08-03-gamecube-floor.matched.csv --status owned --played completed
```

### 5. Verify in the browser

Start the local interface:

```bash
game-collection serve
```

Open http://127.0.0.1:8765 and check:

- the title,
- platform,
- ownership status,
- play status,
- cover art,
- developer/publisher/release metadata when available.

Click a game title to edit its metadata, update ownership, mark it as a sell candidate, mark it sold, or record play status.

### 6. Choose what to play next

In the browser, use the `Library` view filters and shelves to focus on unplayed, playing, completed, owned, sold, or sell-candidate games. Tap a game to update ownership and play status; the library updates from those fields.

The CLI still has a compact planning command:

```bash
game-collection plan-next
```

## Collection Semantics

The database separates game identity from collection state:

- `games`: canonical game metadata from a provider.
- `collection_items`: whether you currently own, would sell, sold, loaned, or wishlisted a copy.
- `playthroughs`: historical play records that remain even if a copy is sold.

That means marking a game as sold does not erase that you played it.

## Browser Interface

Run:

```bash
game-collection serve --host 127.0.0.1 --port 8765
```

The interface is local-only by default and uses `collection.sqlite3`.

Available views:

- `Library`: browse, search, and filter the full collection.
- `Scan`: upload or take one back-cover photo, scan the barcode, and confirm one match.
- `Cache`: choose which IGDB platforms should have complete local metadata indexes for title autocomplete and cover art display. Cached platforms appear first, followed by uncached platforms alphabetically.
- `Game Detail`: edit metadata, ownership state, location/condition notes, sale notes, and play status.

Useful edits:

- Set ownership to `would_sell` for games you are considering selling.
- Set ownership to `sold` after sale; the game remains in history.
- Record play status as `completed` so sold games do not look unplayed later.

## Public Repo Hygiene

This repository is intended to be safe to publish without personal library data.

Committed files should include:

- source code,
- tests,
- documentation,
- `.env.example`,
- placeholder folders,
- example workflows such as `examples/review.sample.csv`.

Do not commit:

- `.env`,
- `collection.sqlite3`,
- real source photos,
- generated review CSVs,
- matched CSVs from your actual collection,
- exported reports that reveal your library.

The default `.gitignore` excludes those local files and keeps only placeholders plus examples.

## Current Photo Recognition Status

The current recognition implementation is intentionally conservative:

- it scans back-cover photos for retail barcodes,
- it validates decoded values as GS1 GTIN-8/12/13/14 formats,
- it looks up exact matches in local barcode caches,
- it uses cached provider metadata only for title autocomplete and cover art display,
- imports only manually accepted rows.
