# Game Collection

Local web app for scanning physical video games, matching barcodes to public metadata, and managing a personal game library.

The current workflow is web-only. User photos, review runs, caches, secrets, and the SQLite library database are local ignored files and are not intended for a public repo.

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
game-collection credentials check --provider igdb
game-collection serve --host 0.0.0.0 --port 8765
```

Open the app from the Mac at:

```text
http://127.0.0.1:8765
```

Open it from an iPhone on the same network with the Mac's LAN IP:

```text
http://<mac-ip>:8765
```

In Safari, use Share -> Add to Home Screen.

## Credentials

IGDB uses Twitch client credentials.

Create `.env` from `.env.example` and set:

```bash
IGDB_CLIENT_ID=your_client_id_here
IGDB_CLIENT_SECRET=your_client_secret_here
```

Do not commit `.env`.

## Web Workflow

1. Start the web app with `game-collection serve --host 0.0.0.0 --port 8765`.
2. Open `Scan`.
3. Choose or take one back-cover barcode photo. The ingest flow assumes one physical game per scan.
4. Pick the platform hint. The app remembers the last platform used to make repeated scans faster.
5. Tap `Scan`.
6. Review the result. Barcode matches are treated as exact code matches, but the user still accepts or rejects the proposed game.
7. If needed, edit the platform or title. Title autocomplete uses locally cached IGDB platform metadata when available.
8. Set collection state and played state.
9. Tap `Accept` to add the game. Duplicate titles for the same platform are blocked.
10. After accept or reject, the app returns to the scan screen for the next game.

If no barcode lookup resolves the game, the review form prompts for title and platform. Box art is used only for visual verification from metadata caches; cover image matching is not part of ingest.

## Library Workflow

Use `Library` to browse, search, filter, and open game details. The detail page is intentionally small for mobile use:

- cover art,
- release date,
- description,
- collection state,
- played state,
- delete from library with confirmation.

Metadata fields are not editable on the detail page. The app treats provider metadata as cacheable reference data and keeps user state limited to collection and play status.

## Cache Workflow

Use `Cache` to refresh provider data used by the web UI:

- IGDB platform list,
- title autocomplete data,
- cover art used for visual confirmation,
- barcode source downloads and local barcode cache rebuilds,
- missing library cover art refresh.

Barcode lookup checks local cache first, then uses live public lookup sources when possible. Local barcode caches are useful for speed, but live lookup is part of the normal web ingest path.

## Local Data

These files are intentionally ignored because they can contain private library data or credentials:

```text
.env
collection.sqlite3
review/
```

`review/` is used internally by the web app for uploaded images, generated review rows, barcode sources, and metadata caches.
