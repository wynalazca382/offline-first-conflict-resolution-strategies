import requests
import json
import time
import uuid
import numpy as np
import os
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SERVER = "http://localhost:8000"
REPEATS = 30
OPS_COUNT = 100

# ===== Profile sieciowe (symulacja Clumsy w Pythonie) =====

PROFILES = {
    "P1_high_latency": {
        "delay_ms": 500,
        "delay_std_ms": 100,
        "packet_loss_pct": 0,
        "description": "Wysokie opóźnienie (500ms ±100ms, 0% utraty)"
    },
    "P2_packet_loss": {
        "delay_ms": 50,
        "delay_std_ms": 10,
        "packet_loss_pct": 20,
        "description": "Utrata pakietów (50ms, 20% utraty)"
    },
    "P3_extreme": {
        "delay_ms": 1000,
        "delay_std_ms": 200,
        "packet_loss_pct": 40,
        "description": "Ekstremalne warunki (1000ms ±200ms, 40% utraty)"
    }
}

# ===== Symulacja niestabilnej sieci =====

class UnstableNetworkSession:
    """Symuluje niestabilne połączenie sieciowe."""

    def __init__(self, profile: dict):
        self.delay_ms = profile["delay_ms"]
        self.delay_std_ms = profile["delay_std_ms"]
        self.loss_pct = profile["packet_loss_pct"]
        self.session = requests.Session()
        retry = Retry(total=5, backoff_factor=0.5,
                      status_forcelist=[500, 502, 503, 504])
        self.session.mount('http://', HTTPAdapter(max_retries=retry))

    def _apply_delay(self):
        delay = max(0, np.random.normal(self.delay_ms, self.delay_std_ms))
        time.sleep(delay / 1000)

    def _should_drop(self) -> bool:
        return random.random() < (self.loss_pct / 100)

    def post(self, url, **kwargs) -> requests.Response:
        attempts = 0
        while True:
            self._apply_delay()
            if self._should_drop():
                attempts += 1
                if attempts >= 5:
                    raise requests.ConnectionError("Zbyt wiele utraconych pakietów")
                time.sleep(0.1 * attempts)
                continue
            return self.session.post(url, **kwargs)

    def get(self, url, **kwargs) -> requests.Response:
        attempts = 0
        while True:
            self._apply_delay()
            if self._should_drop():
                attempts += 1
                if attempts >= 5:
                    raise requests.ConnectionError("Zbyt wiele utraconych pakietów")
                time.sleep(0.1 * attempts)
                continue
            return self.session.get(url, **kwargs)

# ===== Reset serwera =====

def reset_server():
    try:
        requests.delete(f"{SERVER}/sync/reset", timeout=5)
    except Exception as e:
        print(f"Reset error: {e}")
    time.sleep(0.1)

# ===== Generowanie operacji =====

def generate_ops(device_id: str, n: int) -> list:
    note_ids = [str(uuid.uuid4()) for _ in range(max(1, n // 10))]
    now = int(time.time() * 1000)
    ops = []
    for i in range(n):
        note_id = note_ids[i % len(note_ids)]
        note = {
            "id": note_id,
            "title": f"Notatka {note_id[:8]} iter {i}",
            "content": f"Treść od {device_id}, iter {i}",
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

def merge_lww(remote_ops: list) -> tuple:
    notes = {}
    conflicts = 0
    for op in remote_ops:
        note = json.loads(op["payload"])
        nid = note["id"]
        if nid in notes:
            conflicts += 1
            if note["updated_at"] > notes[nid]["updated_at"]:
                notes[nid] = note
        else:
            notes[nid] = note
    return notes, conflicts

def merge_vc(remote_ops: list) -> tuple:
    def hb(a, b):
        keys = set(a) | set(b)
        sl = False
        for k in keys:
            if a.get(k,0) > b.get(k,0): return False
            if a.get(k,0) < b.get(k,0): sl = True
        return sl

    notes = {}
    clocks = {}
    conflicts = 0
    for op in remote_ops:
        note = json.loads(op["payload"])
        nid = note["id"]
        rc = json.loads(op.get("vector_clock") or "{}")
        if nid in notes:
            conflicts += 1
            lc = clocks.get(nid, {})
            if hb(lc, rc):
                notes[nid] = note
                clocks[nid] = rc
            elif not hb(rc, lc):
                if note["updated_at"] > notes[nid]["updated_at"]:
                    notes[nid] = note
                    clocks[nid] = rc
        else:
            notes[nid] = note
            clocks[nid] = rc
    return notes, conflicts

def merge_orset(remote_ops: list) -> tuple:
    from collections import defaultdict
    entries = defaultdict(list)
    conflicts = 0
    for op in remote_ops:
        note = json.loads(op["payload"])
        nid = note["id"]
        if nid in entries:
            conflicts += 1
        entries[nid].append(note)
    notes = {nid: max(vs, key=lambda n: n["updated_at"])
             for nid, vs in entries.items()}
    return notes, conflicts

MERGE_FNS = {
    "LWW": merge_lww,
    "VectorClockLWW": merge_vc,
    "ORSet": merge_orset,
}

# ===== Scenariusz S5 =====

def run_s5():
    print("=== Scenariusz S5: Sieć niestabilna ===")
    print(f"Operacji: {OPS_COUNT}, Powtórzeń: {REPEATS}\n")

    all_results = []

    for profile_name, profile in PROFILES.items():
        print(f"\n{'='*55}")
        print(f"Profil: {profile_name}")
        print(f"  {profile['description']}")
        print(f"{'='*55}")

        for strategy_name, merge_fn in MERGE_FNS.items():
            print(f"\n  Strategia: {strategy_name}")
            durations_total = []
            durations_merge = []
            sync_attempts = []
            sync_success = []
            conflicts_list = []

            for rep in range(REPEATS):
                reset_server()

                # Przygotuj operacje (bez opóźnień sieciowych)
                ops_a = generate_ops(f"device-a-{rep}", OPS_COUNT)
                ops_b = generate_ops(f"device-b-{rep}", OPS_COUNT)

                # Push A na serwer (normalnie)
                requests.post(f"{SERVER}/sync/push", json=ops_a, timeout=10)

                # Symuluj niestabilną sieć dla urządzenia B
                net = UnstableNetworkSession(profile)
                device_b_id = f"device-b-{rep}"

                attempts = 0
                success = False
                total_start = time.perf_counter()

                try:
                    # Push B przez niestabilną sieć
                    attempts += 1
                    net.post(f"{SERVER}/sync/push",
                             json=ops_b, timeout=15)

                    # Pull A przez niestabilną sieć
                    attempts += 1
                    resp = net.get(
                        f"{SERVER}/sync/pull/{device_b_id}?since=0",
                        timeout=15
                    )
                    remote_ops = resp.json().get("operations", [])

                    # Mierz czas merge (bez opóźnień sieciowych)
                    merge_start = time.perf_counter()
                    _, conflicts = merge_fn(remote_ops)
                    merge_us = int((time.perf_counter() - merge_start) * 1_000_000)

                    success = True
                    total_us = int((time.perf_counter() - total_start) * 1_000_000)

                except Exception as e:
                    total_us = int((time.perf_counter() - total_start) * 1_000_000)
                    merge_us = 0
                    conflicts = 0
                    remote_ops = []

                durations_total.append(total_us)
                durations_merge.append(merge_us)
                sync_attempts.append(attempts)
                sync_success.append(1 if success else 0)
                conflicts_list.append(conflicts)

                all_results.append({
                    "strategy": strategy_name,
                    "scenario": "S5",
                    "profile": profile_name,
                    "repeat": rep,
                    "sync_success": 1 if success else 0,
                    "sync_attempts": attempts,
                    "total_duration_us": total_us,
                    "merge_duration_us": merge_us,
                    "payload_size_bytes": sum(len(o["payload"]) for o in ops_a),
                    "conflicts_detected": conflicts,
                    "operations_received": len(remote_ops) if success else 0,
                    "timestamp": int(time.time() * 1000)
                })

            sr = sum(sync_success) / REPEATS * 100
            avg_total = np.mean(durations_total) / 1000
            avg_merge = np.mean([d for d in durations_merge if d > 0]) if any(d > 0 for d in durations_merge) else 0
            print(f"    sukces={sr:.0f}%  "
                  f"avg_total={avg_total:.0f}ms  "
                  f"avg_merge={avg_merge:.0f}µs  "
                  f"konflikty={np.mean(conflicts_list):.0f}")

    os.makedirs("../results/S5", exist_ok=True)
    path = "../results/S5/s5_results.jsonl"
    with open(path, "w") as f:
        for r in all_results:
            f.write(json.dumps(r) + "\n")

    print(f"\n✓ Zapisano {len(all_results)} wyników do {path}")
    return all_results

if __name__ == "__main__":
    run_s5()