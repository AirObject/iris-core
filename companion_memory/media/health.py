"""Safe media lifecycle projection, with no private task or file authority."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MediaHealth:
    """Current ownership and bounded maintenance observations, never commit evidence."""
    ready: bool
    collection_in_flight: bool
    file_workers: int
    cleanup_pending: bool
    processing_suspects: int | None
    processing_observed_at_us: int | None
