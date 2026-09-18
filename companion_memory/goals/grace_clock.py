"""Bounded, owner-checkpointed active-time representation for reminder grace.

Runtime keeps one gate checkpoint; goals consumes exactly its revision in the
same single-writer transaction and retains one matching checkpoint. Closing
records an immutable trusted time before another send can enter. Neither owner
waits for network I/O or scans plans under a gate lock. Only acknowledged
transitions can admit new dispatch; an unknown handoff is recovered by its
original transition ID and input. Open crash downtime consumes active time.

Each window stores its original absolute expiry plus the cumulative pause
counter at birth. Their difference with the acknowledged root represents the
exact remaining time for every plan at a close checkpoint, without a plan scan
or an unbounded transition history. Resume shifts the effective absolute expiry
by precisely the confirmed paused interval. No listener infers pause from polls.
"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GateClock:
    transition_id: str
    revision: int
    observed_us: int
    paused_us: int
    closed_since_us: int | None
    reasons: frozenset[str]

    def transition(self, transition_id: str, observed_us: int, reasons: frozenset[str]) -> 'GateClock':
        if reasons - {'FOCUS', 'MAINTENANCE'} or observed_us < self.observed_us or transition_id == self.transition_id:
            raise ValueError('CLOCK_UNCERTAIN')
        accumulated = self.paused_us
        closed = self.closed_since_us
        if closed is not None and not reasons:
            accumulated += observed_us - closed
            closed = None
        elif closed is None and reasons:
            closed = observed_us
        return GateClock(transition_id, self.revision + 1, observed_us, accumulated, closed, reasons)


@dataclass(frozen=True, slots=True)
class GraceWindow:
    plan_id: str
    configuration_id: str
    started_us: int
    duration_us: int
    original_expires_us: int
    paused_at_birth_us: int
    gate_revision: int

    @classmethod
    def begin(cls, plan_id: str, configuration_id: str, now_us: int, duration_us: int, gate: GateClock):
        if gate.reasons or now_us < gate.observed_us or duration_us != 300000000:
            raise ValueError('NOT_READY')
        return cls(plan_id, configuration_id, now_us, duration_us, now_us + duration_us, gate.paused_us, gate.revision)

    def remaining(self, gate: GateClock, now_us: int) -> tuple[int, int | None]:
        """Return remaining microseconds and active absolute expiry, never renew G."""
        if gate.revision < self.gate_revision or now_us < gate.observed_us or gate.paused_us < self.paused_at_birth_us:
            raise ValueError('CLOCK_UNCERTAIN')
        expires = self.original_expires_us + gate.paused_us - self.paused_at_birth_us
        reference = gate.closed_since_us if gate.closed_since_us is not None else now_us
        return max(0, expires - reference), None if gate.reasons else expires
