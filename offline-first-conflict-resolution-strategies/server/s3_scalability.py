import requests
import json
import time
import uuid
import numpy as np
from collections import defaultdict

SERVER = "http://localhost:8000"
N_VALUES = [10, 50, 100, 500, 1000]
REPEATS = 30

# ===== Implementacje merge per strategia =====

def merge_lww(local_notes: dict, remote_ops: list) -> tuple:
    """LWW: wygrywa nowszy timestamp."""
    conflicts = 0
    lost = 0
    result = dict(local_notes)
    for op in remote_ops:
        note = json.loads(op["payload"])
        nid = note["id"]
        if nid in result:
            conflicts += 1
            if note["updated_at"] > result[nid]["updated_at"]:
                result[nid] = note
            else:
                lost += 1
        else:
            result[nid] = note
    return result, conflicts, lost

def merge_vector_clock_lww(local_notes: dict, remote_ops: list) -> tuple:
    """LWW + Vector Clocks: porównaj zegary, fallback do timestamp."""
    def happens_before(a: dict, b: dict) -> bool:
        keys = set(a.keys()) | set(b.keys())
        strictly_less = False
        for k in keys:
            va, vb = a.get(k, 0), b.get(k, 0)
            if va > vb: return False
            if va < vb: strictly_less = True
        return strictly_less

    conflicts = 0
    lost = 0
    result = dict(local_notes)
    for op in remote_ops:
        note = json.loads(op["payload"])
        nid = note["id"]
        if nid in result:
            conflicts += 1
            lc = json.loads(result[nid].get("vector_clock") or "{}")
            rc = json.loads(op.get("vector_clock") or "{}")
            if happens_before(lc, rc):
                result[nid] = note
            elif happens_before(rc, lc):
                lost += 1
            else:
                # współbieżne — fallback do timestamp
                if note["updated_at"] > result[nid]["updated_at"]:
                    result[nid] = note
                else:
                    lost += 1
        else:
            result[nid] = note
    return result, conflicts, lost

def merge_orset(local_notes: dict, remote_ops: list) -> tuple:
    """OR-Set: add-wins, tombstones."""
    entries = defaultdict(set)
    tombstones = set()

    # Stan lokalny
    for nid, note in local_notes.items():
        tag = f"{nid}-local"
        if note.get("is_deleted"):
            tombstones.add(tag)
        else:
            entries[nid].add(tag)

    conflicts = 0
    lost = 0
    for op in remote_ops:
        note = json.loads(op["payload"])
        nid = note["id"]
        tag = f"{nid}-{op['id']}"
        if nid in local_notes:
            conflicts += 1
        if note.get("is_deleted"):
            existing = entries.pop(nid, set())
            tombstones.update(existing)
            tombstones.add(tag)
            lost += 1
        else:
            if tag not in tombstones:
                entries[nid].add(tag)

    # Wynik: elementy z aktywnymi tagami
    result = {}
    for nid in entries:
        active = entries[nid] - tombstones
        if active:
            result[nid] = local_notes.get(nid, {})
    return result, conflicts, lost

STRATEGIES = {
    "LWW": merge_lww,
    "VectorClockLWW": merge_vector_clock_lww,
    "ORSet": merge_orset,
}

# ===== Helpers =====

def reset_server():
    try:
        requests.delete(f"{SERVER}/sync/reset", timeout=5)
    except Exception as e:
        print(f"Reset error: {e}")
    time.sleep(0.1)

def generate_operations(n: int, device_id: str) -> list:
    note_ids = [str(uuid.uuid4()) for _ in range(max(1, n // 10))]
    now = int(time.time() * 1000)
    ops = []
    for i in range(n):
        note_id = note_ids[i % len(note_ids)]
        note = {
            "id": note_id,
            "title": f"Notatka {note_id[:8]} iter {i}",
            "content": f"Treść operacji {i} z urządzenia {device_id}",
            "created_at": now,
            "updated_at": now + i,
            "is_deleted": False,
            "vector_clock": json.dumps({device_id: i + 1})
        }
        ops.append({
            "id": str(uuid.uuid4()),
            "note_id": note_id,
            "operation_type": "CREATE" if i < len(note_ids) else "UPDATE",
            "payload": json.dumps(note),
            "vector_clock": json.dumps({device_id: i + 1}),
            "timestamp": now + i,
            "device_id": device_id
        })
    return ops

def pull_operations(device_id: str) -> list:
    resp = requests.get(
        f"{SERVER}/sync/pull/{device_id}?since=0",
        timeout=10
    )
    return resp.json().get("operations", [])

# ===== Główna logika =====

def run_s3():
    print("=== Scenariusz S3: Skalowalność offline — izolowany czas merge ===")
    print(f"N: {N_VALUES}, Strategie: {list(STRATEGIES.keys())}, Powtórzeń: {REPEATS}\n")

    all_results = []

    for n in N_VALUES:
        print(f"\n--- N = {n} operacji ---")

        # Przygotuj dane raz dla tego N
        reset_server()
        ops = generate_operations(n, f"source-device-n{n}")
        requests.post(f"{SERVER}/sync/push", json=ops, timeout=30)

        # Pobierz operacje raz (HTTP overhead poza pomiarem)
        remote_ops = pull_operations(f"measuring-device-n{n}")
        print(f"  Pobrano {len(remote_ops)} operacji ({sum(len(o['payload']) for o in remote_ops)} bajtów)")

        for strategy_name, merge_fn in STRATEGIES.items():
            durations = []
            conflicts_list = []
            lost_list = []

            for rep in range(REPEATS):
                # Symuluj lokalny stan — połowa notatek już istnieje
                local_notes = {}
                for i, op in enumerate(remote_ops[:len(remote_ops)//2]):
                    note = json.loads(op["payload"])
                    note["updated_at"] -= 1  # lokalny jest starszy
                    local_notes[note["id"]] = note

                # Mierz TYLKO czas merge
                start = time.perf_counter()
                _, conflicts, lost = merge_fn(local_notes, remote_ops)
                elapsed_us = int((time.perf_counter() - start) * 1_000_000)

                durations.append(elapsed_us)
                conflicts_list.append(conflicts)
                lost_list.append(lost)

            avg = np.mean(durations)
            std = np.std(durations)
            print(f"  {strategy_name:<18} avg={avg:6.0f}µs  std={std:5.0f}µs  "
                  f"konflikty={np.mean(conflicts_list):.0f}  utracone={np.mean(lost_list):.0f}")

            for rep, (dur, conf, lost) in enumerate(zip(durations, conflicts_list, lost_list)):
                all_results.append({
                    "strategy": strategy_name,
                    "scenario": "S3",
                    "n_operations": n,
                    "repeat": rep,
                    "merge_duration_us": dur,
                    "payload_size_bytes": sum(len(o["payload"]) for o in remote_ops),
                    "conflicts_detected": conf,
                    "operations_lost": lost,
                    "operations_received": len(remote_ops),
                    "timestamp": int(time.time() * 1000)
                })

    # Zapis wyników
    import os
    os.makedirs("../results/S3", exist_ok=True)
    path = "../results/S3/s3_results_v2.jsonl"
    with open(path, "w") as f:
        for r in all_results:
            f.write(json.dumps(r) + "\n")

    print(f"\n✓ Zapisano {len(all_results)} wyników do {path}")
    return all_results

if __name__ == "__main__":
    run_s3()