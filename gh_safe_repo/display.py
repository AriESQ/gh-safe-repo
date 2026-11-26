"""Make untrusted text safe to print on a terminal (#125).

File names, file contents, git output and API responses come from the repo
being scanned or from a remote, so they can carry bytes a terminal acts on:
ESC/CSI/OSC sequences (erase a line, set the title, OSC 8 links, OSC 52
clipboard writes), carriage returns and backspaces (overwrite what was just
printed), C1 controls, and invisible Unicode formatting such as bidi
overrides. `safe_display()` renders every such character as a visible
backslash escape, so the user sees that something odd is there instead of the
terminal executing it.

Apply it where a value is printed, never where it is collected: stored
`Finding` values and `--json` output keep the raw text (`json.dumps` escapes
it on its own). The tool's own colour codes are added around the sanitized
text and are not affected.

This module is a leaf (stdlib only) so `security_scanner.py`, the plugins and
`commands/_common.py` can all import it.
"""

import unicodedata

# Unicode general categories that are never shown as-is:
#   Cc  C0 and C1 controls and DEL (ESC, CR, BS, BEL, NUL, 0x9b CSI, ...)
#   Cf  invisible formatting: bidi overrides/isolates, zero-width characters,
#       BOM, soft hyphen, the tag block
#   Cs  lone surrogates (undecodable file-name bytes; print() cannot encode them)
#   Zl  LINE SEPARATOR, Zp  PARAGRAPH SEPARATOR
# The same set, minus private-use and unassigned code points, that the AI
# assistant file catalog rejects in its text fields (#124).
_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _escape(ch: str) -> str:
    cp = ord(ch)
    if 0xDC80 <= cp <= 0xDCFF:
        # A byte that was not valid UTF-8, carried through by the
        # surrogateescape error handler (os.walk, `git ls-files -z`): show
        # the original byte.
        return f"\\x{cp - 0xDC00:02x}"
    if cp <= 0xFF:
        return f"\\x{cp:02x}"
    if cp <= 0xFFFF:
        return f"\\u{cp:04x}"
    return f"\\U{cp:08x}"


def safe_display(value, *, multiline: bool = False) -> str:
    """Return `value` as text that cannot drive the terminal.

    Control and invisible formatting characters become visible escapes
    (`\\x1b`, `\\x0d`, `\\u202e`); everything else, including tab and all
    printable non-ASCII text, is unchanged. Newlines are escaped too (`\\x0a`)
    unless `multiline` is true: a single value such as a file name must not
    be able to start a new output line, while a message block (an error with
    git's stderr in it) keeps its line breaks.

    A literal backslash is not doubled, so the result is for reading, not for
    parsing back.
    """
    text = value if isinstance(value, str) else str(value)
    if text.isprintable():  # fast path: nothing to escape
        return text
    out = []
    for ch in text:
        if ch == "\t" or (multiline and ch == "\n"):
            out.append(ch)
        elif unicodedata.category(ch) in _ESCAPED_CATEGORIES:
            out.append(_escape(ch))
        else:
            out.append(ch)
    return "".join(out)
