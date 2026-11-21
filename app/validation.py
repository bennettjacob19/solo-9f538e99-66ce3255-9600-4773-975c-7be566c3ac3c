"""Scenario validation.

Validation runs before anything is persisted, so a bad scenario never creates a
rehearsal.  Errors are collected so the caller can report all of them at once.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import LinkRecord, TunnelRecord
from .errors import BackupInvalid, BackupUnsupported, ScenarioError

MIN_TUNNELS = 2
MAX_TUNNELS = 10


@dataclass(frozen=True)
class Scenario:
    links: tuple[LinkRecord, ...]
    tunnels: tuple[TunnelRecord, ...]


def build_scenario(link_specs, tunnel_specs) -> Scenario:
    """Validate raw request models and normalise them into records."""
    errors: list[str] = []

    if not MIN_TUNNELS <= len(tunnel_specs) <= MAX_TUNNELS:
        errors.append(
            f"between {MIN_TUNNELS} and {MAX_TUNNELS} tunnels are required, "
            f"got {len(tunnel_specs)}"
        )

    links: list[LinkRecord] = []
    seen_links: set[str] = set()
    for i, spec in enumerate(link_specs):
        if spec.link_id in seen_links:
            errors.append(f"links[{i}]: duplicate link id {spec.link_id!r}")
            continue
        seen_links.add(spec.link_id)
        if (spec.source is None) != (spec.target is None):
            errors.append(
                f"links[{i}] ({spec.link_id!r}): give both source and target, or neither"
            )
        elif spec.source is not None and spec.source == spec.target:
            errors.append(
                f"links[{i}] ({spec.link_id!r}): source and target must differ; "
                "a directed self-loop cannot carry a path"
            )
        # Register the link regardless, so path checks do not cascade into
        # confusing "unknown link" errors for a link that was clearly declared.
        links.append(
            LinkRecord(
                link_id=spec.link_id,
                source=spec.source,
                target=spec.target,
                capacity=spec.capacity,
            )
        )
    by_id = {link.link_id: link for link in links}
    topology_known = bool(links) and all(link.has_endpoints for link in links)

    def resolve(path: list[str], where: str) -> tuple[str, ...] | None:
        """Check that every link exists and, when known, the chain is contiguous."""
        ok = True
        for position, link_id in enumerate(path):
            if link_id not in by_id:
                errors.append(f"{where}: unknown link {link_id!r} at position {position}")
                ok = False
        if not ok:
            return None
        if len(set(path)) != len(path):
            errors.append(f"{where}: a link appears more than once")
            ok = False
        if topology_known:
            for previous, current in zip(path, path[1:]):
                if by_id[previous].target != by_id[current].source:
                    errors.append(
                        f"{where}: link {previous!r} ends at {by_id[previous].target!r} "
                        f"but {current!r} starts at {by_id[current].source!r}; "
                        "the path is not contiguous"
                    )
                    ok = False
        return tuple(path) if ok else None

    tunnels: list[TunnelRecord] = []
    seen_tunnels: set[str] = set()
    for i, spec in enumerate(tunnel_specs):
        where = f"tunnels[{i}] ({spec.tunnel_id!r})"
        if spec.tunnel_id in seen_tunnels:
            errors.append(f"tunnels[{i}]: duplicate tunnel id {spec.tunnel_id!r}")
            continue
        seen_tunnels.add(spec.tunnel_id)

        current = resolve(spec.current_path, f"{where}.current_path")
        target = resolve(spec.target_path, f"{where}.target_path")

        if current is not None and target is not None:
            if current == target:
                errors.append(f"{where}: current_path and target_path are identical")
            if topology_known:
                start_old, start_new = by_id[current[0]].source, by_id[target[0]].source
                end_old, end_new = by_id[current[-1]].target, by_id[target[-1]].target
                if start_old != start_new:
                    errors.append(
                        f"{where}: the target path starts at {start_new!r} but the "
                        f"current path starts at {start_old!r}; endpoints must match"
                    )
                if end_old != end_new:
                    errors.append(
                        f"{where}: the target path ends at {end_new!r} but the "
                        f"current path ends at {end_old!r}; endpoints must match"
                    )

        if current is None or target is None:
            continue
        tunnels.append(
            TunnelRecord(
                tunnel_id=spec.tunnel_id,
                bandwidth=spec.bandwidth,
                current_path=current,
                target_path=target,
            )
        )

    if errors:
        raise ScenarioError(errors)

    # The *initial* occupancy must already fit.  Whether the migration as a
    # whole is possible is the planner's job.
    for link in links:
        load = sum(t.bandwidth for t in tunnels if link.link_id in t.current_path)
        if load > link.capacity:
            errors.append(
                f"link {link.link_id!r}: the current paths already need {load} Mbps "
                f"but its capacity is {link.capacity} Mbps"
            )
    if errors:
        raise ScenarioError(errors)

    return Scenario(links=tuple(links), tunnels=tuple(tunnels))


def validate_backups(
    links: tuple[LinkRecord, ...],
    tunnels: tuple[TunnelRecord, ...],
    entries,
) -> dict[str, tuple[str, ...]]:
    """Validate a whole-group backup submission against the live paths.

    ``entries`` are request models with ``tunnel_id`` and ``path``.  Returns
    the replacement backup map (tunnel id -> ordered link ids).  An empty
    submission clears the set and is always accepted.  Structural problems
    raise :class:`BackupInvalid`; rehearsals whose links lack endpoints raise
    :class:`BackupUnsupported` because contiguity cannot be checked there.
    """
    if not entries:
        return {}
    if not links or not all(link.has_endpoints for link in links):
        raise BackupUnsupported(
            "backup paths require every link to declare source and target; "
            "this rehearsal was submitted without full link endpoints"
        )

    by_id = {link.link_id: link for link in links}
    tunnel_by_id = {tunnel.tunnel_id: tunnel for tunnel in tunnels}
    errors: list[str] = []
    backups: dict[str, tuple[str, ...]] = {}
    for i, entry in enumerate(entries):
        where = f"backups[{i}] ({entry.tunnel_id!r})"
        if entry.tunnel_id in backups:
            errors.append(f"{where}: duplicate backup for tunnel {entry.tunnel_id!r}")
            continue
        tunnel = tunnel_by_id.get(entry.tunnel_id)
        if tunnel is None:
            errors.append(f"{where}: unknown tunnel {entry.tunnel_id!r}")
            continue
        path = tuple(entry.path)
        if not path:
            errors.append(f"{where}: the backup path needs at least one link")
            continue

        ok = True
        for position, link_id in enumerate(path):
            if link_id not in by_id:
                errors.append(f"{where}.path: unknown link {link_id!r} at position {position}")
                ok = False
        if not ok:
            continue
        if len(set(path)) != len(path):
            errors.append(f"{where}.path: a link appears more than once")
            ok = False
        for previous, current in zip(path, path[1:]):
            if by_id[previous].target != by_id[current].source:
                errors.append(
                    f"{where}.path: link {previous!r} ends at "
                    f"{by_id[previous].target!r} but {current!r} starts at "
                    f"{by_id[current].source!r}; the backup path is not contiguous"
                )
                ok = False
        # The backup must span the same endpoints as the path carrying
        # traffic now (current and target paths share endpoints, so either
        # side of a pending migration agrees).
        active = tunnel.active_path
        start, end = by_id[active[0]].source, by_id[active[-1]].target
        if by_id[path[0]].source != start:
            errors.append(
                f"{where}.path: the backup starts at {by_id[path[0]].source!r} "
                f"but the live path starts at {start!r}; endpoints must match"
            )
            ok = False
        if by_id[path[-1]].target != end:
            errors.append(
                f"{where}.path: the backup ends at {by_id[path[-1]].target!r} "
                f"but the live path ends at {end!r}; endpoints must match"
            )
            ok = False
        if ok:
            backups[entry.tunnel_id] = path

    if errors:
        raise BackupInvalid(errors)
    return backups
