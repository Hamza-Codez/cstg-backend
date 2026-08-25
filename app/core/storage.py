"""Pluggable blob storage (spec03 §3).

Cross-cutting, like ``clock.py``, and for the same reason: something the app
depends on that tests must be able to swap.

``storage_path`` on an attachment row is a **backend-opaque key**. For
``LocalStorage`` it is still a path under ``uploads/``; for a future
object-storage backend it is a bucket key. Nothing above this module may assume
a filesystem.

The chunked, cap-enforcing write moved here from ``attachment_service`` and is
unchanged, including its thread dispatch. The reason recorded there still
governs: this process also hosts the SLA monitor (docs/ARCHITECTURE.md §6), so
blocking the event loop on a large upload would delay breach detection.
"""

import asyncio
import io
from pathlib import Path
from typing import BinaryIO, Protocol

_CHUNK_BYTES = 1024 * 1024


class StorageError(Exception):
    """Base class for storage failures."""


class StorageTooLarge(StorageError):
    """The source exceeded ``max_bytes``; nothing was retained."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"Object exceeds the maximum size of {max_bytes} bytes")
        self.max_bytes = max_bytes


class StorageKeyNotFound(StorageError):
    """No object is stored under this key."""

    def __init__(self, key: str) -> None:
        super().__init__(f"No stored object for key {key!r}")
        self.key = key


class StorageBackend(Protocol):
    """The contract every backend satisfies identically (tests/unit/test_storage.py)."""

    async def put(self, key: str, source: BinaryIO, max_bytes: int) -> int:
        """Store ``source`` under ``key``, returning bytes written.

        Enforces the cap **while streaming**, so an oversized object never lands
        whole. On any failure nothing is retained — a rejected upload must not
        leave a partial object for a later request to find.
        """
        ...

    async def open(self, key: str) -> BinaryIO:
        """Open the stored object for reading. Raises ``StorageKeyNotFound``."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the object. Idempotent — deleting an absent key is not an error."""
        ...


def _stream(source: BinaryIO, sink: BinaryIO, max_bytes: int) -> int:
    """Copy in chunks, failing as soon as the cap is passed."""
    written = 0
    while chunk := source.read(_CHUNK_BYTES):
        written += len(chunk)
        if written > max_bytes:
            raise StorageTooLarge(max_bytes)
        sink.write(chunk)
    return written


class LocalStorage:
    """Filesystem-backed. The v1 behaviour, now behind the port.

    NOTE for deployment: this loses every object on redeploy where the
    filesystem is ephemeral (IMPLEMENTATION_V2.md R14). Production needs either
    a persistent volume mounted at ``root`` or an object-storage backend.
    """

    def __init__(self, root: Path = Path(".")) -> None:
        self._root = root.resolve()

    def _resolve(self, key: str) -> Path:
        """Map a key to a path, refusing anything that escapes the root.

        Attachment keys are generated server-side from a UUID, so traversal
        cannot happen today. It is checked anyway because the port's contract is
        'opaque key' — a future caller could pass something else through, and
        this is the layer that owns the filesystem.
        """
        candidate = (self._root / key).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ValueError(f"Storage key escapes the storage root: {key!r}")
        return candidate

    async def put(self, key: str, source: BinaryIO, max_bytes: int) -> int:
        destination = self._resolve(key)
        return await asyncio.to_thread(self._put_sync, destination, source, max_bytes)

    @staticmethod
    def _put_sync(destination: Path, source: BinaryIO, max_bytes: int) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("wb") as out:
                return _stream(source, out, max_bytes)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    async def open(self, key: str) -> BinaryIO:
        path = self._resolve(key)
        try:
            return path.open("rb")
        except FileNotFoundError as exc:
            raise StorageKeyNotFound(key) from exc

    async def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)


class MemoryStorage:
    """In-process. For tests: no disk, no cleanup, no ordering surprises."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def put(self, key: str, source: BinaryIO, max_bytes: int) -> int:
        sink = io.BytesIO()
        try:
            written = _stream(source, sink, max_bytes)
        except BaseException:
            # Mirrors LocalStorage: a failed put retains nothing.
            self._objects.pop(key, None)
            raise
        self._objects[key] = sink.getvalue()
        return written

    async def open(self, key: str) -> BinaryIO:
        try:
            return io.BytesIO(self._objects[key])
        except KeyError as exc:
            raise StorageKeyNotFound(key) from exc

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)


def build_storage(backend: str, root: str) -> StorageBackend:
    """Construct the configured backend.

    Takes plain values rather than importing Settings: ``core`` must not depend
    on configuration loading, the same rule ``clock.py`` follows.
    """
    if backend == "memory":
        return MemoryStorage()
    return LocalStorage(root=Path(root))
