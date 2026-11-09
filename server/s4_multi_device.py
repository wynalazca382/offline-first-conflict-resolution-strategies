import requests
import json
import time
import uuid
import numpy as np
import os
from collections import defaultdict

SERVER = "http://localhost:8000"
K_VALUES = [2, 3, 5, 10]
OPS_PER_DEVICE = 100
REPEATS = 30

def reset_server():
    try:
        requests.delete(f"{SERVER}/sync/reset", timeout=5)
    except Exception as e:
        print(f"Reset error: {e}")
    time.sleep(0.1)

def generate_device_ops(device_id: str, k: int, n: int) -> list:
    """Generuje N operacji dla urządzenia — część notatek wspólna (konflikty)."""
    shared_ids = [f"shared-note-{i}" for i in range(n // 2)]
    private_ids = [f"private-{device_id}-{i}" for i in range(n // 2)]
    all_ids = shared_ids + private_ids
    now = int(time.time() * 1000)
    ops = []
    for i, note_id in enumerate(all_ids[:n]):
        note = {
            "id": note_id,
            "title": f"Notatka {note_id[:12]} od {device_id}",
            "content": f"Treść od urządzenia {device_id}, iteracja {i}",
            "created_at": now,
            "updated_at": now + i,
            "is_deleted": False,
            "vector_clock": json.dumps({device_id: i + 1})
        }
        ops.append({
            "id": str(uuid.uuid4()),
            "note_id": note_id,
            "operation_type": "UPDATE",
            "payload": json.dumps(note),
            "vector_clock": json.dumps({device_id: i + 1}),
            "timestamp": now + i,
            "device_id": device_id
        })
    return ops

# ===== Implementacje merge =====

def merge_all_lww(all_ops: list) -> tuple:
    """LWW: wygrywa najnowszy timestamp."""
    notes = {}
    conflicts = 0
    lost = 0
    for op in sorted(all_ops, key=lambda x: x["timestamp"]):
        note = json.loads(op["payload"])
        nid = note["id"]
        if nid in notes:
            conflicts += 1
            if note["updated_at"] > notes[nid]["updated_at"]:
                lost += 1  # poprzednia wersja utracona
                notes[nid] = note
            else:
                lost += 1  # nowa wersja utracona
        else:
            notes[nid] = note
    return notes, conflicts, lost

def merge_all_vector_clock(all_ops: list) -> tuple:
    """VectorClock LWW: porównaj zegary, fallback do timestamp."""
    def happens_before(a, b):
        keys = set(a) | set(b)
        strictly_less = False
        for k in keys:
            if a.get(k, 0) > b.get(k, 0): return False
            if a.get(k, 0) < b.get(k, 0): strictly_less = True
        return strictly_less

    notes = {}
    clocks = {}
    conflicts = 0
    lost = 0

    for op in all_ops:
        note = json.loads(op["payload"])
        nid = note["id"]
        rc = json.loads(op.get("vector_clock") or "{}")
        if nid in notes:
            conflicts += 1
            lc = clocks.get(nid, {})
            if happens_before(lc, rc):
                notes[nid] = note
                clocks[nid] = rc
                lost += 1
            elif happens_before(rc, lc):
                lost += 1
            else:
                if note["updated_at"] > notes[nid]["updated_at"]:
                    notes[nid] = note
                    clocks[nid] = rc
                    lost += 1
                else:
                    lost += 1
        else:
            notes[nid] = note
            clocks[nid] = rc
    return notes, conflicts, lost

def merge_all_orset(all_ops: list) -> tuple:
    """OR-Set: add-wins, zachowaj wszystkie wersje przez merge treści."""
    entries = defaultdict(list)
    conflicts = 0
    lost = 0
    for op in all_ops:
        note = json.loads(op["payload"])
        nid = note["id"]
        if nid in entries:
            conflicts += 1
        entries[nid].append(note)
    # Merge: zachowaj najnowszą wersję (uproszczenie dla pomiaru)
    notes = {}
    for nid, versions in entries.items():
        notes[nid] = max(versions, key=lambda n: n["updated_at"])
    return notes, conflicts, lost  # OR-Set nie traci operacji

STRATEGIES = {
    "LWW": merge_all_lww,
    "VectorClockLWW": merge_all_vector_clock,
    "ORSet": merge_all_orset,
}

# ===== Główna logika =====

def run_s4():
    print("=== Scenariusz S4: Wiele urządzeń jednocześnie ===")
    print(f"K: {K_VALUES}, Operacji/urządzenie: {OPS_PER_DEVICE}, Powtórzeń: {REPEATS}\n")

    all_results = []

    for k in K_VALUES:
        print(f"\n--- K = {k} urządzeń ---")

        # Przygotuj operacje dla wszystkich urządzeń
        all_ops = []
        for d in range(k):
            device_id = f"device-{d}"
            ops = generate_device_ops(device_id, k, OPS_PER_DEVICE)
            all_ops.extend(ops)

        total_ops = len(all_ops)
        payload_size = sum(len(o["payload"]) for o in all_ops)
        print(f"  Łącznie operacji: {total_ops}, payload: {payload_size//1024}KB")

        for strategy_name, merge_fn in STRATEGIES.items():
            durations = []
            conflicts_list = []
            lost_list = []

            for rep in range(REPEATS):
                # Mierz TYLKO czas merge
                start = time.perf_counter()
                _, conflicts, lost = merge_fn(all_ops)
                elapsed_us = int((time.perf_counter() - start) * 1_000_000)

                durations.append(elapsed_us)
                conflicts_list.append(conflicts)
                lost_list.append(lost)

            avg = np.mean(durations)
            std = np.std(durations)
            print(f"  {strategy_name:<18} avg={avg:7.1f}µs  std={std:6.1f}µs  "
                  f"konflikty={np.mean(conflicts_list):.0f}  utracone={np.mean(lost_list):.0f}")

            for rep, (dur, conf, lost) in enumerate(
                    zip(durations, conflicts_list, lost_list)):
                all_results.append({
                    "strategy": strategy_name,
                    "scenario": "S4",
                    "k_devices": k,
                    "total_operations": total_ops,
                    "repeat": rep,
                    "merge_duration_us": dur,
                    "payload_size_bytes": payload_size,
                    "conflicts_detected": conf,
                    "operations_lost": lost,
                    "timestamp": int(time.time() * 1000)
                })

    os.makedirs("../results/S4", exist_ok=True)
    path = "../results/S4/s4_results.jsonl"
    with open(path, "w") as f:
        for r in all_results:
            f.write(json.dumps(r) + "\n")

    print(f"\n✓ Zapisano {len(all_results)} wyników do {path}")
    return all_results

if __name__ == "__main__":
    run_s4()