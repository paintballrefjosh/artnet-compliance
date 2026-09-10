"""
Filesystem path safety: keeping user-supplied names (fixture profile ids,
archive entry names) from escaping the directory they're meant to land in.

Windows makes this sharper than it looks -- pathlib treats BOTH "/" and "\\"
as separators there, so sanitising only "/" (as this code originally did)
still let "..\\..\\evil.json" walk out of the profiles directory.
"""
import os
import re
from pathlib import Path

# Path separators, the Windows drive/alternate-data-stream colon, and NULs.
# Deliberately NOT a whitelist: profile filenames already on disk contain
# characters like "+" (e.g. elation-six+-bar-l.json), and rewriting the
# naming scheme would orphan every existing file.
_DANGEROUS_FILENAME_CHARS = re.compile(r"[/\\:\x00]")


def safe_filename(name: str, fallback: str = "unnamed") -> str:
    """Reduces an arbitrary string to a single safe filename component.

    Path separators and colons become "_", and a name made only of dots
    ("." / "..") is rejected outright -- those traverse rather than name a
    file. Everything else is preserved so existing files keep their names.
    """
    cleaned = _DANGEROUS_FILENAME_CHARS.sub("_", name).strip()
    if set(cleaned) <= {"."}:  # "", ".", "..", "..." -- all traversal, no name
        cleaned = ""
    return cleaned or fallback


def resolve_within(base, *parts: str):
    """Joins `parts` onto `base`, returning the result only if it genuinely
    lands inside `base`. Returns None otherwise.

    This is the backstop behind safe_filename: it catches absolute paths,
    ".." segments, symlinked escapes, and platform-specific separator
    quirks by asking the filesystem where the path actually resolves to,
    rather than trying to pattern-match every hostile spelling.
    """
    base_path = Path(base).resolve()
    try:
        candidate = base_path.joinpath(*parts).resolve()
    except (OSError, ValueError):
        return None
    if candidate == base_path or base_path in candidate.parents:
        return candidate
    return None


def read_text_lenient(path: str) -> str:
    """Reads a config file as UTF-8, falling back to cp1252.

    Every read/write here used to go through bare open(), which on Windows
    means cp1252 -- so patch.yaml has always been written in whatever the
    platform default was. The header comments' em-dashes survived only by
    coincidence (they round-trip byte-identically between UTF-8 and cp1252);
    a fixture name with any character outside cp1252 would have raised
    UnicodeEncodeError and failed the write outright. Everything writes
    UTF-8 now, and this fallback keeps any file already written the old way
    readable until its next write normalises it.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    # Normalise newlines the way text-mode reading would. Without this,
    # reading binary keeps the CRLFs and atomic_write_text (newline=None)
    # translates each "\n" again on the way out -- turning every line ending
    # into "\r\r\n", a little worse on every single write.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def atomic_write_text(path: str, text: str, encoding: str = "utf-8"):
    """Writes `text` to `path` atomically: content goes to a temp file in
    the same directory, is flushed and fsynced, then renamed over the
    target. os.replace() is atomic on both POSIX and Windows.

    Matters because patch.yaml is the only copy of a user's entire show
    setup -- a crash or power loss partway through a plain open(path, "w")
    would leave it truncated, and every dashboard edit rewrites it.
    """
    import tempfile

    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".artnet-", suffix=".tmp")
    try:
        # newline=None (the default) keeps the platform-native line endings
        # these files have always been written with -- CRLF on Windows.
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
