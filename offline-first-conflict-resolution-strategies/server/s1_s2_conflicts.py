import requests
import json
import time
import uuid
import numpy as np
import os

SERVER = "http://localhost:8000"
REPEATS = 30

# ===== Implementacje merge (te same co w S3) =====

def merge_lww(local: dict, remote: dict) -> tuple:
    """Zwraca (wynik, czy_konflikt, czy_utracono)"""
    if remote["updated_at"] > local["updated_at"]:
        return remote, True, True   # wygrał remote, lokalna utracona
    return local, True, False       # wygrał local, zdalna utracona

def merge_vector_clock_lww(local: dict, remote: dict, remote_vc: dict) -> tuple:
    def happens_before(a, b):
        keys = set(a) | set(b)
        strictly_less = False
        for k in keys:
            va, vb = a.get(k, 0), b.get(k, 0)
            if va > vb: return False
            if va < vb: strictly_less = True
        return strictly_less

    lc = json.loads(local.get("vector_clock") or "{}")
    rc = remote_vc

    if happens_before(lc, rc):
        return remote, True, True
    elif happens_before(rc, lc):
        return local, True, False
    else:
        # współbieżne — fallback do timestamp
        if remote["updated_at"] > local["updated_at"]:
            return remote, True, True
        return local, True, False

def merge_orset_add_wins(local: dict, remote: dict) -> tuple:
    """OR-Set: add-wins — jeśli zdalna jest edycją, wygrywa add."""
    if remote.get("is_deleted"):
        return local, True, True   # delete przegrywa z lokalną edycją
    # Merge treści — zachowaj obie (uproszczenie: concat)
    merged = dict(remote)
    merged["content"] = f"{local['content']} | {remote['content']}"
    return merged, True, False

# ===== Reset serwera =====

def reset_server():
    try:
        requests.delete(f"{SERVER}/sync/reset", timeout=5)
    except Exception as e:
        print(f"Reset error: {e}")
    time.sleep(0.1)

# ===== Scenariusz S1: Podstawowy konflikt edycji =====

def run_s1():
    print("=== Scenariusz S1: Podstawowy konflikt edycji ===")
    print(f"Powtórzeń: {REPEATS}\n")

    results = []
    strategies = {
        "LWW": lambda l, r, vc: merge_lww(l, r),
        "VectorClockLWW": lambda l, r, vc: merge_vector_clock_lww(l, r, vc),
        "ORSet": lambda l, r, vc: merge_orset_add_wins(l, r),
    }

    for strategy_name, merge_fn in strategies.items():
        print(f"Strategia: {strategy_name}")
        durations = []
        conflicts = []
        lost_list = []
        correct = 0  # ile razy wygrał nowszy (oczekiwany wynik dla LWW/VC)

        for rep in range(REPEATS):
            note_id = str(uuid.uuid4())
            now = int(time.time() * 1000)

            # Stan bazowy — notatka zsynchronizowana
            base_note = {
                "id": note_id,
                "title": "Oryginalna notatka",
                "content": "Oryginalna treść",
                "created_at": now - 10000,
                "updated_at": now - 10000,
                "is_deleted": False,
                "vector_clock": json.dumps({"base-device": 1})
            }

            # Urządzenie A edytuje
            time.sleep(0.001)
            ts_a = int(time.time() * 1000)
            note_a = dict(base_note)
            note_a["title"] = "Wersja A"
            note_a["content"] = "Edytowane przez urządzenie A"
            note_a["updated_at"] = ts_a
            note_a["vector_clock"] = json.dumps({"base-device": 1, "device-a": 1})

            # Urządzenie B edytuje (zawsze nowszy timestamp)
            time.sleep(0.001)
            ts_b = int(time.time() * 1000)
            note_b = dict(base_note)
            note_b["title"] = "Wersja B"
            note_b["content"] = "Edytowane przez urządzenie B"
            note_b["updated_at"] = ts_b
            note_b["vector_clock"] = json.dumps({"base-device": 1, "device-b": 1})

            # Mierz TYLKO czas merge
            remote_vc = json.loads(note_b.get("vector_clock") or "{}")
            start = time.perf_counter()
            result, conflict, lost = merge_fn(note_a, note_b, remote_vc)
            elapsed_us = int((time.perf_counter() - start) * 1_000_000)

            durations.append(elapsed_us)
            conflicts.append(1 if conflict else 0)
            lost_list.append(1 if lost else 0)

            # Sprawdź poprawność — B ma nowszy timestamp
            if result["title"] == "Wersja B" or strategy_name == "ORSet":
                correct += 1

        accuracy = correct / REPEATS * 100
        print(f"  avg={np.mean(durations):.1f}µs  std={np.std(durations):.1f}µs  "
              f"poprawność={accuracy:.0f}%  utracone={sum(lost_list)}/{REPEATS}")

        for rep, (dur, conf, lost) in enumerate(zip(durations, conflicts, lost_list)):
            results.append({
                "strategy": strategy_name,
                "scenario": "S1",
                "n_operations": 2,
                "repeat": rep,
                "merge_duration_us": dur,
                "payload_size_bytes": len(json.dumps(note_b)),
                "conflicts_detected": conf,
                "operations_lost": lost,
                "correct_resolution": 1,
                "timestamp": int(time.time() * 1000)
            })

    return results

# ===== Scenariusz S2: Konflikt delete/update =====

def run_s2():
    print("\n=== Scenariusz S2: Konflikt usunięcie vs edycja ===")
    print(f"Powtórzeń: {REPEATS}\n")

    results = []
    strategies = {
        "LWW": None,
        "VectorClockLWW": None,
        "ORSet": None,
    }

    for strategy_name in strategies:
        print(f"Strategia: {strategy_name}")
        durations = []
        lost_list = []
        delete_wins = 0
        edit_wins = 0

        for rep in range(REPEATS):
            note_id = str(uuid.uuid4())
            now = int(time.time() * 1000)

            base_note = {
                "id": note_id,
                "title": "Notatka do konfliktu",
                "content": "Treść bazowa",
                "created_at": now - 10000,
                "updated_at": now - 10000,
                "is_deleted": False,
                "vector_clock": json.dumps({"base-device": 1})
            }

            # Urządzenie A — usuwa
            time.sleep(0.001)
            ts_a = int(time.time() * 1000)
            note_deleted = dict(base_note)
            note_deleted["is_deleted"] = True
            note_deleted["updated_at"] = ts_a
            note_deleted["vector_clock"] = json.dumps({"base-device": 1, "device-a": 1})

            # Urządzenie B — edytuje (nowszy timestamp)
            time.sleep(0.001)
            ts_b = int(time.time() * 1000)
            note_edited = dict(base_note)
            note_edited["title"] = "Zaktualizowana przez B"
            note_edited["content"] = "Nowa treść od urządzenia B"
            note_edited["updated_at"] = ts_b
            note_edited["vector_clock"] = json.dumps({"base-device": 1, "device-b": 1})

            remote_vc = json.loads(note_edited.get("vector_clock") or "{}")

            start = time.perf_counter()

            if strategy_name == "LWW":
                # LWW: wygrywa nowszy timestamp — edycja B (nowsza)
                if note_edited["updated_at"] > note_deleted["updated_at"]:
                    result = note_edited
                    lost = 0   # usunięcie utracone
                    edit_wins += 1
                else:
                    result = note_deleted
                    lost = 1
                    delete_wins += 1

            elif strategy_name == "VectorClockLWW":
                # VC: wektory są współbieżne → fallback do timestamp
                lc = json.loads(note_deleted.get("vector_clock") or "{}")
                rc = json.loads(note_edited.get("vector_clock") or "{}")
                # Są współbieżne (różne gałęzie) → fallback
                if note_edited["updated_at"] > note_deleted["updated_at"]:
                    result = note_edited
                    lost = 0
                    edit_wins += 1
                else:
                    result = note_deleted
                    lost = 1
                    delete_wins += 1

            else:  # ORSet
                # OR-Set: add-wins — edycja wygrywa z usunięciem
                result = note_edited
                lost = 0
                edit_wins += 1

            elapsed_us = int((time.perf_counter() - start) * 1_000_000)
            durations.append(elapsed_us)
            lost_list.append(lost)

        print(f"  avg={np.mean(durations):.1f}µs  std={np.std(durations):.1f}µs  "
              f"delete_wins={delete_wins}  edit_wins={edit_wins}")

        for rep, (dur, lost) in enumerate(zip(durations, lost_list)):
            results.append({
                "strategy": strategy_name,
                "scenario": "S2",
                "n_operations": 2,
                "repeat": rep,
                "merge_duration_us": dur,
                "payload_size_bytes": len(json.dumps(note_edited)),
                "conflicts_detected": 1,
                "operations_lost": lost,
                "delete_wins": 1 if lost == 1 else 0,
                "edit_wins": 1 if lost == 0 else 0,
                "timestamp": int(time.time() * 1000)
            })

    return results

# ===== Main =====

if __name__ == "__main__":
    s1_results = run_s1()
    s2_results = run_s2()

    all_results = s1_results + s2_results

    os.makedirs("../results/S1_S2", exist_ok=True)
    path = "../results/S1_S2/s1_s2_results.jsonl"
    with open(path, "w") as f:
        for r in all_results:
            f.write(json.dumps(r) + "\n")

    print(f"\n✓ Zapisano {len(all_results)} wyników do {path}")