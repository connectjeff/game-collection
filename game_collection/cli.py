from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import db
from .automation import auto_import_review, import_accepted_rows
from .barcode_match import (
    BARCODE_CACHE_ROOT,
    BARCODE_CATALOG_PATH,
    build_barcode_cache,
    read_barcode_catalog,
    read_platform_barcode_cache,
)
from .config import load_dotenv
from .cover_match import default_index_path, read_cover_index
from .photo_ingest import PhotoIngestError, image_paths, write_photo_candidates
from .providers import GameMatch, ProviderError, get_provider
from .review import match_to_row, read_review, write_intake_template, write_review
from .sample_validation import (
    load_sample_expectations,
    validate_sample_photos,
    write_sample_expectations_template,
)
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
        release_date=row.get("release_date") or None,
        developer=row.get("developer") or None,
        publisher=row.get("publisher") or None,
        description=row.get("description") or None,
        cover_url=row.get("cover_url") or None,
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
                release_date=match.release_date,
                developer=match.developer,
                publisher=match.publisher,
                description=match.description,
                cover_url=match.cover_url,
                metadata_json=json.dumps(match.raw or {}, ensure_ascii=True),
            )
            db.add_collection_item(conn, game_id=game_id, acquisition_status=args.status)
            db.add_playthrough(conn, game_id=game_id, play_status=args.played)
            imported += 1
    print(f"Imported {imported} accepted rows.")
    return 0


def cmd_auto_import_review(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    result = auto_import_review(
        db_path=args.db,
        review_csv=args.review_csv,
        provider=provider,
        audit_path=args.audit_out,
        accept_threshold=args.accept_threshold,
        status=args.status,
        played=args.played,
        skip_existing=not args.allow_duplicates,
    )
    print(
        f"Imported {result.imported}; skipped_existing={result.skipped_existing}; "
        f"needs_review={result.needs_review}; audit={result.audit_path}"
    )
    return 0


def cmd_ingest_photos(args: argparse.Namespace) -> int:
    photos = image_paths(args.path)
    if not photos:
        print(f"No image files found in {args.path}")
        return 0
    cover_entries = read_cover_index(args.cover_index) if args.cover_index and args.cover_index.exists() else []
    barcode_entries = (
        read_barcode_catalog(args.barcode_catalog)
        if args.barcode_catalog != BARCODE_CATALOG_PATH
        else read_platform_barcode_cache(args.platform)
    )
    candidate_count = write_photo_candidates(
        photo_paths=photos,
        out_path=args.candidates_out,
        crops_dir=args.crops_dir,
        platform=args.platform,
        cover_entries=cover_entries,
        barcode_entries=barcode_entries,
        accept_threshold=args.accept_threshold,
    )
    rows = read_review(args.candidates_out)
    write_review(args.audit_out, rows)
    imported, skipped_existing = import_accepted_rows(
        db_path=args.db,
        rows=rows,
        status=args.status,
        played=args.played,
        skip_existing=not args.allow_duplicates,
    )
    needs_review = sum(1 for row in rows if row.get("decision") != "accept")
    print(
        f"Detected {candidate_count} barcode candidate(s) from {len(photos)} photo(s). "
        f"Barcode catalog entries={len(barcode_entries)}. "
        f"Imported {imported}; skipped_existing={skipped_existing}; "
        f"needs_review={needs_review}; candidates={args.candidates_out}; audit={args.audit_out}"
    )
    return 0


def cmd_build_barcode_cache(args: argparse.Namespace) -> int:
    source_paths = args.source or [BARCODE_CATALOG_PATH]
    results = build_barcode_cache(
        source_paths=source_paths,
        platforms=args.platform or None,
        cache_root=args.cache_root,
        provider=args.source_provider,
    )
    for platform, count in results.items():
        print(f"{platform}: {count} barcode(s)")
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
    serve(
        args.db,
        host=args.host,
        port=args.port,
        prebuild_cover_indexes=not args.skip_cover_prebuild,
        refresh_cover_indexes=args.refresh_cover_indexes,
        refresh_platform_cache=args.refresh_platform_cache,
    )
    return 0


def cmd_validate_samples(args: argparse.Namespace) -> int:
    if args.write_template:
        write_sample_expectations_template(args.expectations)
        print(f"Wrote sample expectations template to {args.expectations}")
        return 0
    provider = get_provider(args.provider)
    expectations = load_sample_expectations(args.expectations)
    result = validate_sample_photos(
        expectations=expectations,
        provider=provider,
        report_path=args.report_out,
        crops_dir=args.crops_dir,
        suggestions_path=args.suggestions_out,
        cover_index_limit=args.cover_index_limit,
        refresh_cover_index=args.refresh_cover_index,
        accept_threshold=args.accept_threshold,
    )
    print(
        f"Validated {result.photo_count} photo(s): detected={result.detected_count}; "
        f"expected={result.expected_count}; found={result.found_count}; "
        f"report={result.report_path}; suggestions={result.suggestions_path}"
    )
    return 0 if result.found_count == result.expected_count else 1


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

    auto_import = subparsers.add_parser("auto-import-review", help="Match review rows and import only rows already marked accept")
    _add_db_arg(auto_import)
    auto_import.add_argument("review_csv", type=Path)
    auto_import.add_argument("--provider", default="igdb", choices=PROVIDER_CHOICES)
    auto_import.add_argument("--audit-out", type=Path, default=Path("review/auto-ingest.audit.csv"))
    auto_import.add_argument("--accept-threshold", type=float, default=0.92)
    auto_import.add_argument("--status", default="owned", choices=["owned", "would_sell", "sold", "loaned", "wishlist"])
    auto_import.add_argument("--played", default="unplayed", choices=["unplayed", "playing", "completed", "retired"])
    auto_import.add_argument("--allow-duplicates", action="store_true")
    auto_import.set_defaults(func=cmd_auto_import_review)

    ingest_photos = subparsers.add_parser("ingest-photos", help="Scan uploaded photos for barcodes")
    _add_db_arg(ingest_photos)
    ingest_photos.add_argument("path", type=Path, help="Image file or folder of image files")
    ingest_photos.add_argument("--provider", default="igdb", choices=PROVIDER_CHOICES)
    ingest_photos.add_argument("--platform")
    ingest_photos.add_argument("--cover-index", type=Path, default=None, help="Optional existing metadata index used only for cover art/title enrichment")
    ingest_photos.add_argument("--barcode-catalog", type=Path, default=BARCODE_CATALOG_PATH)
    ingest_photos.add_argument("--candidates-out", type=Path, default=Path("review/photo-candidates.csv"))
    ingest_photos.add_argument("--audit-out", type=Path, default=Path("review/photo-ingest.audit.csv"))
    ingest_photos.add_argument("--crops-dir", type=Path, default=Path("review/crops"))
    ingest_photos.add_argument("--accept-threshold", type=float, default=0.92)
    ingest_photos.add_argument("--status", default="owned", choices=["owned", "would_sell", "sold", "loaned", "wishlist"])
    ingest_photos.add_argument("--played", default="unplayed", choices=["unplayed", "playing", "completed", "retired"])
    ingest_photos.add_argument("--allow-duplicates", action="store_true")
    ingest_photos.set_defaults(func=cmd_ingest_photos)

    barcode_cache = subparsers.add_parser("build-barcode-cache", help="Build local platform barcode lookup caches from CSV sources")
    barcode_cache.add_argument(
        "--source",
        action="append",
        help="CSV file, folder, or CSV URL containing barcode catalog data; repeat for multiple sources",
    )
    barcode_cache.add_argument("--source-provider", help="Provider label to store for rows that do not already include provider")
    barcode_cache.add_argument(
        "--platform",
        action="append",
        help="Limit cache output to a platform name; repeat for multiple platforms",
    )
    barcode_cache.add_argument("--cache-root", type=Path, default=BARCODE_CACHE_ROOT)
    barcode_cache.set_defaults(func=cmd_build_barcode_cache)

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
    serve_cmd.add_argument("--refresh-platform-cache", action="store_true")
    serve_cmd.add_argument("--refresh-cover-indexes", action="store_true")
    serve_cmd.add_argument("--skip-cover-prebuild", action="store_true")
    serve_cmd.set_defaults(func=cmd_serve)

    validate_samples = subparsers.add_parser(
        "validate-samples",
        help="Run backend barcode-ingest validation against local sample photo expectations",
    )
    validate_samples.add_argument(
        "--expectations",
        type=Path,
        default=Path("review/sample-expectations.json"),
        help="Ignored local JSON mapping sample photos to expected titles",
    )
    validate_samples.add_argument("--provider", default="igdb", choices=["igdb"])
    validate_samples.add_argument("--cover-index-limit", type=int, default=None, help="Optional debug limit; omitted builds the full index")
    validate_samples.add_argument("--refresh-cover-index", action="store_true")
    validate_samples.add_argument("--accept-threshold", type=float, default=0.92)
    validate_samples.add_argument("--crops-dir", type=Path, default=Path("review/sample-validation/crops"))
    validate_samples.add_argument("--report-out", type=Path, default=Path("review/sample-validation/report.csv"))
    validate_samples.add_argument(
        "--suggestions-out",
        type=Path,
        default=Path("review/sample-validation/suggestions.csv"),
        help="CSV of every decoded barcode and suggested match",
    )
    validate_samples.add_argument(
        "--write-template",
        action="store_true",
        help="Write an example expectations JSON at --expectations and exit",
    )
    validate_samples.set_defaults(func=cmd_validate_samples)

    return parser


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "cover_index", None) is None and getattr(args, "command", None) == "ingest-photos":
        args.cover_index = default_index_path(args.provider, args.platform)
    try:
        return args.func(args)
    except (ProviderError, PhotoIngestError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
