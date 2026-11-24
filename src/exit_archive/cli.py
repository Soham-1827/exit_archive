"""Command-line entry point."""

import argparse
from collections.abc import Sequence

from exit_archive import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exit-archive",
        description="Export every record from a Harvest account as an offline archive.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Only ``--version`` exists so far."""
    build_parser().parse_args(argv)
    return 0
