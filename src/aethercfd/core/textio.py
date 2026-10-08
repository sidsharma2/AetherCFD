"""Byte-exact text I/O for case files.

Case files are never normalised: what we decode is exactly what we re-encode, so saving an
unchanged document reproduces the original bytes (encoding, BOM and line endings included).
New text we insert follows the file's own newline style.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


_BINARY_HEADER = re.compile(rb"\bformat\s+binary\s*;")


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Return (text, encoding) such that ``encode_text(text, encoding) == data`` exactly.

    - OpenFOAM ``format binary`` files decode as latin-1: one character per byte, so binary list
      payloads can be skipped by their declared byte length and re-encode byte-for-byte.
    - Otherwise UTF-8 (with/without BOM), then cp1252 (legacy Windows text), then latin-1 (always
      lossless) for anything else.
    """
    if _BINARY_HEADER.search(data[:4096]):
        return data.decode("latin-1"), "latin-1"
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data[len(bom):].decode("utf-8" if enc == "utf-8-sig" else enc), enc
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def encode_text(text: str, encoding: str) -> bytes:
    if encoding == "utf-8-sig":
        return b"\xef\xbb\xbf" + text.encode("utf-8")
    if encoding == "utf-16-le":
        return b"\xff\xfe" + text.encode("utf-16-le")
    if encoding == "utf-16-be":
        return b"\xfe\xff" + text.encode("utf-16-be")
    return text.encode(encoding, errors="strict")


def detect_newline(text: str) -> str:
    """'\\r\\n' if CRLF dominates, else '\\n' (also the default for files with no newline)."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


@dataclass(frozen=True)
class TextSnapshot:
    path: Path
    text: str
    encoding: str
    newline: str
    sha256: str
    size: int
    mtime_ns: int

    def encode(self, text: str | None = None) -> bytes:
        return encode_text(self.text if text is None else text, self.encoding)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_snapshot(path: str | os.PathLike) -> TextSnapshot:
    p = Path(path)
    st = p.stat()
    data = p.read_bytes()
    text, enc = decode_bytes(data)
    return TextSnapshot(path=p, text=text, encoding=enc, newline=detect_newline(text),
                        sha256=sha256_bytes(data), size=len(data), mtime_ns=st.st_mtime_ns)


def atomic_write_bytes(path: str | os.PathLike, data: bytes, *, retries: int = 5) -> None:
    """Write via a temp file in the same directory and `os.replace`, so readers (including a running
    solver re-reading controlDict) never see a half-written file. Retries briefly when Windows reports
    the target as in use (editors, antivirus, OneDrive)."""
    p = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".aether-tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def write_generated_text(path: str | os.PathLike, text: str) -> None:
    """Files AetherCFD generates (scripts, new dictionaries) are always UTF-8 with LF only."""
    if "\r" in text:
        raise ValueError(f"generated text for {path} contains a carriage return")
    atomic_write_bytes(path, text.encode("utf-8"))
