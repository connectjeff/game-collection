# Architecture

## Goals

The system should inventory physical games from wide photos while keeping a trustworthy local history of ownership and play status.

Core requirements:

- Add games from a floor-layout photo.
- Use an external metadata/artwork provider where possible.
- Plan what to play next from owned/unplayed games.
- Track completed games independently of ownership.
- Mark games as candidates for sale.
- Mark games as sold without losing play history.

## Pipeline

```mermaid
flowchart LR
    A["Floor photo"] --> B["Case detection"]
    B --> C["Crop each case"]
    C --> D["OCR title/spine text"]
    D --> E["Metadata search"]
    E --> F["Match ranking"]
    F --> G["Human review CSV"]
    G --> H["SQLite import"]
    H --> I["Planning and collection views"]
```

## Data Model

`games` stores provider-backed identity and metadata. A game can exist even when you no longer own a copy.

`collection_items` stores ownership state for a physical copy:

- `owned`
- `would_sell`
- `sold`
- `loaned`
- `wishlist`

`playthroughs` stores durable history:

- `unplayed`
- `playing`
- `completed`
- `retired`

This is the key design choice: selling a copy updates `collection_items`, but completed history remains in `playthroughs`.

## Provider Strategy

The metadata provider boundary returns a normalized `GameMatch` with title, platform, dates, cover URL, and raw provider payload.

Start with:

- TheGamesDB for community game/artwork data.
- IGDB for richer metadata and cover art using Twitch client-credentials auth.

Add later:

- RAWG as a broad fallback source if its attribution terms fit the UI.
- Local LaunchBox export/import if you already maintain a LaunchBox library.

## Recognition Strategy

The photo recognizer should be optimized for accuracy over magic:

1. Ask the user to photograph cases in a grid with minimal overlap.
2. Detect rectangular case boundaries using OpenCV contours.
3. Save numbered crops.
4. Run OCR on each crop, trying rotations because spine/title orientation varies.
5. Match OCR text plus optional platform hints against the provider.
6. Auto-accept only high-confidence matches.
7. Send ambiguous results to CSV review.

## Recommended Next Implementation Step

Add optional dependencies behind an `image` extra:

```toml
[project.optional-dependencies]
image = ["opencv-python", "pillow", "pytesseract"]
```

Then implement:

```bash
game-collection detect-cases photos/floor-layout.jpg --out review/floor-layout.csv --crops review/crops
```

The current `new-intake` command provides the same downstream review/import shape so the database and matching workflow can be used immediately.
