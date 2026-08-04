from __future__ import annotations

import email.policy
import json
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


def _normalize_title(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _cover_thumb(
    path: str | Path | None,
    label: str,
    alt: str,
    *,
    row_index: int | None = None,
    role: str | None = None,
) -> str:
    sample_attrs = ""
    image_attrs = ""
    if row_index is not None:
        sample_attrs += f' data-row="{row_index}"'
        image_attrs += f' data-row="{row_index}"'
    if role:
        sample_attrs += f' data-role="{_h(role)}"'
        image_role = role.removesuffix("-sample")
        image_attrs += f' data-role="{_h(image_role)}"'
    if not path:
        hidden = " hidden" if role == "matched-cover-sample" else ""
        image_hidden = " hidden" if role == "matched-cover-sample" else ""
        return (
            f'<div class="cover-sample"{sample_attrs}{hidden}>'
            f'<img class="crop-thumb"{image_attrs}{image_hidden} alt="{_h(alt)}">'
            f'<span class="cover-label">{_h(label)}</span>'
            "</div>"
        )
    src = urllib.parse.quote(str(path))
    return (
        f'<div class="cover-sample"{sample_attrs}>'
        f'<img class="crop-thumb" src="/media?path={src}" alt="{_h(alt)}"{image_attrs}>'
        f'<span class="cover-label">{_h(label)}</span>'
        "</div>"
    )


def _matched_cover_path(row: dict[str, str]) -> str:
    provider = row.get("provider")
    platform = row.get("platform")
    provider_game_id = row.get("provider_game_id")
    if provider and platform and provider_game_id:
        indexed_cover = default_index_path(provider, platform).parent / "covers" / f"{provider_game_id}.jpg"
        if indexed_cover.exists():
            return str(indexed_cover)
    notes = row.get("notes", "")
    marker = "cover_path="
    if marker not in notes:
        return ""
    return notes.split(marker, 1)[1].split(";", 1)[0].strip()


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
      min-width: 0;
    }}
    .review-table textarea {{
      min-width: 0;
      min-height: 64px;
      resize: vertical;
      line-height: 1.3;
    }}
    .title-field {{
      overflow-wrap: anywhere;
    }}
    .review-table {{
      table-layout: fixed;
    }}
    .review-table th:nth-child(1), .review-table td:nth-child(1) {{ width: 210px; }}
    .review-table th:nth-child(2), .review-table td:nth-child(2) {{ width: 20%; }}
    .review-table th:nth-child(3), .review-table td:nth-child(3) {{ width: 16%; }}
    .review-table th:nth-child(4), .review-table td:nth-child(4) {{ width: auto; }}
    .review-table th:nth-child(5), .review-table td:nth-child(5) {{ width: 120px; }}
    .review-table th:nth-child(6), .review-table td:nth-child(6) {{ width: 1px; padding-left: 0; padding-right: 0; }}
    .decision-actions {{
      display: flex;
      gap: 6px;
      justify-content: flex-end;
    }}
    .icon-button {{
      width: 34px;
      height: 34px;
      display: inline-grid;
      place-items: center;
      padding: 0;
      border-radius: 6px;
      font-size: 17px;
      line-height: 1;
    }}
    .icon-button.accept {{
      background: var(--accent);
      color: #fff;
    }}
    .icon-button.ignore {{
      background: #ece3db;
      color: var(--warn);
    }}
    .icon-button.review {{
      background: #e7ebef;
      color: var(--text);
    }}
    .outcome-section {{
      margin-top: 22px;
    }}
    .empty-state {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
      padding: 14px;
    }}
    .empty-state[hidden] {{ display: none; }}
    .match-control {{
      position: relative;
    }}
    .match-suggestions {{
      position: absolute;
      z-index: 4;
      left: 0;
      right: 0;
      top: calc(100% + 4px);
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      box-shadow: 0 8px 22px rgba(20, 31, 42, 0.14);
      max-height: 260px;
      overflow-y: auto;
    }}
    .match-suggestions[hidden] {{ display: none; }}
    .match-option {{
      display: flex;
      gap: 8px;
      align-items: center;
      width: 100%;
      border: 0;
      border-radius: 0;
      background: #fff;
      color: var(--text);
      padding: 8px 10px;
      text-align: left;
      font-weight: 600;
      white-space: normal;
    }}
    .match-option:hover, .match-option:focus {{
      background: var(--accent-weak);
      color: var(--text);
      outline: 0;
    }}
    .match-option small {{
      color: var(--muted);
      font-weight: 500;
    }}
    .crop-thumb {{
      width: 86px;
      height: 112px;
      object-fit: cover;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: #e2e6ea;
    }}
    .cover-pair {{
      display: flex;
      gap: 10px;
      align-items: start;
      min-width: 190px;
    }}
    .cover-sample {{
      display: grid;
      gap: 4px;
      justify-items: center;
    }}
    .cover-label {{
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    @media (max-width: 760px) {{
      header, main {{ padding-left: 14px; padding-right: 14px; }}
      form.filters, .detail, .grid {{ grid-template-columns: 1fr; }}
      table, thead, tbody, tr, th, td {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--line); padding: 8px 0; }}
      td {{ border: 0; padding: 5px 12px; }}
      .review-table tr {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 8px;
        margin-bottom: 12px;
      }}
      .review-table td {{
        padding: 8px 12px;
      }}
      .cover-pair {{ min-width: 0; }}
    }}
  </style>
  <script>
    const matchSuggestionState = new Map();

    function setField(row, field, value) {{
      const element = document.querySelector(`[name="row_${{row}}_${{field}}"]`);
      if (element) element.value = value || "";
    }}

    function setMatchedCover(row, src) {{
      const image = document.querySelector(`[data-row="${{row}}"][data-role="matched-cover"]`);
      const sample = document.querySelector(`[data-row="${{row}}"][data-role="matched-cover-sample"]`);
      if (!image || !sample) return;
      if (src) {{
        image.src = `/media?path=${{encodeURIComponent(src)}}`;
        image.hidden = false;
        sample.hidden = false;
      }} else {{
        image.removeAttribute("src");
        image.hidden = true;
        sample.hidden = true;
      }}
    }}

    function applyMatch(row, match) {{
      setField(row, "provider", match.provider);
      setField(row, "provider_game_id", match.provider_game_id);
      setField(row, "matched_title", match.title);
      setField(row, "release_date", match.release_date);
      setField(row, "developer", match.developer);
      setField(row, "publisher", match.publisher);
      setField(row, "description", match.description);
      setField(row, "cover_url", match.cover_url);
      setField(row, "confidence", match.confidence);
      setField(row, "notes", match.notes);
      setMatchedCover(row, match.cover_path);
    }}

    function updateOutcomeCounts() {{
      ["review", "accept", "ignore"].forEach((decision) => {{
        const tableBody = document.querySelector(`[data-role="${{decision}}-rows"]`);
        const empty = document.querySelector(`[data-role="${{decision}}-empty"]`);
        const count = tableBody ? tableBody.querySelectorAll("tr").length : 0;
        if (empty) empty.hidden = count !== 0;
        document.querySelectorAll(`[data-role="${{decision}}-count"]`).forEach((target) => {{
          target.textContent = String(count);
        }});
      }});
    }}

    function moveReviewRow(row, decision) {{
      const tableRow = document.querySelector(`tr[data-row="${{row}}"]`);
      const target = document.querySelector(`[data-role="${{decision}}-rows"]`);
      if (!tableRow || !target) return;
      setField(row, "decision", decision);
      tableRow.dataset.decision = decision;
      target.appendChild(tableRow);
      updateOutcomeCounts();
    }}

    async function loadMatches(input) {{
      const row = input.dataset.row;
      const platform = document.querySelector(`[name="row_${{row}}_platform"]`)?.value || "";
      const q = input.value.trim();
      const panel = document.querySelector(`[data-row="${{row}}"][data-role="match-suggestions"]`);
      if (!panel || q.length < 2 || !platform) {{
        if (panel) panel.hidden = true;
        return [];
      }}
      const response = await fetch(`/matches?platform=${{encodeURIComponent(platform)}}&q=${{encodeURIComponent(q)}}`);
      if (!response.ok) {{
        panel.hidden = true;
        return [];
      }}
      const matches = await response.json();
      matchSuggestionState.set(row, matches);
      panel.innerHTML = matches.map((match, index) => `
        <button class="match-option" type="button" data-row="${{row}}" data-index="${{index}}">
          <span>${{match.title}}</span>
          <small>${{match.release_date || ""}}</small>
        </button>
      `).join("");
      panel.hidden = matches.length === 0;
      panel.querySelectorAll(".match-option").forEach((button) => {{
        button.addEventListener("mousedown", (event) => {{
          event.preventDefault();
          const selected = matchSuggestionState.get(row)?.[Number(button.dataset.index)];
          if (selected) applyMatch(row, selected);
          panel.hidden = true;
        }});
      }});
      return matches;
    }}

    function installMatchInputs() {{
      document.querySelectorAll("[data-role='match-title-input']").forEach((input) => {{
        let timer;
        input.addEventListener("input", () => {{
          clearTimeout(timer);
          timer = setTimeout(() => loadMatches(input), 180);
        }});
        input.addEventListener("focus", () => loadMatches(input));
        input.addEventListener("blur", () => {{
          const row = input.dataset.row;
          const matches = matchSuggestionState.get(row) || [];
          const exact = matches.find((match) => match.title.toLowerCase() === input.value.trim().toLowerCase());
          if (exact) applyMatch(row, exact);
          setTimeout(() => {{
            const panel = document.querySelector(`[data-row="${{row}}"][data-role="match-suggestions"]`);
            if (panel) panel.hidden = true;
          }}, 120);
        }});
      }});
    }}

    function installDecisionActions() {{
      document.querySelectorAll("[data-action-decision]").forEach((button) => {{
        button.addEventListener("click", () => {{
          moveReviewRow(button.dataset.row, button.dataset.actionDecision);
        }});
      }});
      updateOutcomeCounts();
    }}

    document.addEventListener("DOMContentLoaded", () => {{
      installMatchInputs();
      installDecisionActions();
    }});
  </script>
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
        if parsed.path == "/matches":
            self._send_matches(parsed.query)
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
            allowed_roots = [WEB_INGEST_ROOT.resolve(), Path("review/cover-indexes").resolve()]
            media_path = Path(raw_path).resolve()
            if not any(_is_relative_to(media_path, root) for root in allowed_roots):
                raise ValueError
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

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_matches(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        q = (params.get("q") or [""])[0].strip()
        platform = (params.get("platform") or [""])[0].strip()
        if len(q) < 2 or not platform:
            self._send_json([])
            return
        entries = read_cover_index(default_index_path("igdb", platform))
        normalized_query = _normalize_title(q)
        matches = []
        for entry in entries:
            normalized_title = _normalize_title(entry.title)
            if normalized_query not in normalized_title:
                continue
            matches.append(
                {
                    "provider": entry.provider,
                    "provider_game_id": entry.provider_game_id,
                    "title": entry.title,
                    "platform": entry.platform or platform,
                    "release_date": entry.release_date or "",
                    "developer": entry.developer or "",
                    "publisher": entry.publisher or "",
                    "description": entry.description or "",
                    "cover_url": entry.cover_url or "",
                    "cover_path": str(entry.cover_path),
                    "confidence": "",
                    "notes": json.dumps({"manual_match": True, "cover_index_path": str(entry.cover_path)}, ensure_ascii=True),
                }
            )
            if len(matches) >= 8:
                break
        self._send_json(matches)

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
            db.init_db(self.db_path)
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
                        accept_threshold=2.0,
                    )
                )
            for row in rows:
                row["decision"] = "review"

            write_review(self._audit_path(run_id), rows)

            summary = {
                "provider": provider_name,
                "platform": platform or "",
                "status": status,
                "played": played,
                "uploaded": str(len(image_files)),
                "candidates": str(len(rows)),
                "cover_index_entries": str(len(cover_entries)),
                "imported": "0",
                "skipped_existing": "0",
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
  <p><strong>{len(rows)}</strong> suggested match(es), <strong>{accepted}</strong> marked for import, <strong>{needs_review}</strong> awaiting review.</p>
  <p class="muted">Compared detected covers against {_h(summary.get('cover_index_entries', '0'))} indexed cover images.</p>
  <p class="muted">Provider: {_h(summary.get('provider'))} | Platform hint: {_h(summary.get('platform')) or 'none'}</p>
</section>
<form method="post" action="/ingest/{_h(run_id)}/review">
  {self._review_rows_table(rows)}
  <div class="actions"><button type="submit">Save And Import Accepted Rows</button><a class="button secondary" href="/ingest">Upload More Photos</a><a class="button secondary" href="/">Back To Library</a></div>
</form>
"""

    def _review_rows_table(self, rows: list[dict[str, str]]) -> str:
        if not rows:
            return '<div class="panel muted">No case candidates were detected.</div>'
        grouped_rows = {"review": [], "accept": [], "ignore": []}
        for index, row in enumerate(rows):
            crop = row.get("crop_path")
            decision = row.get("decision") if row.get("decision") in grouped_rows else "review"
            matched_cover = _matched_cover_path(row)
            crop_html = _cover_thumb(crop, "Uploaded", "Detected crop from uploaded photo")
            matched_cover_html = _cover_thumb(
                matched_cover,
                "Matched",
                "Matched cached cover art",
                row_index=index,
                role="matched-cover-sample",
            )
            cover_pair_html = f'<div class="cover-pair">{crop_html}{matched_cover_html}</div>'
            hidden_metadata = "".join(
                f'<input type="hidden" name="row_{index}_{field}" value="{_h(row.get(field))}">'
                for field in (
                    "provider",
                    "provider_game_id",
                    "release_date",
                    "developer",
                    "publisher",
                    "description",
                    "cover_url",
                    "confidence",
                    "notes",
                )
            )
            action_buttons = f"""
    <div class="decision-actions">
      <button class="icon-button accept" type="button" data-row="{index}" data-action-decision="accept" title="Accept">&#10003;</button>
      <button class="icon-button ignore" type="button" data-row="{index}" data-action-decision="ignore" title="Ignore">&#10005;</button>
      <button class="icon-button review" type="button" data-row="{index}" data-action-decision="review" title="Move back to review">&#8634;</button>
    </div>"""
            grouped_rows[decision].append(
                f"""
<tr data-row="{index}" data-decision="{_h(decision)}">
  <td>{cover_pair_html}<input type="hidden" name="row_{index}_photo_path" value="{_h(row.get('photo_path'))}"><input type="hidden" name="row_{index}_crop_path" value="{_h(crop)}"></td>
  <td><textarea class="title-field" name="row_{index}_candidate_title" rows="2">{_h(row.get('candidate_title'))}</textarea></td>
  <td><input name="row_{index}_platform" value="{_h(row.get('platform'))}"></td>
  <td>
    <div class="match-control">
      <textarea class="title-field" name="row_{index}_matched_title" rows="2" data-row="{index}" data-role="match-title-input" autocomplete="off">{_h(row.get('matched_title'))}</textarea>
      <div class="match-suggestions" data-row="{index}" data-role="match-suggestions" hidden></div>
    </div>
  </td>
  <td>{action_buttons}<input type="hidden" name="row_{index}_decision" value="{_h(decision)}"></td>
  <td>{hidden_metadata}</td>
</tr>"""
            )

        def section_table(decision: str, title: str, empty_text: str) -> str:
            body_rows = "".join(grouped_rows[decision])
            empty_hidden = " hidden" if grouped_rows[decision] else ""
            return f"""
<section class="outcome-section">
  <h2>{_h(title)} <span class="badge" data-role="{decision}-count">{len(grouped_rows[decision])}</span></h2>
  <div class="empty-state" data-role="{decision}-empty"{empty_hidden}>{_h(empty_text)}</div>
  <table class="review-table">
    <thead><tr><th>Covers</th><th>Suggested</th><th>Platform</th><th>Matched Title</th><th>Action</th><th></th></tr></thead>
    <tbody data-role="{decision}-rows">{body_rows}</tbody>
  </table>
</section>"""

        return f"""
<input type="hidden" name="row_count" value="{len(rows)}">
{section_table("review", "Review Queue", "No rows waiting for review.")}
{section_table("accept", "Accepted", "No accepted rows yet.")}
{section_table("ignore", "Ignored", "No ignored rows yet.")}
"""

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
