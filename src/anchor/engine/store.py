"""Persisting configuration and state safely (SPEC 6.1).

Two requirements shape this module. Writes are atomic, so that losing power
halfway through leaves the previous file intact rather than a truncated one.
And the documents are signed, so that editing them by hand while a session runs
is detected: that is a rupture, and the engine keeps the stricter reading of
the two (P4).

The signature is not a security boundary. Root can read the key, and SPEC 16
says so plainly. It exists to make tampering visible, not impossible.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from anchor.protocol.errors import IntegrityError

_HASH: Final = "sha256"
_KEY_BYTES: Final = 32
_DOCUMENT_VERSION: Final = 1


def canonical_json(data: Any) -> bytes:
    """Serialise ``data`` so the same content always signs identically."""
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def atomic_write(path: Path, data: bytes, *, mode: int = 0o640) -> None:
    """Write ``data`` to ``path`` atomically (SPEC 6.1).

    The content goes to a temporary file in the same directory, is flushed to
    the disk, and is then renamed over the target. The directory itself is
    synced afterwards so the rename survives a power cut too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        tmp.replace(path)

        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_or_create_key(path: Path) -> bytes:
    """Return the HMAC key, creating it on first use with mode 0600."""
    try:
        key = path.read_bytes()
        if len(key) >= _KEY_BYTES:
            # A key file that anyone can read is not the boundary it claims to
            # be, so tighten it rather than trusting how it was installed.
            path.chmod(0o600)
            return key
    except FileNotFoundError:
        pass
    key = secrets.token_bytes(_KEY_BYTES)
    atomic_write(path, key, mode=0o600)
    return key


class LoadStatus(StrEnum):
    """How a stored document came back."""

    OK = "ok"
    MISSING = "missing"
    """No file yet: a fresh install, or the document was deleted."""
    TAMPERED = "tampered"
    """The file exists but does not match its signature, or is unreadable."""


@dataclass(frozen=True, slots=True)
class LoadResult:
    """The outcome of reading a signed document."""

    status: LoadStatus
    data: dict[str, Any]
    detail: str = ""

    @property
    def trustworthy(self) -> bool:
        return self.status is LoadStatus.OK


class SignedStore:
    """Reads and writes one HMAC-signed JSON document."""

    __slots__ = ("_key", "_mode", "_path")

    def __init__(self, path: Path, key: bytes, *, mode: int = 0o640) -> None:
        self._path = path
        self._key = key
        self._mode = mode

    @property
    def path(self) -> Path:
        return self._path

    def _sign(self, payload: bytes) -> str:
        return hmac.new(self._key, payload, _HASH).hexdigest()

    def save(self, data: dict[str, Any]) -> None:
        """Sign ``data`` and write it atomically."""
        body = canonical_json(data)
        document = {
            "version": _DOCUMENT_VERSION,
            "hmac": self._sign(body),
            "data": data,
        }
        atomic_write(self._path, canonical_json(document) + b"\n", mode=self._mode)

    def load(self) -> LoadResult:
        """Read the document, reporting rather than raising on a bad signature.

        The caller decides what a failure means: outside a session the engine
        can start from defaults, while during one it is a rupture and the
        stricter interpretation is kept (SPEC 6.1, P4).
        """
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return LoadResult(LoadStatus.MISSING, {})
        except OSError as exc:
            return LoadResult(LoadStatus.TAMPERED, {}, f"cannot read {self._path}: {exc}")

        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            return LoadResult(LoadStatus.TAMPERED, {}, f"not valid JSON: {exc.msg}")

        if not isinstance(document, dict):
            return LoadResult(LoadStatus.TAMPERED, {}, "document is not an object")

        signature = document.get("hmac")
        data = document.get("data")
        if not isinstance(signature, str) or not isinstance(data, dict):
            return LoadResult(LoadStatus.TAMPERED, {}, "document is missing 'hmac' or 'data'")

        expected = self._sign(canonical_json(data))
        if not hmac.compare_digest(expected, signature):
            return LoadResult(LoadStatus.TAMPERED, data, "signature does not match the contents")

        return LoadResult(LoadStatus.OK, data)

    def load_strict(self) -> dict[str, Any]:
        """Like :meth:`load`, but raise when the document cannot be trusted."""
        result = self.load()
        if result.status is LoadStatus.TAMPERED:
            raise IntegrityError(f"{self._path} failed its integrity check: {result.detail}")
        return result.data
