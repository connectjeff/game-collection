from __future__ import annotations

import argparse
from pathlib import Path

from . import db
from .config import load_dotenv
from .providers import ProviderError, get_provider
from .web import serve


PROVIDER_CHOICES = ["igdb"]


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help="SQLite database path")


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


def cmd_credentials_check(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    matches = provider.search(args.query, limit=1)
    if matches:
        match = matches[0]
        print(f"{args.provider} OK: {match.title} ({match.provider_game_id})")
    else:
        print(f"{args.provider} OK: authenticated, but no result for {args.query!r}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="game-collection")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_cmd = subparsers.add_parser("serve", help="Run the local web app")
    _add_db_arg(serve_cmd)
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)
    serve_cmd.add_argument("--refresh-platform-cache", action="store_true")
    serve_cmd.add_argument("--refresh-cover-indexes", action="store_true")
    serve_cmd.add_argument("--skip-cover-prebuild", action="store_true")
    serve_cmd.set_defaults(func=cmd_serve)

    credentials = subparsers.add_parser("credentials", help="Credential utilities")
    credential_subparsers = credentials.add_subparsers(dest="credentials_command", required=True)

    check = credential_subparsers.add_parser("check", help="Check metadata provider credentials")
    check.add_argument("--provider", default="igdb", choices=PROVIDER_CHOICES)
    check.add_argument("--query", default="Metroid Prime")
    check.set_defaults(func=cmd_credentials_check)

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
