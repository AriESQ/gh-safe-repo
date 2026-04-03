"""gh-safe-repo scan — Local secret scanning only."""

import os
import sys

from ..config_manager import ConfigManager
from ..errors import ConfigError
from ..security_scanner import SecurityScanner
from ._common import (
    _BOLD,
    _DIM,
    _GREEN,
    _YELLOW,
    _c,
    _print_findings,
    error,
)

NAME = "scan"
HELP = "Scan a local directory for secrets (no GitHub interaction)"


def add_arguments(parser):
    parser.add_argument(
        "path",
        help="Local directory path to scan",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show scanner details",
    )
    parser.add_argument(
        "--config",
        nargs="?",
        const="",
        metavar="PATH",
        help="Path to config file; bare --config uses built-in defaults only",
    )


def run(args):
    scan_path = os.path.abspath(args.path)
    if not os.path.isdir(scan_path):
        error(f"'{args.path}' is not a directory")
        sys.exit(2)

    try:
        config_path = args.config or None  # bare --config ("") → defaults only
        config = ConfigManager(config_path=config_path, require_exists=config_path is not None)
    except ConfigError as e:
        error(str(e))
        sys.exit(1)

    if args.debug:
        print(f"[debug] config: {config.config_source}", file=sys.stderr)

    scanner = SecurityScanner(config, debug=args.debug)
    print(f"\n{_c(_BOLD, 'Scanning')} {scan_path}...")
    findings = scanner.scan(scan_path)

    if scanner.skipped_committed_dirs:
        print(_c(_YELLOW, "Warning: the following directories were skipped during scan:"))
        for d in scanner.skipped_committed_dirs:
            print(_c(_DIM, f"  {d}/"))
        print()

    has_criticals = _print_findings(findings, config)
    sys.exit(1 if has_criticals else 0)
