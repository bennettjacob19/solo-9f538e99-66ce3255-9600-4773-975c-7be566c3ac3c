"""Backup paths: registration, clearing, and the single-link failure checks."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.domain import RehearsalRecord
from app.engine import Engine
from app.errors import FailoverUnsafe
from app.main import create_app, create_memory_app
from app.store import MemoryStore, SqliteStore
from tests.conftest import links, tunnel


def scenario(l3_capacity=10):
    """Two tunnels swapping between A->B->C and A->C."""
    return {
        "links": links(
            ("L1", "A", "B", 10),
            ("L2", "B", "C", 10),
            ("L3", "A", "C", l3_capacity),
        ),
        "tunnels": [
            tunnel("T1", 4, ["L1", "L2"], ["L3"]),
            tunnel("T2", 3, ["L3"], ["L1", "L2"]),
        ],
    }


def shared_backup_scenario(l4_capacity=10):
    """The motivating case: T1 and T2 can each fail over to L4 alone, but once
    T1 moves next to T2 a single break pushes *both* onto L4 at once."""
    return {
        "links": links(
            ("L1", "A", "B", 12),
            ("L2", "B", "C", 12),
            ("L3", "A", "C", 10),
            ("L4", "A", "C", l4_capacity),
        ),
        "tunnels": [
            tunnel("T1", 6, ["L3"], ["L1", "L2"]),
            tunnel("T2", 6, ["L1", "L2"], ["L3"]),
        ],
    }


def create(client, sc):
    response = client.post("/rehearsals", json=sc)
    assert response.status_code == 201, response.text
    return response.json()["rehearsal_id"]


def put_backups(client, rid, backups=None):
    if backups is None:
        return client.put(f"/rehearsals/{rid}/backups")
    return client.put(f"/rehearsals/{rid}/backups", json={"backups": backups})


def backup_paths(client, rid):
    view = client.get(f"/rehearsals/{rid}").json()
    return {t["tunnel_id"]: t["backup_path"] for t in view["tunnels"]}


# ------------------------------------------------------------------ happy path
def test_submit_backups_and_view_shows_them(client):
    rid = create(client, scenario())
    assert backup_paths(client, rid) == {"T1": None, "T2": None}

    response = put_backups(
        client, rid, [{"tunnel_id": "T1", "path": ["L3"]}, {"tunnel_id": "T2", "path": ["L1", "L2"]}]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rehearsal_id"] == rid
    assert body["backups"] == [
        {"tunnel_id": "T1", "path": ["L3"]},
        {"tunnel_id": "T2", "path": ["L1", "L2"]},
    ]
    assert backup_paths(client, rid) == {"T1": ["L3"], "T2": ["L1", "L2"]}


def test_submission_replaces_the_whole_group(client):
    rid = create(client, scenario())
    put_backups(client, rid, [{"tunnel_id": "T1", "path": ["L3"]}, {"tunnel_id": "T2", "path": ["L1", "L2"]}])
    # Second submission replaces: T2 is no longer listed, so it has no backup.
    response = put_backups(client, rid, [{"tunnel_id": "T2", "path": ["L1", "L2"]}])
    assert response.status_code == 200
    assert backup_paths(client, rid) == {"T1": None, "T2": ["L1", "L2"]}


def test_clear_backups_with_empty_list_and_with_empty_body(client):
    rid = create(client, scenario())
    put_backups(client, rid, [{"tunnel_id": "T1", "path": ["L3"]}])
    assert put_backups(client, rid, []).status_code == 200
    assert backup_paths(client, rid) == {"T1": None, "T2": None}

    put_backups(client, rid, [{"tunnel_id": "T1", "path": ["L3"]}])
    assert put_backups(client, rid).status_code == 200  # no body at all
    assert backup_paths(client, rid) == {"T1": None, "T2": None}


def test_aliases_are_accepted(client):
    rid = create(client, scenario())
    response = client.put(
        f"/rehearsals/{rid}/backups",
        json={"backups": [{"id": "T1", "links": ["L3"]}]},
    )
    assert response.status_code == 200, response.text
    assert backup_paths(client, rid)["T1"] == ["L3"]


def test_unknown_rehearsal_is_404(client):
    assert put_backups(client, "reh_nope", []).status_code == 404


# ------------------------------------------------------- structural validation
def test_unknown_tunnel_rejected(client):
    rid = create(client, scenario())
    response = put_backups(client, rid, [{"tunnel_id": "TX", "path": ["L3"]}])
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_backup"
    assert any("unknown tunnel" in e for e in response.json()["errors"])
    assert backup_paths(client, rid) == {"T1": None, "T2": None}


def test_unknown_link_rejected(client):
    rid = create(client, scenario())
    response = put_backups(client, rid, [{"tunnel_id": "T1", "path": ["LX"]}])
    assert response.status_code == 422
    assert any("unknown link" in e for e in response.json()["errors"])


def test_discontiguous_backup_rejected(client):
    sc = scenario()
    sc["links"].append({"link_id": "L4", "source": "C", "target": "D", "capacity": 10})
    rid = create(client, sc)
    response = put_backups(client, rid, [{"tunnel_id": "T1", "path": ["L1", "L4"]}])
    assert response.status_code == 422
    assert any("not contiguous" in e for e in response.json()["errors"])


def test_backup_endpoints_must_match_live_path(client):
    sc = scenario()
    sc["links"].append({"link_id": "L4", "source": "C", "target": "D", "capacity": 10})
    rid = create(client, sc)
    # T1 runs A->C; a backup starting at C cannot serve it.
    response = put_backups(client, rid, [{"tunnel_id": "T1", "path": ["L4"]}])
    assert response.status_code == 422
    assert any("endpoints must match" in e for e in response.json()["errors"])


def test_duplicate_tunnel_entry_rejected(client):
    rid = create(client, scenario())
    response = put_backups(
        client, rid, [{"tunnel_id": "T1", "path": ["L3"]}, {"tunnel_id": "T1", "path": ["L3"]}]
    )
    assert response.status_code == 422
    assert any("duplicate" in e for e in response.json()["errors"])


def test_empty_path_rejected(client):
    rid = create(client, scenario())
    response = client.put(
        f"/rehearsals/{rid}/backups", json={"backups": [{"tunnel_id": "T1", "path": []}]}
    )
    assert response.status_code == 422


def test_backups_require_full_link_endpoints(client):
    sc = {
        "links": [{"link_id": "L1", "capacity": 10}, {"link_id": "L2", "capacity": 10}],
        "tunnels": [tunnel("T1", 4, ["L1"], ["L2"]), tunnel("T2", 3, ["L2"], ["L1"])],
    }
    rid = create(client, sc)
    response = put_backups(client, rid, [{"tunnel_id": "T1", "path": ["L2"]}])
    assert response.status_code == 409
    assert response.json()["error"] == "backup_unsupported"
    # Clearing is always allowed, even without endpoints.
    assert put_backups(client, rid, []).status_code == 200


# ------------------------------------------------------- single-link failures
def test_backup_crossing_the_same_breakpoint_is_rejected(client):
    rid = create(client, scenario())
    # T1's "backup" is its own live path: any break on it strands T1.
    response = put_backups(client, rid, [{"tunnel_id": "T1", "path": ["L1", "L2"]}])
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "failover_unsafe"
    by_link = {f["link_id"]: f for f in body["failures"]}
    assert by_link["L1"]["tunnels_without_viable_backup"] == ["T1"]
    assert by_link["L2"]["tunnels_without_viable_backup"] == ["T1"]
    assert "L1" in body["detail"] and "T1" in body["detail"]
    # Nothing was saved.
    assert backup_paths(client, rid) == {"T1": None, "T2": None}


def test_shared_backup_overload_is_rejected(client):
    # L3 (cap 6) carries T2 (3); if L1/L2 break, T1 (4) would join it: 7 > 6.
    rid = create(client, scenario(l3_capacity=6))
    response = put_backups(client, rid, [{"tunnel_id": "T1", "path": ["L3"]}])
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "failover_unsafe"
    by_link = {f["link_id"]: f for f in body["failures"]}
    assert by_link["L1"]["overloaded_links"] == [
        {"link_id": "L3", "capacity": 6, "load": 7, "gap": 1}
    ]
    assert by_link["L2"]["overloaded_links"][0]["gap"] == 1
    assert backup_paths(client, rid) == {"T1": None, "T2": None}


def test_tunnel_without_backup_simply_stops_flowing(client):
    """No registered backup is not an error: the tunnel drops in an outage."""
    rid = create(client, scenario())
    # Only T2 gets a backup; T1 has none and would stop flowing.  Still safe.
    response = put_backups(client, rid, [{"tunnel_id": "T2", "path": ["L1", "L2"]}])
    assert response.status_code == 200, response.text


# --------------------------------------------------------------- confirm-time
def test_confirm_blocked_when_switch_breaks_failover(client):
    rid = create(client, shared_backup_scenario())
    # Safe while T1 sits on L3: either tunnel can fail over to L4 alone.
    assert put_backups(
        client, rid, [{"tunnel_id": "T1", "path": ["L4"]}, {"tunnel_id": "T2", "path": ["L4"]}]
    ).status_code == 200

    reserved = client.post(f"/rehearsals/{rid}/reservations").json()
    assert reserved["tunnel_id"] == "T1"
    response = client.post(f"/rehearsals/{rid}/reservations/{reserved['reservation_id']}/confirm")
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "failover_unsafe"
    # After the switch both tunnels would sit behind L1 (and L2); one break
    # pushes 12 Mbps onto L4 which only fits 10.
    by_link = {f["link_id"]: f for f in body["failures"]}
    assert by_link["L1"]["overloaded_links"] == [
        {"link_id": "L4", "capacity": 10, "load": 12, "gap": 2}
    ]
    assert by_link["L2"]["overloaded_links"][0]["link_id"] == "L4"

    # The switch did not happen and the reservation is still pending.
    view = client.get(f"/rehearsals/{rid}").json()
    assert view["pending"]["reservation_id"] == reserved["reservation_id"]
    assert all(not t["moved"] for t in view["tunnels"])

    # ...so the original id still cancels it.
    cancelled = client.post(f"/rehearsals/{rid}/reservations/{reserved['reservation_id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    # Clearing the backups removes the obstacle: reserve and confirm again.
    assert put_backups(client, rid, []).status_code == 200
    retry = client.post(f"/rehearsals/{rid}/reservations").json()
    confirmed = client.post(f"/rehearsals/{rid}/reservations/{retry['reservation_id']}/confirm")
    assert confirmed.status_code == 200


def test_confirm_blocked_when_backup_shares_new_breakpoint(client):
    rid = create(client, shared_backup_scenario(l4_capacity=12))
    # T1's backup is its own target path: fine now, useless after the switch.
    assert put_backups(client, rid, [{"tunnel_id": "T1", "path": ["L1", "L2"]}]).status_code == 200
    reserved = client.post(f"/rehearsals/{rid}/reservations").json()
    response = client.post(f"/rehearsals/{rid}/reservations/{reserved['reservation_id']}/confirm")
    assert response.status_code == 409
    by_link = {f["link_id"]: f for f in response.json()["failures"]}
    assert by_link["L1"]["tunnels_without_viable_backup"] == ["T1"]
    cancelled = client.post(f"/rehearsals/{rid}/reservations/{reserved['reservation_id']}/cancel")
    assert cancelled.status_code == 200


def test_confirm_passes_when_failover_stays_safe(client):
    rid = create(client, shared_backup_scenario(l4_capacity=12))
    assert put_backups(
        client, rid, [{"tunnel_id": "T1", "path": ["L4"]}, {"tunnel_id": "T2", "path": ["L4"]}]
    ).status_code == 200
    while True:
        view = client.get(f"/rehearsals/{rid}").json()
        if view["status"] == "completed":
            break
        reserved = client.post(f"/rehearsals/{rid}/reservations").json()
        confirmed = client.post(
            f"/rehearsals/{rid}/reservations/{reserved['reservation_id']}/confirm"
        )
        assert confirmed.status_code == 200, confirmed.text
    assert backup_paths(client, rid) == {"T1": ["L4"], "T2": ["L4"]}


def test_pending_reservation_counts_as_withdrawn_in_failure_scenario(client):
    """Reserved (unconfirmed) bandwidth must not load the failure replay."""
    sc = {
        "links": links(("L1", "A", "B", 10), ("L2", "B", "C", 10), ("L3", "A", "C", 5)),
        "tunnels": [
            tunnel("T1", 5, ["L1", "L2"], ["L3"]),
            tunnel("T2", 3, ["L3"], ["L1", "L2"]),
        ],
    }
    rid = create(client, sc)
    # T2 is next; its reservation holds 3 Mbps on L1/L2.
    reserved = client.post(f"/rehearsals/{rid}/reservations")
    assert reserved.status_code == 201
    # L3 breaks -> T2 fails over to L1/L2: 5 (T1 live) + 3 = 8 <= 10.  If the
    # pending reservation were counted too, 11 > 10 would reject this.
    response = put_backups(client, rid, [{"tunnel_id": "T2", "path": ["L1", "L2"]}])
    assert response.status_code == 200, response.text
    client.post(f"/rehearsals/{rid}/reservations/{reserved.json()['reservation_id']}/cancel")


# ------------------------------------------------------------- compatibility
def test_documents_without_backups_default_to_empty(client):
    """Rehearsals stored before backups existed still load and show empty."""
    rid = create(client, scenario())
    store = client.app.state.engine._store
    document, version = store.get(rid)
    del document["backups"]  # simulate a pre-backup document
    store.replace(rid, version, document)

    record = RehearsalRecord.from_document(document)
    assert record.backups == {}
    view = client.get(f"/rehearsals/{rid}").json()
    assert all(t["backup_path"] is None for t in view["tunnels"])


def test_bundled_failover_example_behaves_as_documented(client):
    root = Path(__file__).resolve().parent.parent / "examples"
    sc = json.loads((root / "failover.json").read_text())
    rid = create(client, sc)
    backups = json.loads((root / "backups.json").read_text())
    response = client.put(f"/rehearsals/{rid}/backups", json=backups)
    assert response.status_code == 200, response.text
    while client.get(f"/rehearsals/{rid}").json()["status"] != "completed":
        reserved = client.post(f"/rehearsals/{rid}/reservations").json()
        confirmed = client.post(
            f"/rehearsals/{rid}/reservations/{reserved['reservation_id']}/confirm"
        )
        assert confirmed.status_code == 200, confirmed.text


# ------------------------------------------- instances sharing one SQLite file
def test_shared_sqlite_instances_check_against_committed_state(tmp_path):
    path = str(tmp_path / "shared.db")
    with TestClient(create_app(SqliteStore(path))) as c1, TestClient(
        create_app(SqliteStore(path))
    ) as c2:
        rid = create(c1, shared_backup_scenario())
        # Registered via one instance...
        assert put_backups(
            c2, rid, [{"tunnel_id": "T1", "path": ["L4"]}, {"tunnel_id": "T2", "path": ["L4"]}]
        ).status_code == 200
        # ...visible through the other.
        assert backup_paths(c1, rid) == {"T1": ["L4"], "T2": ["L4"]}

        reserved = c1.post(f"/rehearsals/{rid}/reservations").json()
        # The second instance re-checks the committed backups and refuses.
        blocked = c2.post(f"/rehearsals/{rid}/reservations/{reserved['reservation_id']}/confirm")
        assert blocked.status_code == 409
        assert blocked.json()["error"] == "failover_unsafe"
        cancelled = c2.post(f"/rehearsals/{rid}/reservations/{reserved['reservation_id']}/cancel")
        assert cancelled.status_code == 200


def test_concurrent_backup_and_confirm_are_checked_in_commit_order(tmp_path):
    """Two engines on one SQLite file: backup submission and confirmation
    race; exactly one may win, and the loser is re-checked against the
    committed state instead of slipping through."""
    path = str(tmp_path / "race.db")
    engines = []
    for _ in range(2):
        engine = Engine(SqliteStore(path))
        engine.initialize()
        engines.append(engine)

    sc = shared_backup_scenario()
    record = engines[0].create_rehearsal(
        [SimpleNamespace(**link) for link in sc["links"]],
        [SimpleNamespace(**t) for t in sc["tunnels"]],
    )
    reservation = engines[0].reserve(record.rehearsal_id)
    entries = [SimpleNamespace(tunnel_id="T1", path=["L4"]), SimpleNamespace(tunnel_id="T2", path=["L4"])]

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def submit_backups():
        barrier.wait()
        try:
            engines[0].set_backups(record.rehearsal_id, entries)
            outcomes.append("backups_saved")
        except FailoverUnsafe:
            outcomes.append("backups_rejected")

    def confirm_switch():
        barrier.wait()
        try:
            engines[1].confirm(record.rehearsal_id, reservation.reservation_id)
            outcomes.append("confirmed")
        except FailoverUnsafe:
            outcomes.append("confirm_rejected")

    threads = [threading.Thread(target=submit_backups), threading.Thread(target=confirm_switch)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for engine in engines:
        engine.close()

    # Whichever committed first forces the other to fail its re-check: the
    # backups and the switch are unsafe together, so they cannot both land.
    assert sorted(outcomes) in (
        ["backups_saved", "confirm_rejected"],
        ["backups_rejected", "confirmed"],
    )
    final = engines[0].view(record.rehearsal_id)[0]
    if "backups_saved" in outcomes:
        assert final.backups and not final.tunnel("T1").moved
    else:
        assert not final.backups and final.tunnel("T1").moved
