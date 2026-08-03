from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import db
from .config import load_dotenv
from .providers import GameMatch, ProviderError, get_provider
from .review import match_to_row, read_review, write_intake_template, write_review
from .web import serve


PROVIDER_CHOICES = ["thegamesdb", "igdb", "rawg"]


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help="SQLite database path")


def cmd_init(args: argparse.Namespace) -> int:
    db.init_db(args.db)
    print(f"Initialized {args.db}")
    return 0


def cmd_add_manual(args: argparse.Namespace) -> int:
    db.init_db(args.db)
    with db.connect(args.db) as conn:
        game_id = db.upsert_game(
            conn,
            provider="manual",
            provider_game_id=f"{args.platform or 'unknown'}::{args.title}".casefold(),
            title=args.title,
            platform=args.platform,
            metadata_json=json.dumps({"source": "manual"}, ensure_ascii=True),
        )
        item_id = db.add_collection_item(
            conn,
            game_id=game_id,
            acquisition_status=args.status,
            condition_notes=args.condition_notes,
            location=args.location,
        )
        db.add_playthrough(conn, game_id=game_id, play_status=args.played)
    print(f"Added collection item {item_id}: {args.title}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db.init_db(args.db)
    with db.connect(args.db) as conn:
        rows = list(db.list_collection(conn))
    if not rows:
        print("No games in collection yet.")
        return 0
    for row in rows:
        platform = f" [{row['platform']}]" if row["platform"] else ""
        print(
            f"item={row['collection_item_id']:<4} game={row['game_id']:<4} {row['title']}{platform}  "
            f"owning={row['acquisition_status']} played={row['latest_play_status']}"
        )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    matches = provider.search(args.title, platform=args.platform, limit=args.limit)
    for match in matches:
        platform = f" [{match.platform}]" if match.platform else ""
        print(f"{match.confidence:.2f}  {match.provider}:{match.provider_game_id}  {match.title}{platform}")
    return 0


def cmd_new_intake(args: argparse.Namespace) -> int:
    write_intake_template(args.photo, args.out)
    print(f"Wrote review template to {args.out}")
    return 0


def cmd_match_review(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    rows = read_review(args.review_csv)
    matched_rows: list[dict[str, str]] = []
    for row in rows:
        candidate_title = row.get("candidate_title", "").strip()
        if not candidate_title:
            matched_rows.append(row)
            continue
        matches = provider.search(candidate_title, platform=row.get("platform") or None, limit=1)
        matched_rows.append(match_to_row(row, matches[0] if matches else None))
    write_review(args.out, matched_rows)
    print(f"Wrote matched review CSV to {args.out}")
    return 0


def _row_to_match(row: dict[str, str]) -> GameMatch:
    return GameMatch(
        provider=row["provider"],
        provider_game_id=row["provider_game_id"],
        title=row.get("matched_title") or row["candidate_title"],
        platform=row.get("platform") or None,
        confidence=float(row.get("confidence") or 0.0),
        raw={"review_notes": row.get("notes")},
    )


def cmd_import_review(args: argparse.Namespace) -> int:
    db.init_db(args.db)
    rows = read_review(args.review_csv)
    imported = 0
    with db.connect(args.db) as conn:
        for row in rows:
            if row.get("decision") != "accept":
                continue
            if not row.get("provider") or not row.get("provider_game_id"):
                continue
            match = _row_to_match(row)
            game_id = db.upsert_game(
                conn,
                provider=match.provider,
                provider_game_id=match.provider_game_id,
                title=match.title,
                platform=match.platform,
                cover_url=match.cover_url,
                metadata_json=json.dumps(match.raw or {}, ensure_ascii=True),
            )
            db.add_collection_item(conn, game_id=game_id, acquisition_status=args.status)
            db.add_playthrough(conn, game_id=game_id, play_status=args.played)
            imported += 1
    print(f"Imported {imported} accepted rows.")
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    db.init_db(args.db)
    with db.connect(args.db) as conn:
        db.mark_status(conn, collection_item_id=args.collection_item_id, status=args.status)
    print(f"Marked collection item {args.collection_item_id} as {args.status}")
    return 0


def cmd_play(args: argparse.Namespace) -> int:
    db.init_db(args.db)
    with db.connect(args.db) as conn:
        db.add_playthrough(conn, game_id=args.game_id, play_status=args.status, notes=args.notes)
    print(f"Recorded game {args.game_id} play status as {args.status}")
    return 0


def cmd_plan_next(args: argparse.Namespace) -> int:
    db.init_db(args.db)
    with db.connect(args.db) as conn:
        rows = list(db.plan_next(conn, limit=args.limit))
    if not rows:
        print("No owned unplayed/playing games found.")
        return 0
    for row in rows:
        platform = f" [{row['platform']}]" if row["platform"] else ""
        sale_hint = " sell-candidate" if row["acquisition_status"] == "would_sell" else ""
        print(
            f"item={row['collection_item_id']:<4} game={row['game_id']:<4} "
            f"{row['title']}{platform} played={row['latest_play_status']}{sale_hint}"
        )
    return 0


def cmd_credentials_check(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    matches = provider.search(args.query, limit=1)
    if matches:
        match = matches[0]
        print(f"{args.provider} OK: {match.title} ({match.provider_game_id})")
    else:
        print(f"{args.provider} OK: authenticated, but no result for {args.query!r}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    serve(args.db, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="game-collection")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Initialize the collection database")
    _add_db_arg(init)
    init.set_defaults(func=cmd_init)

    add_manual = subparsers.add_parser("add-manual", help="Add a game without provider lookup")
    _add_db_arg(add_manual)
    add_manual.add_argument("title")
    add_manual.add_argument("--platform")
    add_manual.add_argument("--status", default="owned", choices=["owned", "would_sell", "sold", "loaned", "wishlist"])
    add_manual.add_argument("--played", default="unplayed", choices=["unplayed", "playing", "completed", "retired"])
    add_manual.add_argument("--condition-notes")
    add_manual.add_argument("--location")
    add_manual.set_defaults(func=cmd_add_manual)

    list_cmd = subparsers.add_parser("list", help="List collection summary")
    _add_db_arg(list_cmd)
    list_cmd.set_defaults(func=cmd_list)

    search = subparsers.add_parser("search", help="Search a metadata provider")
    search.add_argument("title")
    search.add_argument("--platform")
    search.add_argument("--provider", default="thegamesdb", choices=PROVIDER_CHOICES)
    search.add_argument("--limit", type=int, default=5)
    search.set_defaults(func=cmd_search)

    new_intake = subparsers.add_parser("new-intake", help="Create a CSV review file for a source photo")
    new_intake.add_argument("photo", type=Path)
    new_intake.add_argument("--out", type=Path, required=True)
    new_intake.set_defaults(func=cmd_new_intake)

    match_review = subparsers.add_parser("match-review", help="Match candidate titles in a review CSV")
    match_review.add_argument("review_csv", type=Path)
    match_review.add_argument("--provider", default="thegamesdb", choices=PROVIDER_CHOICES)
    match_review.add_argument("--out", type=Path, required=True)
    match_review.set_defaults(func=cmd_match_review)

    import_review = subparsers.add_parser("import-review", help="Import accepted rows from a review CSV")
    _add_db_arg(import_review)
    import_review.add_argument("review_csv", type=Path)
    import_review.add_argument("--status", default="owned", choices=["owned", "would_sell", "sold", "loaned", "wishlist"])
    import_review.add_argument("--played", default="unplayed", choices=["unplayed", "playing", "completed", "retired"])
    import_review.set_defaults(func=cmd_import_review)

    mark = subparsers.add_parser("mark", help="Mark a collection item as owned/would_sell/sold/etc.")
    _add_db_arg(mark)
    mark.add_argument("collection_item_id", type=int)
    mark.add_argument("status", choices=["owned", "would_sell", "sold", "loaned", "wishlist"])
    mark.set_defaults(func=cmd_mark)

    play = subparsers.add_parser("play", help="Record play status for a game")
    _add_db_arg(play)
    play.add_argument("game_id", type=int)
    play.add_argument("status", choices=["unplayed", "playing", "completed", "retired"])
    play.add_argument("--notes")
    play.set_defaults(func=cmd_play)

    plan_next = subparsers.add_parser("plan-next", help="List owned games that are not completed")
    _add_db_arg(plan_next)
    plan_next.add_argument("--limit", type=int, default=20)
    plan_next.set_defaults(func=cmd_plan_next)

    credentials = subparsers.add_parser("credentials", help="Credential utilities")
    credential_subparsers = credentials.add_subparsers(dest="credentials_command", required=True)

    check = credential_subparsers.add_parser("check", help="Check metadata provider credentials")
    check.add_argument("--provider", default="igdb", choices=PROVIDER_CHOICES)
    check.add_argument("--query", default="Metroid Prime")
    check.set_defaults(func=cmd_credentials_check)

    serve_cmd = subparsers.add_parser("serve", help="Run the local browser interface")
    _add_db_arg(serve_cmd)
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)
    serve_cmd.set_defaults(func=cmd_serve)

    return parser


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ProviderError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
