from pathlib import Path

from league.fantrax_data import Snapshot

SNAPSHOT = Snapshot(Path(__file__).resolve().parents[2] / "data" / "fantrax" / "2026-final")


def test_end_state_counts_plate_appearances_from_at_bats_and_walks():
    end = SNAPSHOT.end_states()["05yby"]  # Kevin McGonigle: 597 AB, 95 BB
    assert (end.status, end.games_played, end.plate_appearances, end.outs) == ("Minors", 154, 692, 0)


def test_end_state_converts_innings_to_outs():
    end = SNAPSHOT.end_states()["05ydp"]  # Kade Anderson: 32.1 IP
    assert (end.outs, end.plate_appearances) == (97, 0)
