"""Stable irreversible aliases for persisted and observable entity references."""

from hashlib import blake2s
from uuid import UUID


def entity_alias(value: UUID | None) -> str | None:
    """Return a deterministic alias without retaining the source UUID."""
    return f"entity_{blake2s(value.bytes, digest_size=6).hexdigest()}" if value else None
