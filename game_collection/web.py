from __future__ import annotations

import email.policy
import mimetypes
import html
import sqlite3
import threading
import uuid
import urllib.parse
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import db
from .automation import import_accepted_rows
from .cover_match import (
    PRIORITIZED_PLATFORMS,
    build_cover_index,
    build_platform_cache,
    default_index_path,
    platform_cache_statuses,
    prebuild_prioritized_cover_indexes,
    read_cover_index,
)
from .photo_ingest import PhotoIngestError, detect_photo_candidates
from .providers import ProviderError, get_provider
from .review import INTAKE_FIELDS, read_review, write_review


OWNERSHIP_STATUSES = ["owned", "would_sell", "sold", "loaned", "wishlist"]
PLAY_STATUSES = ["unplayed", "playing", "completed", "retired"]
PROVIDER_CHOICES = ["igdb"]
PLATFORM_PRESETS = PRIORITIZED_PLATFORMS
WEB_INGEST_ROOT = Path("review/web-ingests")


def _h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _selected(left: str | None, right: str) -> str:
    return " selected" if left == right else ""


def _cached_platform_options(platforms: list[str]) -> list[str]:
    return [status.name for status in platform_cache_statuses("igdb", platforms) if status.cached]


def _layout(title: str, body: str) -> bytes:
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_h(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1e252c;
      --muted: #65707c;
      --line: #d9dee5;
      --accent: #0c6b58;
      --accent-weak: #d9f0ea;
      --warn: #9b4f00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      padding: 14px 24px;
      display: flex;
      align-items: center;
      gap: 18px;
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    header a {{
      color: var(--accent);
      font-weight: 700;
      text-decoration: none;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1 {{ font-size: 24px; margin: 0 0 18px; }}
    h2 {{ font-size: 18px; margin: 28px 0 12px; }}
    form.filters {{
      display: grid;
      grid-template-columns: minmax(180px, 1fr) 170px 170px auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 18px;
    }}
    input, select, textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      background: #fff;
      color: var(--text);
      font: inherit;
    }}
    textarea {{ min-height: 130px; resize: vertical; }}
    label {{ display: grid; gap: 5px; color: var(--muted); font-size: 13px; }}
    button, .button {{
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      padding: 10px 14px;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
      white-space: nowrap;
    }}
    .button.secondary, button.secondary {{
      background: #e7ebef;
      color: var(--text);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--line);
      vertical-align: middle;
    }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: 0; }}
    .title-link {{ color: var(--accent); font-weight: 700; text-decoration: none; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--accent-weak);
      color: #124c41;
      font-size: 12px;
      font-weight: 700;
    }}
    .badge.sold {{ background: #eee3d5; color: var(--warn); }}
    .detail {{
      display: grid;
      grid-template-columns: 220px minmax(0, 1fr);
      gap: 22px;
      align-items: start;
    }}
    .cover {{
      width: 100%;
      aspect-ratio: 3 / 4;
      object-fit: cover;
      background: #e2e6ea;
      border: 1px solid var(--line);
      border-radius: 6px;
    }}
    .placeholder {{
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      padding: 16px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .actions {{ display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }}
    .muted {{ color: var(--muted); }}
    .notice {{
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      background: #fff;
      padding: 12px 14px;
      border-radius: 6px;
      margin-bottom: 16px;
    }}
    .notice.error {{ border-left-color: #b3261e; }}
    .upload-panel {{
      max-width: 760px;
    }}
    .review-table input, .review-table select {{
      min-width: 130px;
    }}
    .crop-thumb {{
      width: 86px;
      height: 112px;
      object-fit: cover;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: #e2e6ea;
    }}
    @media (max-width: 760px) {{
      header, main {{ padding-left: 14px; padding-right: 14px; }}
      form.filters, .detail, .grid {{ grid-template-columns: 1fr; }}
      table, thead, tbody, tr, th, td {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--line); padding: 8px 0; }}
      td {{ border: 0; padding: 5px 12px; }}
    }}
  </style>
</head>
<body>
  <header>
    <a href="/">Game Collection</a>
    <a href="/plan">Plan Next</a>
    <a href="/ingest">Upload Photos</a>
    <a href="/caches">Cache Settings</a>
  </header>
  <main>{body}</main>
</body>
</html>"""
    return page.encode("utf-8")


class CollectionHandler(BaseHTTPRequestHandler):
    db_path: Path
    platform_options: list[str] = PLATFORM_PRESETS

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_html(self, title: str, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = _layout(title, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, path: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", path)
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(data, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _multipart_form(self) -> tuple[dict[str, str], list[dict[str, Any]]]:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        message = BytesParser(policy=email.policy.default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8") + body
        )
        fields: dict[str, str] = {}
        files: list[dict[str, Any]] = []
        if not message.is_multipart():
            return fields, files
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if not name:
                continue
            if filename:
                files.append(
                    {
                        "name": name,
                        "filename": Path(filename).name,
                        "content_type": part.get_content_type(),
                        "data": payload,
                    }
                )
            else:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return fields, files

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send_html("Game Collection", self._collection(parsed.query))
            return
        if parsed.path == "/plan":
            self._send_html("Plan Next", self._plan())
            return
        if parsed.path == "/ingest":
            self._send_html("Upload Photos", self._ingest_form())
            return
        if parsed.path == "/caches":
            self._send_html("Cache Settings", self._cache_settings())
            return
        if parsed.path.startswith("/ingest/"):
            run_id = parsed.path.removeprefix("/ingest/").strip("/")
            self._send_html("Ingest Results", self._ingest_results(run_id))
            return
        if parsed.path == "/media":
            self._send_media(parsed.query)
            return
        if parsed.path.startswith("/games/"):
            try:
                game_id = int(parsed.path.removeprefix("/games/").strip("/"))
            except ValueError:
                self._send_html("Not Found", "<h1>Not found</h1>", HTTPStatus.NOT_FOUND)
                return
            self._send_html("Game Detail", self._game_detail(game_id))
            return
        self._send_html("Not Found", "<h1>Not found</h1>", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/ingest":
            self._handle_ingest_upload()
            return
        if parsed.path == "/caches":
            self._handle_cache_settings()
            return
        if parsed.path.startswith("/ingest/") and parsed.path.endswith("/review"):
            run_id = parsed.path.split("/")[2]
            self._handle_ingest_review(run_id)
            return
        form = self._form()
        with db.connect(self.db_path) as conn:
            if parsed.path.startswith("/games/") and parsed.path.endswith("/metadata"):
                game_id = int(parsed.path.split("/")[2])
                db.update_game_metadata(
                    conn,
                    game_id=game_id,
                    title=form["title"].strip(),
                    platform=form.get("platform") or None,
                    release_date=form.get("release_date") or None,
                    developer=form.get("developer") or None,
                    publisher=form.get("publisher") or None,
                    description=form.get("description") or None,
                    cover_url=form.get("cover_url") or None,
                )
                self._redirect(f"/games/{game_id}")
                return
            if parsed.path.startswith("/items/") and parsed.path.endswith("/collection"):
                item_id = int(parsed.path.split("/")[2])
                db.update_collection_item(
                    conn,
                    collection_item_id=item_id,
                    acquisition_status=form["acquisition_status"],
                    condition_notes=form.get("condition_notes") or None,
                    location=form.get("location") or None,
                    sale_notes=form.get("sale_notes") or None,
                )
                self._redirect(f"/games/{form['game_id']}")
                return
            if parsed.path.startswith("/games/") and parsed.path.endswith("/play"):
                game_id = int(parsed.path.split("/")[2])
                db.add_playthrough(
                    conn,
                    game_id=game_id,
                    play_status=form["play_status"],
                    notes=form.get("notes") or None,
                )
                self._redirect(f"/games/{game_id}")
                return
        self._send_html("Not Found", "<h1>Not found</h1>", HTTPStatus.NOT_FOUND)

    def _send_media(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        raw_path = (params.get("path") or [""])[0]
        try:
            base = WEB_INGEST_ROOT.resolve()
            media_path = Path(raw_path).resolve()
            media_path.relative_to(base)
        except (ValueError, OSError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not media_path.exists() or not media_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
        payload = media_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _collection(self, query: str) -> str:
        params = urllib.parse.parse_qs(query)
        q = (params.get("q") or [""])[0].strip().casefold()
        owning = (params.get("owning") or [""])[0]
        played = (params.get("played") or [""])[0]
        conn = db.connect(self.db_path)
        try:
            rows = list(db.list_collection(conn))
        finally:
            conn.close()
        filtered = []
        for row in rows:
            text = f"{row['title']} {row['platform'] or ''}".casefold()
            if q and q not in text:
                continue
            if owning and row["acquisition_status"] != owning:
                continue
            if played and row["latest_play_status"] != played:
                continue
            filtered.append(row)
        body = f"""
<h1>Library</h1>
<form class="filters" method="get" action="/">
  <label>Search
    <input name="q" value="{_h(q)}" placeholder="Title or platform">
  </label>
  <label>Ownership
    <select name="owning">
      <option value="">Any</option>
      {''.join(f'<option value="{status}"{_selected(owning, status)}>{status}</option>' for status in OWNERSHIP_STATUSES)}
    </select>
  </label>
  <label>Play status
    <select name="played">
      <option value="">Any</option>
      {''.join(f'<option value="{status}"{_selected(played, status)}>{status}</option>' for status in PLAY_STATUSES)}
    </select>
  </label>
  <button type="submit">Filter</button>
</form>
<p class="muted">{len(filtered)} shown from {len(rows)} collection items.</p>
{self._rows_table(filtered)}
"""
        return body

    def _plan(self) -> str:
        conn = db.connect(self.db_path)
        try:
            rows = list(db.plan_next(conn, limit=100))
        finally:
            conn.close()
        return f"<h1>Plan Next</h1><p class=\"muted\">Owned games that are unplayed or in progress.</p>{self._rows_table(rows)}"

    def _cache_settings(self, message: str | None = None, *, error: bool = False) -> str:
        notice = f'<div class="notice{" error" if error else ""}">{_h(message)}</div>' if message else ""
        statuses = platform_cache_statuses("igdb", self.platform_options)
        rows = []
        for status in statuses:
            checked = " checked" if status.cached else ""
            cached_text = f"{status.count} covers" if status.cached else "not cached"
            rows.append(
                f"""
<tr>
  <td><input type="checkbox" name="platform" value="{_h(status.name)}"{checked}></td>
  <td>{_h(status.name)}</td>
  <td><span class="badge{' sold' if not status.cached else ''}">{_h(cached_text)}</span></td>
</tr>"""
            )
        return f"""
<h1>Cache Settings</h1>
{notice}
<section class="panel">
  <p class="muted">Choose platforms to build local cover-art image indexes for. Cached platforms are listed first; uncached platforms are alphabetical.</p>
  <form method="post" action="/caches">
    <table>
      <thead><tr><th></th><th>Platform</th><th>Status</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <div class="actions"><button type="submit">Build Selected Indexes</button></div>
  </form>
</section>
"""

    def _handle_cache_settings(self) -> None:
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
        selected = [item for item in form.get("platform", []) if item]
        if not selected:
            self._send_html("Cache Settings", self._cache_settings("Choose at least one platform.", error=True))
            return
        try:
            provider = get_provider("igdb")
            built: list[str] = []
            for platform in selected:
                entries = build_cover_index(
                    provider=provider,
                    platform=platform,
                    index_path=default_index_path("igdb", platform),
                    limit=None,
                    refresh=True,
                )
                built.append(f"{platform}: {len(entries)} covers")
            self._send_html("Cache Settings", self._cache_settings("Built indexes for " + "; ".join(built)))
        except (ProviderError, ValueError) as exc:
            self._send_html("Cache Settings", self._cache_settings(str(exc), error=True), HTTPStatus.BAD_REQUEST)

    def _ingest_form(self, message: str | None = None, *, error: bool = False) -> str:
        notice = f'<div class="notice{" error" if error else ""}">{_h(message)}</div>' if message else ""
        provider_options = "".join(f'<option value="{provider}">{provider}</option>' for provider in PROVIDER_CHOICES)
        cached_platforms = _cached_platform_options(self.platform_options)
        platform_options = "".join(
            f'<option value="{_h(platform)}">{_h(platform)}</option>'
            for platform in cached_platforms
        )
        platform_control = (
            f'<select name="platform" required>{platform_options}</select>'
            if cached_platforms
            else '<select name="platform" required disabled><option value="">No cached platforms</option></select>'
        )
        status_options = "".join(f'<option value="{status}">{status}</option>' for status in OWNERSHIP_STATUSES)
        played_options = "".join(f'<option value="{status}">{status}</option>' for status in PLAY_STATUSES)
        return f"""
<h1>Upload Photos</h1>
{notice}
<section class="panel upload-panel">
  <form method="post" action="/ingest" enctype="multipart/form-data">
    <label>Game case photos
      <input type="file" name="photos" accept="image/*" multiple required>
    </label>
    <div class="grid">
      <label>Metadata provider
        <select name="provider">{provider_options}</select>
      </label>
      <label>Platform hint
        {platform_control}
      </label>
      <label>Auto-import threshold
        <input name="accept_threshold" type="number" min="0" max="1" step="0.01" value="0.92">
      </label>
      <label>Ownership status
        <select name="status">{status_options}</select>
      </label>
      <label>Initial play status
        <select name="played">{played_options}</select>
      </label>
    </div>
    <div class="actions"><button type="submit">Upload And Ingest</button></div>
  </form>
</section>
"""

    def _run_dir(self, run_id: str) -> Path:
        return WEB_INGEST_ROOT / run_id

    def _audit_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "audit.csv"

    def _handle_ingest_upload(self) -> None:
        try:
            fields, files = self._multipart_form()
            image_files = [item for item in files if item["name"] == "photos" and item["data"]]
            if not image_files:
                self._send_html("Upload Photos", self._ingest_form("Choose at least one photo.", error=True))
                return

            run_id = uuid.uuid4().hex
            run_dir = self._run_dir(run_id)
            uploads_dir = run_dir / "uploads"
            crops_dir = run_dir / "crops"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            crops_dir.mkdir(parents=True, exist_ok=True)

            provider_name = fields.get("provider", "igdb")
            provider = get_provider(provider_name)
            platform = fields.get("platform") or None
            accept_threshold = float(fields.get("accept_threshold") or "0.92")
            status = fields.get("status") or "owned"
            played = fields.get("played") or "unplayed"
            if not platform:
                self._send_html("Upload Photos", self._ingest_form("Choose a cached platform.", error=True))
                return
            cover_entries = read_cover_index(default_index_path(provider_name, platform))
            if not cover_entries:
                self._send_html(
                    "Upload Photos",
                    self._ingest_form(f"Build the cover index for {platform} before uploading photos.", error=True),
                    HTTPStatus.BAD_REQUEST,
                )
                return

            rows: list[dict[str, str]] = []
            for index, file_info in enumerate(image_files, start=1):
                suffix = Path(file_info["filename"]).suffix.lower() or ".jpg"
                upload_path = uploads_dir / f"upload-{index:03d}{suffix}"
                upload_path.write_bytes(file_info["data"])
                rows.extend(
                    detect_photo_candidates(
                        photo_path=upload_path,
                        crops_dir=crops_dir,
                        platform=platform,
                        cover_entries=cover_entries,
                        accept_threshold=accept_threshold,
                    )
                )

            write_review(self._audit_path(run_id), rows)
            imported, skipped = import_accepted_rows(
                db_path=self.db_path,
                rows=rows,
                status=status,
                played=played,
                skip_existing=True,
            )

            summary = {
                "provider": provider_name,
                "platform": platform or "",
                "accept_threshold": str(accept_threshold),
                "status": status,
                "played": played,
                "uploaded": str(len(image_files)),
                "candidates": str(len(rows)),
                "cover_index_entries": str(len(cover_entries)),
                "imported": str(imported),
                "skipped_existing": str(skipped),
            }
            (run_dir / "summary.csv").write_text(
                "\n".join(f"{key},{value}" for key, value in summary.items()),
                encoding="utf-8",
            )
            self._redirect(f"/ingest/{run_id}")
        except (PhotoIngestError, ProviderError, ValueError) as exc:
            self._send_html("Upload Photos", self._ingest_form(str(exc), error=True), HTTPStatus.BAD_REQUEST)

    def _summary(self, run_id: str) -> dict[str, str]:
        path = self._run_dir(run_id) / "summary.csv"
        if not path.exists():
            return {}
        summary: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(",")
            summary[key] = value
        return summary

    def _ingest_results(self, run_id: str, message: str | None = None) -> str:
        audit_path = self._audit_path(run_id)
        if not audit_path.exists():
            return "<h1>Ingest run not found</h1>"
        rows = read_review(audit_path)
        summary = self._summary(run_id)
        accepted = sum(1 for row in rows if row.get("decision") == "accept")
        needs_review = len(rows) - accepted
        notice = f'<div class="notice">{_h(message)}</div>' if message else ""
        return f"""
<h1>Ingest Results</h1>
{notice}
<section class="panel">
  <p><strong>{_h(summary.get('imported', accepted))}</strong> imported, <strong>{_h(summary.get('skipped_existing', '0'))}</strong> skipped as existing, <strong>{needs_review}</strong> needs review.</p>
  <p class="muted">Compared detected covers against {_h(summary.get('cover_index_entries', '0'))} indexed cover images.</p>
  <p class="muted">Provider: {_h(summary.get('provider'))} | Platform hint: {_h(summary.get('platform')) or 'none'} | Threshold: {_h(summary.get('accept_threshold'))}</p>
</section>
<form method="post" action="/ingest/{_h(run_id)}/review">
  {self._review_rows_table(rows)}
  <div class="actions"><button type="submit">Save And Import Accepted Rows</button><a class="button secondary" href="/ingest">Upload More Photos</a><a class="button secondary" href="/">Back To Library</a></div>
</form>
"""

    def _review_rows_table(self, rows: list[dict[str, str]]) -> str:
        if not rows:
            return '<div class="panel muted">No case candidates were detected.</div>'
        table_rows = []
        decision_options = ["review", "accept", "ignore"]
        for index, row in enumerate(rows):
            crop = row.get("crop_path")
            crop_html = (
                f'<img class="crop-thumb" src="/media?path={urllib.parse.quote(crop)}" alt="Detected crop">'
                if crop
                else ""
            )
            options = "".join(
                f'<option value="{decision}"{_selected(row.get("decision"), decision)}>{decision}</option>'
                for decision in decision_options
            )
            hidden_metadata = "".join(
                f'<input type="hidden" name="row_{index}_{field}" value="{_h(row.get(field))}">'
                for field in ("release_date", "developer", "publisher", "description", "cover_url")
            )
            table_rows.append(
                f"""
<tr>
  <td>{crop_html}<input type="hidden" name="row_{index}_photo_path" value="{_h(row.get('photo_path'))}"><input type="hidden" name="row_{index}_crop_path" value="{_h(crop)}"></td>
  <td><input name="row_{index}_candidate_title" value="{_h(row.get('candidate_title'))}"></td>
  <td><input name="row_{index}_platform" value="{_h(row.get('platform'))}"></td>
  <td><input name="row_{index}_provider" value="{_h(row.get('provider'))}"></td>
  <td><input name="row_{index}_provider_game_id" value="{_h(row.get('provider_game_id'))}"></td>
  <td><input name="row_{index}_matched_title" value="{_h(row.get('matched_title'))}"></td>
  <td><input name="row_{index}_confidence" value="{_h(row.get('confidence'))}"></td>
  <td><select name="row_{index}_decision">{options}</select></td>
  <td><input name="row_{index}_notes" value="{_h(row.get('notes'))}">{hidden_metadata}</td>
</tr>"""
            )
        return f"""
<input type="hidden" name="row_count" value="{len(rows)}">
<table class="review-table">
  <thead><tr><th>Crop</th><th>Detected Match</th><th>Platform</th><th>Provider</th><th>Provider ID</th><th>Matched Title</th><th>Confidence</th><th>Decision</th><th>Notes</th></tr></thead>
  <tbody>{''.join(table_rows)}</tbody>
</table>"""

    def _handle_ingest_review(self, run_id: str) -> None:
        form = self._form()
        row_count = int(form.get("row_count") or "0")
        rows: list[dict[str, str]] = []
        for index in range(row_count):
            row = {}
            for field in INTAKE_FIELDS:
                row[field] = form.get(f"row_{index}_{field}", "")
            rows.append(row)
        write_review(self._audit_path(run_id), rows)
        summary = self._summary(run_id)
        imported, skipped = import_accepted_rows(
            db_path=self.db_path,
            rows=rows,
            status=summary.get("status") or "owned",
            played=summary.get("played") or "unplayed",
            skip_existing=True,
        )
        self._send_html(
            "Ingest Results",
            self._ingest_results(run_id, f"Imported {imported} newly accepted row(s); skipped {skipped} existing row(s)."),
        )

    def _rows_table(self, rows: list[sqlite3.Row]) -> str:
        if not rows:
            return '<div class="panel muted">No games match this view.</div>'
        table_rows = []
        for row in rows:
            owning_class = " sold" if row["acquisition_status"] == "sold" else ""
            platform = _h(row["platform"]) if row["platform"] else ""
            table_rows.append(
                f"""
<tr>
  <td><a class="title-link" href="/games/{row['game_id']}">{_h(row['title'])}</a></td>
  <td>{platform}</td>
  <td><span class="badge{owning_class}">{_h(row['acquisition_status'])}</span></td>
  <td><span class="badge">{_h(row['latest_play_status'])}</span></td>
  <td class="muted">{_h(row['provider'])}:{_h(row['provider_game_id'])}</td>
</tr>"""
            )
        return f"""
<table>
  <thead><tr><th>Title</th><th>Platform</th><th>Ownership</th><th>Played</th><th>Provider</th></tr></thead>
  <tbody>{''.join(table_rows)}</tbody>
</table>"""

    def _game_detail(self, game_id: int) -> str:
        conn = db.connect(self.db_path)
        try:
            row = db.get_game_detail(conn, game_id=game_id)
        finally:
            conn.close()
        if row is None:
            return "<h1>Game not found</h1>"
        cover = (
            f'<img class="cover" src="{_h(row["cover_url"])}" alt="Cover art for {_h(row["title"])}">'
            if row["cover_url"]
            else '<div class="cover placeholder">No cover art</div>'
        )
        ownership_options = "".join(
            f'<option value="{status}"{_selected(row["acquisition_status"], status)}>{status}</option>'
            for status in OWNERSHIP_STATUSES
        )
        play_options = "".join(f'<option value="{status}">{status}</option>' for status in PLAY_STATUSES)
        return f"""
<h1>{_h(row['title'])}</h1>
<div class="detail">
  <div>
    {cover}
    <p class="muted">{_h(row['provider'])}:{_h(row['provider_game_id'])}</p>
  </div>
  <div>
    <section class="panel">
      <h2>Metadata</h2>
      <form method="post" action="/games/{game_id}/metadata">
        <div class="grid">
          <label>Title <input name="title" value="{_h(row['title'])}" required></label>
          <label>Platform <input name="platform" value="{_h(row['platform'])}"></label>
          <label>Release date <input name="release_date" value="{_h(row['release_date'])}" placeholder="YYYY-MM-DD"></label>
          <label>Developer <input name="developer" value="{_h(row['developer'])}"></label>
          <label>Publisher <input name="publisher" value="{_h(row['publisher'])}"></label>
          <label>Cover URL <input name="cover_url" value="{_h(row['cover_url'])}"></label>
        </div>
        <label>Description <textarea name="description">{_h(row['description'])}</textarea></label>
        <div class="actions"><button type="submit">Save Metadata</button><a class="button secondary" href="/">Back</a></div>
      </form>
    </section>

    <section class="panel">
      <h2>Collection Copy</h2>
      <form method="post" action="/items/{row['collection_item_id']}/collection">
        <input type="hidden" name="game_id" value="{game_id}">
        <div class="grid">
          <label>Ownership
            <select name="acquisition_status">{ownership_options}</select>
          </label>
          <label>Location <input name="location" value="{_h(row['location'])}"></label>
        </div>
        <label>Condition notes <textarea name="condition_notes">{_h(row['condition_notes'])}</textarea></label>
        <label>Sale notes <textarea name="sale_notes">{_h(row['sale_notes'])}</textarea></label>
        <p class="muted">Latest play status: <strong>{_h(row['latest_play_status'])}</strong>{' | Sold on: ' + _h(row['sold_on']) if row['sold_on'] else ''}</p>
        <div class="actions"><button type="submit">Save Collection Copy</button></div>
      </form>
    </section>

    <section class="panel">
      <h2>Play History</h2>
      <form method="post" action="/games/{game_id}/play">
        <div class="grid">
          <label>New play status
            <select name="play_status">{play_options}</select>
          </label>
          <label>Notes <input name="notes" placeholder="Optional note"></label>
        </div>
        <div class="actions"><button type="submit">Record Play Status</button></div>
      </form>
    </section>
  </div>
</div>"""


def serve(
    db_path: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    prebuild_cover_indexes: bool = True,
    refresh_cover_indexes: bool = False,
    refresh_platform_cache: bool = False,
) -> None:
    db.init_db(db_path)
    platform_options = PLATFORM_PRESETS
    try:
        provider = get_provider("igdb")
        platform_options = build_platform_cache(provider=provider, refresh=refresh_platform_cache)
        if prebuild_cover_indexes:
            def prebuild() -> None:
                print("Prebuilding prioritized cover indexes in the background...")
                try:
                    for platform, count in prebuild_prioritized_cover_indexes(
                        provider=provider,
                        limit=None,
                        refresh=refresh_cover_indexes,
                    ).items():
                        print(f"  {platform}: {count} covers indexed")
                except ProviderError as exc:
                    print(f"Warning: background cover-index prebuild failed: {exc}")

            threading.Thread(target=prebuild, daemon=True).start()
    except ProviderError as exc:
        print(f"Warning: could not prebuild IGDB caches: {exc}")
    handler = type(
        "ConfiguredCollectionHandler",
        (CollectionHandler,),
        {"db_path": db_path, "platform_options": platform_options},
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Serving game collection at http://{host}:{port}")
    server.serve_forever()
