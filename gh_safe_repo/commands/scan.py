"""gh-safe-repo scan — Local secret scanning only."""

import os
import sys

from ..config_manager import ConfigManager
from ..errors import ConfigError
from ..security_scanner import SecurityScanner, Severity
from ._common import (
    _BOLD,
    _DIM,
    _GREEN,
    _YELLOW,
    _c,
    _print_findings,
    add_common_args,
    error,
    format_findings_json,
    info,
    resolve_config_arg,
)

NAME = "scan"
HELP = "Scan a local directory for secrets (no GitHub interaction)"


def add_arguments(parser):
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Directory to scan (default: current directory)",
    )
    add_common_args(
        parser,
        json_help="Emit findings as JSON instead of the ANSI report",
    )


def run(args):
    scan_path = os.path.abspath(args.path)
    if not os.path.isdir(scan_path):
        error(f"'{args.path}' is not a directory")
        sys.exit(2)

    try:
        config_path = resolve_config_arg(args) or None  # "" → defaults only
        config = ConfigManager(config_path=config_path, require_exists=config_path is not None)
    except ConfigError as e:
        error(str(e))
        sys.exit(1)

    if args.debug:
        print(f"[debug] config: {config.config_source}", file=sys.stderr)

    json_mode = args.json
    _info = lambda msg: info(msg, json_mode=json_mode)

    scanner = SecurityScanner(config, debug=args.debug, progress=not json_mode)
    _info(f"\n{_c(_BOLD, 'Scanning')} {scan_path}...")
    findings = scanner.scan(scan_path)
    _info(_c(_DIM, f"Scan engine: {scanner.engine_summary}"))

    if scanner.skipped_committed_dirs:
        _info(_c(_YELLOW, "Warning: the following directories were skipped during scan:"))
        for d in scanner.skipped_committed_dirs:
            _info(_c(_DIM, f"  {d}/"))
        _info("")

    if json_mode:
        print(format_findings_json(
            findings, scanner.skipped_committed_dirs, engine=scanner.engine_report()
        ))
        has_criticals = any(f.severity == Severity.CRITICAL for f in findings)
    else:
        has_criticals = _print_findings(findings, config)

    # An explicit trufflehog_mode (native/docker) that fell back to regex is a
    # failure even with no findings: the scan was weaker than configured. The
    # results are still printed above (they are real), but the exit is 1.
    engine_msg = scanner.engine_error()
    if engine_msg:
        error(engine_msg)
        sys.exit(1)
    sys.exit(1 if has_criticals else 0)
