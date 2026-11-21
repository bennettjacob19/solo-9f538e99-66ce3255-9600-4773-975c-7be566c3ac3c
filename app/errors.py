"""Domain errors mapped to HTTP status codes by the API layer."""

from __future__ import annotations


class RehearsalError(Exception):
    """Base class for all expected domain failures."""

    code = "rehearsal_error"
    status_code = 400


class ScenarioError(RehearsalError):
    """The submitted scenario is structurally invalid; nothing was created."""

    code = "invalid_scenario"
    status_code = 422

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors) or "invalid scenario")
        self.errors = errors


class Infeasible(RehearsalError):
    """No ordering can move every tunnel; no rehearsal was created."""

    code = "no_feasible_order"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RehearsalNotFound(RehearsalError):
    code = "rehearsal_not_found"
    status_code = 404


class StepConflict(RehearsalError):
    """Reservation requested while another is pending, or out of order."""

    code = "step_conflict"
    status_code = 409


class ReservationInvalid(RehearsalError):
    """Reservation id is unknown, expired, cancelled or already confirmed."""

    code = "reservation_invalid"
    status_code = 409


class BackupUnsupported(RehearsalError):
    """The rehearsal's links lack endpoints, so backups cannot be checked."""

    code = "backup_unsupported"
    status_code = 409


class BackupInvalid(RehearsalError):
    """The submitted backup set is structurally invalid; nothing was saved."""

    code = "invalid_backup"
    status_code = 422

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors) or "invalid backup set")
        self.errors = errors


class FailoverUnsafe(RehearsalError):
    """A single-link outage would strand a tunnel or overload a link.

    Raised when backup paths are submitted or a migration is confirmed and the
    would-be state fails the single-link failure simulation.  Nothing is
    committed, so a pending reservation stays pending and can still be
    cancelled with its original id.
    """

    code = "failover_unsafe"
    status_code = 409

    def __init__(self, failures) -> None:
        self.failures = [failure.to_dict() for failure in failures]
        parts = []
        for failure in failures:
            bits = []
            if failure.tunnels_without_viable_backup:
                bits.append(
                    "tunnels losing their backup: "
                    + ", ".join(repr(t) for t in failure.tunnels_without_viable_backup)
                )
            if failure.overloaded_links:
                bits.append(
                    "overloaded links: "
                    + ", ".join(
                        f"{o.link_id!r} short by {o.gap} Mbps "
                        f"({o.load}/{o.capacity} Mbps)"
                        for o in failure.overloaded_links
                    )
                )
            parts.append(f"if link {failure.link_id!r} fails: " + "; ".join(bits))
        super().__init__("; ".join(parts))
