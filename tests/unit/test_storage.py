"""Storage port contract (spec03 §3).

One suite, run against every backend. That is the point of the port: if
LocalStorage and MemoryStorage do not behave identically here, swapping to
object storage later will surface differences at runtime instead of in tests.

Pure unit tier — MemoryStorage touches nothing, LocalStorage uses tmp_path. No
database, no Docker.
"""

import io
from pathlib import Path

import pytest

from app.core.storage import (
    LocalStorage,
    MemoryStorage,
    StorageBackend,
    StorageKeyNotFound,
    StorageTooLarge,
)

_MAX = 1024 * 1024


@pytest.fixture(params=["local", "memory"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> StorageBackend:
    if request.param == "local":
        return LocalStorage(root=tmp_path)
    return MemoryStorage()


async def test_put_then_open_round_trips(storage: StorageBackend) -> None:
    written = await storage.put("k1.txt", io.BytesIO(b"hello world"), _MAX)
    assert written == 11

    with await storage.open("k1.txt") as fh:
        assert fh.read() == b"hello world"


async def test_put_streams_content_larger_than_one_chunk(storage: StorageBackend) -> None:
    """Exercises the chunked write loop rather than a single small buffer."""
    payload = b"x" * (3 * 1024 * 1024 + 7)

    written = await storage.put("big.bin", io.BytesIO(payload), max_bytes=len(payload))

    assert written == len(payload)
    with await storage.open("big.bin") as fh:
        assert fh.read() == payload


async def test_put_rejects_oversize_and_leaves_nothing_behind(storage: StorageBackend) -> None:
    """The cap is enforced *while writing*, so an oversized file never lands whole.

    The second assertion is the one that matters: a rejected upload must not
    leave a partial object for a later request to find.
    """
    with pytest.raises(StorageTooLarge) as exc:
        await storage.put("toobig.bin", io.BytesIO(b"y" * 5000), max_bytes=1000)

    assert exc.value.max_bytes == 1000
    with pytest.raises(StorageKeyNotFound):
        await storage.open("toobig.bin")


async def test_put_at_exactly_the_cap_is_accepted(storage: StorageBackend) -> None:
    """The boundary is <=, matching the service's 'exceeds the maximum' wording."""
    written = await storage.put("exact.bin", io.BytesIO(b"z" * 1000), max_bytes=1000)
    assert written == 1000


async def test_delete_removes_the_object(storage: StorageBackend) -> None:
    await storage.put("gone.txt", io.BytesIO(b"data"), _MAX)
    await storage.delete("gone.txt")

    with pytest.raises(StorageKeyNotFound):
        await storage.open("gone.txt")


async def test_delete_is_idempotent(storage: StorageBackend) -> None:
    """Rollback calls delete on a key that may never have been written (P14.4)."""
    await storage.delete("never-existed.txt")


async def test_open_missing_key_raises(storage: StorageBackend) -> None:
    with pytest.raises(StorageKeyNotFound):
        await storage.open("absent.txt")


async def test_keys_are_isolated(storage: StorageBackend) -> None:
    await storage.put("a.txt", io.BytesIO(b"AAA"), _MAX)
    await storage.put("b.txt", io.BytesIO(b"BBB"), _MAX)

    with await storage.open("a.txt") as fh:
        assert fh.read() == b"AAA"
    with await storage.open("b.txt") as fh:
        assert fh.read() == b"BBB"


async def test_local_storage_creates_nested_directories(tmp_path: Path) -> None:
    """LocalStorage-specific: keys carry a prefix, so parents must be created."""
    storage = LocalStorage(root=tmp_path)
    await storage.put("uploads/nested/file.txt", io.BytesIO(b"ok"), _MAX)

    assert (tmp_path / "uploads" / "nested" / "file.txt").read_bytes() == b"ok"


async def test_local_storage_rejects_key_escaping_its_root(tmp_path: Path) -> None:
    """A key is not a path the caller controls.

    Attachment keys are generated server-side from a UUID, so this cannot happen
    today. It is guarded anyway because the port's contract is 'opaque key', and
    a future backend or caller could pass something else through.
    """
    storage = LocalStorage(root=tmp_path)

    with pytest.raises(ValueError):
        await storage.put("../escape.txt", io.BytesIO(b"nope"), _MAX)
