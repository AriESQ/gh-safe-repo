"""gh-safe-repo — Manage safe defaults for GitHub repositories."""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml is the single source of truth; read it from the
    # installed distribution's metadata rather than duplicating it here.
    __version__ = version("gh-safe-repo")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "unknown"
