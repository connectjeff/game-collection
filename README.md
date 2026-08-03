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
game-collection add-manual "The Legend of Zelda: The Wind Waker" --platform "Nintendo GameCube" --status owned --played completed
game-collection list
```

Start the browsable library interface:

```bash
game-collection serve
```

Then open http://127.0.0.1:8765.

For metadata lookup:

```bash
export THEGAMESDB_API_KEY=...
game-collection search "Metroid Prime" --platform "Nintendo GameCube" --provider thegamesdb
```

For IGDB:

```bash
cp .env.example .env
# Edit .env and set IGDB_CLIENT_ID / IGDB_CLIENT_SECRET.
game-collection credentials check --provider igdb
game-collection search "Metroid Prime" --platform "Nintendo GameCube" --provider igdb
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

### 2. Put photos in the intake folder

Put new source images here:

```text
photos/incoming/
```

Example:

```text
photos/incoming/2026-08-03-gamecube-floor.jpg
photos/incoming/2026-08-03-ps2-floor.jpg
```

After a photo has been processed and verified, move it to:

```text
photos/processed/
```

### 3. Create an intake review file

For each source photo, create a review CSV:

```bash
game-collection new-intake photos/incoming/2026-08-03-gamecube-floor.jpg --out review/2026-08-03-gamecube-floor.csv
```

The current implementation creates the same review file that the future OCR detector will fill automatically. For now, open the CSV and add one row per visible game with:

- `candidate_title`
- `platform`

Leave the provider/match columns blank before matching.

### 4. Match candidate titles against metadata

Use IGDB:

```bash
game-collection match-review review/2026-08-03-gamecube-floor.csv --provider igdb --out review/2026-08-03-gamecube-floor.matched.csv
```

Or use TheGamesDB:

```bash
game-collection match-review review/2026-08-03-gamecube-floor.csv --provider thegamesdb --out review/2026-08-03-gamecube-floor.matched.csv
```

The matcher writes `decision=accept` for high-confidence matches and `decision=review` for uncertain ones.

### 5. Verify the matched CSV

Open the `.matched.csv` before importing.

For each row:

- Confirm `matched_title` is the actual game.
- Confirm `platform` is correct.
- Set `decision=accept` for rows you want to import.
- Keep `decision=review` or blank for uncertain rows.
- Fix `provider_game_id` only if you have manually found the correct provider record.

This review step is deliberate: it prevents a blurry cover or ambiguous title from polluting the library.

### 6. Import accepted rows

Import only accepted rows:

```bash
game-collection import-review review/2026-08-03-gamecube-floor.matched.csv --status owned --played unplayed
```

If the whole batch is already completed:

```bash
game-collection import-review review/2026-08-03-gamecube-floor.matched.csv --status owned --played completed
```

### 7. Verify in the browser

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

### 8. Plan what to play next

In the browser, open the `Plan Next` view.

Or use the CLI:

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
- `Plan Next`: view owned games that are unplayed or currently being played.
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

The first version creates the intake/review pipeline and data model. The `new-intake` command produces a CSV for candidates from a photo, leaving room for an OCR/detection backend. The intended next step is to add an OpenCV + OCR extractor that:

- detects rectangular case regions,
- crops each case,
- runs OCR on title/spine areas,
- ranks metadata matches using title/platform/provider results,
- asks for review only when confidence is low.
