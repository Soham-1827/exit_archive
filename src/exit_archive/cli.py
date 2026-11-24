"""Command-line entry point. The token comes from ``HARVEST_TOKEN`` only, never a flag."""

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from exit_archive import __version__
from exit_archive.errors import AuthError, ExitArchiveError
from exit_archive.exporter import export_account
from exit_archive.secrets import HarvestToken, install_log_redaction, redact

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_ERROR = 4
EXIT_INTERRUPTED = 130
TOKEN_VAR = "HARVEST_TOKEN"  # noqa: S105 (an env var name, not a secret)
ACCOUNT_VAR = "HARVEST_ACCOUNT_ID"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exit-archive",
        description="Export every record from a Harvest account as an offline archive.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    export = commands.add_parser(
        "export",
        help="export one Harvest account to a zip file",
        description=(
            f"Export one Harvest account to a zip file. The API token is read from the "
            f"{TOKEN_VAR} environment variable."
        ),
    )
    export.add_argument("--out", required=True, type=Path, help="directory for the zip file")
    export.add_argument(
        "--account-id",
        type=int,
        default=None,
        help=f"Harvest account id (default: ${ACCOUNT_VAR}, or the only account the token sees)",
    )
    return parser


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _progress(stage: str, done: int, total: int | None) -> None:
    _say(f"[{stage}] {done}" + (f"/{total}" if total is not None else ""))


def _account_id(arg: int | None) -> int | None:
    if arg is not None:
        return arg
    raw = os.environ.get(ACCOUNT_VAR, "").strip()
    return int(raw) if raw else None


def _run_export(args: argparse.Namespace) -> int:
    raw_token = os.environ.get(TOKEN_VAR, "").strip()
    if not raw_token:
        _say(f"error: set the {TOKEN_VAR} environment variable to your Harvest API token")
        return EXIT_USAGE
    try:
        account_id = _account_id(args.account_id)
    except ValueError:
        _say(f"error: {ACCOUNT_VAR} must be a whole number")
        return EXIT_USAGE
    token = HarvestToken(raw_token)
    uninstall = install_log_redaction(token)
    for name in ("httpx", "httpcore"):  # their INFO lines carry capability URLs
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        result = asyncio.run(export_account(token, args.out, account_id, _progress))
    except KeyboardInterrupt:
        _say("interrupted")
        return EXIT_INTERRUPTED
    except AuthError as exc:
        _say(f"error: {redact(str(exc), token)}")
        return EXIT_AUTH
    except ExitArchiveError as exc:
        _say(f"error: {redact(str(exc), token)}")
        return EXIT_ERROR
    except OSError as exc:
        _say(f"error: could not write the archive ({type(exc).__name__})")
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - never show a traceback that could carry secrets
        _say(f"error: unexpected failure ({type(exc).__name__})")
        return EXIT_ERROR
    finally:
        uninstall()
    print(result.zip_path)
    if result.missing:
        _say(f"note: {len(result.missing)} item(s) could not be fetched; see MANIFEST.json")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "export":
        return _run_export(args)
    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
