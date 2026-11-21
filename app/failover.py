"""Single-link failure simulation over the would-be live state.

Two dedicated lines may share a backup path while they sit on disjoint live
paths; after a reroute they can end up behind the *same* breakpoint and fail
over onto that shared backup at the same time.  To catch that, every state
change that alters live paths or backup paths is checked here before it is
committed: for each link in turn, pretend that link is down and replay what
the network would do.

Rules of the replay
-------------------
* A tunnel whose live path avoids the broken link stays where it is.
* A tunnel whose live path crosses the broken link fails over to its
  registered backup path -- unless the backup also crosses the breakpoint,
  in which case the tunnel has lost its backup and carries nothing.
* A tunnel with no registered backup simply stops flowing.
* Unconfirmed reservations are treated as withdrawn: only live and backup
  paths load the surviving links, never reserved bandwidth.

If every single-link scenario stays within capacity and no backup shares its
tunnel's breakpoint, the state is safe to commit.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import LinkRecord, TunnelRecord


@dataclass(frozen=True)
class LinkOverload:
    """A surviving link whose failover load exceeds its capacity."""

    link_id: str
    capacity: int
    load: int

    @property
    def gap(self) -> int:
        """Bandwidth missing on this link, in Mbps."""
        return self.load - self.capacity

    def to_dict(self) -> dict:
        return {
            "link_id": self.link_id,
            "capacity": self.capacity,
            "load": self.load,
            "gap": self.gap,
        }


@dataclass(frozen=True)
class LinkFailure:
    """Everything that goes wrong when one specific link goes down."""

    link_id: str
    # Affected tunnels whose backup also crosses the breakpoint.
    tunnels_without_viable_backup: tuple[str, ...]
    overloaded_links: tuple[LinkOverload, ...]

    def to_dict(self) -> dict:
        return {
            "link_id": self.link_id,
            "tunnels_without_viable_backup": list(self.tunnels_without_viable_backup),
            "overloaded_links": [overload.to_dict() for overload in self.overloaded_links],
        }


def find_failover_problems(
    links: tuple[LinkRecord, ...],
    tunnels: tuple[TunnelRecord, ...],
    active_paths: dict[str, tuple[str, ...]],
    backups: dict[str, tuple[str, ...]],
) -> list[LinkFailure]:
    """Replay every single-link outage; return one entry per failing link.

    ``active_paths`` is the *would-be* live path per tunnel (after the change
    being checked), ``backups`` the registered backup paths.  Only scenarios
    that lose a backup or overload a surviving link are reported; an empty
    list means the state is safe to commit.
    """
    capacities = {link.link_id: link.capacity for link in links}
    problems: list[LinkFailure] = []
    for broken in links:
        load = {link.link_id: 0 for link in links}
        stranded: list[str] = []
        for tunnel in tunnels:
            path = active_paths[tunnel.tunnel_id]
            if broken.link_id not in path:
                # Unaffected: keeps its live path and its bandwidth.
                for link_id in path:
                    load[link_id] += tunnel.bandwidth
                continue
            backup = backups.get(tunnel.tunnel_id)
            if backup is None:
                continue  # No backup registered: the tunnel stops flowing.
            if broken.link_id in backup:
                stranded.append(tunnel.tunnel_id)
                continue  # Backup crosses the same breakpoint: unusable.
            for link_id in backup:
                load[link_id] += tunnel.bandwidth
        overloaded = tuple(
            LinkOverload(link_id, capacities[link_id], load[link_id])
            for link_id in capacities
            if link_id != broken.link_id and load[link_id] > capacities[link_id]
        )
        if stranded or overloaded:
            problems.append(
                LinkFailure(
                    link_id=broken.link_id,
                    tunnels_without_viable_backup=tuple(stranded),
                    overloaded_links=overloaded,
                )
            )
    return problems
