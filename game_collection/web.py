from __future__ import annotations

import html
import sqlite3
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import db


OWNERSHIP_STATUSES = ["owned", "would_sell", "sold", "loaned", "wishlist"]
PLAY_STATUSES = ["unplayed", "playing", "completed", "retired"]


def _h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _selected(left: str | None, right: str) -> str:
    return " selected" if left == right else ""


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
  </header>
  <main>{body}</main>
</body>
</html>"""
    return page.encode("utf-8")


class CollectionHandler(BaseHTTPRequestHandler):
    db_path: Path

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

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send_html("Game Collection", self._collection(parsed.query))
            return
        if parsed.path == "/plan":
            self._send_html("Plan Next", self._plan())
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

    def _collection(self, query: str) -> str:
        params = urllib.parse.parse_qs(query)
        q = (params.get("q") or [""])[0].strip().casefold()
        owning = (params.get("owning") or [""])[0]
        played = (params.get("played") or [""])[0]
        with db.connect(self.db_path) as conn:
            rows = list(db.list_collection(conn))
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
        with db.connect(self.db_path) as conn:
            rows = list(db.plan_next(conn, limit=100))
        return f"<h1>Plan Next</h1><p class=\"muted\">Owned games that are unplayed or in progress.</p>{self._rows_table(rows)}"

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
        with db.connect(self.db_path) as conn:
            row = db.get_game_detail(conn, game_id=game_id)
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


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    db.init_db(db_path)
    handler = type("ConfiguredCollectionHandler", (CollectionHandler,), {"db_path": db_path})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Serving game collection at http://{host}:{port}")
    server.serve_forever()

