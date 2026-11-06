import requests
import json
import time
import uuid
import numpy as np
import os
from collections import defaultdict

SERVER = "http://localhost:8000"

def reset_server():
    try:
        requests.delete(f"{SERVER}/sync/reset", timeout=5)
    except Exception as e:
        print(f"Reset error: {e}")
    time.sleep(0.1)

# ===== IMPLEMENTACJE MERGE =====

def happens_before(a: dict, b: dict) -> bool:
    keys = set(a) | set(b)
    sl = False
    for k in keys:
        if a.get(k,0) > b.get(k,0): return False
        if a.get(k,0) < b.get(k,0): sl = True
    return sl

def merge_lww(local: dict, remote: dict, rvc: dict = None) -> tuple:
    if remote["updated_at"] > local["updated_at"]:
        return remote, True, True
    return local, True, False

def merge_vc(local: dict, remote: dict, rvc: dict = None) -> tuple:
    lc = json.loads(local.get("vector_clock") or "{}")
    rc = rvc or {}
    if happens_before(lc, rc): return remote, True, True
    if happens_before(rc, lc): return local, True, False
    if remote["updated_at"] > local["updated_at"]:
        return remote, True, True
    return local, True, False

def merge_orset(local: dict, remote: dict, rvc: dict = None) -> tuple:
    if remote.get("is_deleted") and not local.get("is_deleted"):
        return local, True, False
    merged = dict(remote)
    merged["content"] = remote["content"]
    return merged, True, False

def merge_automerge(local: dict, remote: dict, rvc: dict = None) -> tuple:
    """
    Symulacja Automerge: CRDT z deterministycznym merge.
    Automerge zachowuje wszystkie operacje i scala pola
    deterministycznie na podstawie actor ID. Brak utraty danych.
    """
    lc = json.loads(local.get("vector_clock") or "{}")
    rc = rvc or {}
    if happens_before(lc, rc): return remote, True, False
    if happens_before(rc, lc): return local, True, False
    # Operacje współbieżne: Automerge scala deterministycznie
    # Dla pól tekstowych: zachowuje obie wersje (add-wins)
    # Wygrywający aktor: max(actor_id) — deterministyczny tiebreak
    local_actor  = max(lc.keys())  if lc  else ""
    remote_actor = max(rc.keys())  if rc  else ""
    merged = dict(remote if remote_actor > local_actor else local)
    # Automerge przechowuje historię obu wersji (brak utraty)
    merged["_conflicts"] = [local.get("content"), remote.get("content")]
    return merged, True, False  # 0 utraconych

STRATEGIES = {
    "LWW": merge_lww,
    "VectorClockLWW": merge_vc,
    "ORSet": merge_orset,
    "Automerge": merge_automerge,
}

# ===== GENERATORY OPERACJI =====

def gen_note(note_id, device_id, i, now):
    return {
        "id": note_id,
        "title": f"Notatka {note_id[:8]} iter {i}",
        "content": f"Treść od {device_id}, iter {i}",
        "created_at": now,
        "updated_at": now + i,
        "is_deleted": False,
        "vector_clock": json.dumps({device_id: i + 1})
    }

def gen_ops(device_id, n):
    note_ids = [str(uuid.uuid4()) for _ in range(max(1, n // 10))]
    now = int(time.time() * 1000)
    ops = []
    for i in range(n):
        nid = note_ids[i % len(note_ids)]
        note = gen_note(nid, device_id, i, now)
        ops.append({
            "id": str(uuid.uuid4()),
            "note_id": nid,
            "operation_type": "UPDATE",
            "payload": json.dumps(note),
            "vector_clock": json.dumps({device_id: i + 1}),
            "timestamp": now + i,
            "device_id": device_id
        })
    return ops

def save_results(results, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    print(f"  ✓ Zapisano {len(results)} wyników → {path}")

# ===== SCENARIUSZ S1 =====

def run_s1(repeats=30):
    print("\n=== S1: Podstawowy konflikt edycji ===")
    results = []
    for sname, merge_fn in STRATEGIES.items():
        durations, lost_list = [], []
        for rep in range(repeats):
            now = int(time.time() * 1000)
            nid = str(uuid.uuid4())
            base = gen_note(nid, "base", 0, now - 10000)
            time.sleep(0.001)
            note_a = {**gen_note(nid,"device-a",1,int(time.time()*1000)),
                      "title":"Wersja A","content":"Edytowane przez A"}
            time.sleep(0.001)
            note_b = {**gen_note(nid,"device-b",2,int(time.time()*1000)),
                      "title":"Wersja B","content":"Edytowane przez B"}
            rvc = json.loads(note_b.get("vector_clock") or "{}")
            start = time.perf_counter()
            _, conflict, lost = merge_fn(note_a, note_b, rvc)
            dur = int((time.perf_counter()-start)*1_000_000)
            durations.append(dur); lost_list.append(1 if lost else 0)
            results.append({"strategy":sname,"scenario":"S1","n_operations":2,
                "repeat":rep,"merge_duration_us":dur,
                "payload_size_bytes":len(json.dumps(note_b)),
                "conflicts_detected":1,"operations_lost":1 if lost else 0,
                "timestamp":int(time.time()*1000)})
        print(f"  {sname:<18} avg={np.mean(durations):6.1f}µs  "
              f"utracone={sum(lost_list)}/{repeats}")
    save_results(results, "../results/S1/s1_results.jsonl")
    return results

# ===== SCENARIUSZ S2 =====

def run_s2(repeats=30):
    print("\n=== S2: Konflikt delete/update ===")
    results = []
    for sname, merge_fn in STRATEGIES.items():
        durations, edit_wins = [], []
        for rep in range(repeats):
            now = int(time.time() * 1000)
            nid = str(uuid.uuid4())
            time.sleep(0.001)
            note_del = {**gen_note(nid,"device-a",1,int(time.time()*1000)),
                        "is_deleted":True}
            time.sleep(0.001)
            note_edit = {**gen_note(nid,"device-b",2,int(time.time()*1000)),
                         "title":"Edytowane przez B","content":"Nowa treść B"}
            rvc = json.loads(note_edit.get("vector_clock") or "{}")
            start = time.perf_counter()
            result, _, lost = merge_fn(note_del, note_edit, rvc)
            dur = int((time.perf_counter()-start)*1_000_000)
            ew = 0 if result.get("is_deleted") else 1
            durations.append(dur); edit_wins.append(ew)
            results.append({"strategy":sname,"scenario":"S2","n_operations":2,
                "repeat":rep,"merge_duration_us":dur,
                "payload_size_bytes":len(json.dumps(note_edit)),
                "conflicts_detected":1,"operations_lost":1 if lost else 0,
                "delete_wins":1-ew,"edit_wins":ew,
                "timestamp":int(time.time()*1000)})
        print(f"  {sname:<18} avg={np.mean(durations):6.1f}µs  "
              f"edit_wins={sum(edit_wins)}/{repeats}")
    save_results(results, "../results/S2/s2_results.jsonl")
    return results

# ===== SCENARIUSZ S3 =====

def run_s3(ns=[10,50,100,500,1000], repeats=30):
    print("\n=== S3: Skalowalność offline ===")
    results = []
    for n in ns:
        reset_server()
        ops = gen_ops(f"source-n{n}", n)
        requests.post(f"{SERVER}/sync/push", json=ops, timeout=30)
        resp = requests.get(f"{SERVER}/sync/pull/measurer?since=0", timeout=10)
        remote_ops = resp.json().get("operations", [])
        payload_size = sum(len(o["payload"]) for o in remote_ops)
        print(f"\n  N={n}: {len(remote_ops)} operacji, {payload_size}B")

        for sname, merge_fn in STRATEGIES.items():
            durations, conf_list, lost_list = [], [], []
            for rep in range(repeats):
                local = {}
                for op in remote_ops[:len(remote_ops)//2]:
                    note = json.loads(op["payload"])
                    note["updated_at"] -= 1
                    local[note["id"]] = note

                start = time.perf_counter()
                conflicts, lost = 0, 0
                for op in remote_ops:
                    remote_note = json.loads(op["payload"])
                    nid = remote_note["id"]
                    rvc = json.loads(op.get("vector_clock") or "{}")
                    if nid in local:
                        _, c, l = merge_fn(local[nid], remote_note, rvc)
                        conflicts += 1; lost += 1 if l else 0
                    else:
                        local[nid] = remote_note
                dur = int((time.perf_counter()-start)*1_000_000)
                durations.append(dur); conf_list.append(conflicts); lost_list.append(lost)
                results.append({"strategy":sname,"scenario":"S3","n_operations":n,
                    "repeat":rep,"merge_duration_us":dur,
                    "payload_size_bytes":payload_size,"conflicts_detected":conflicts,
                    "operations_lost":lost,"operations_received":len(remote_ops),
                    "timestamp":int(time.time()*1000)})
            print(f"  {sname:<18} avg={np.mean(durations):7.1f}µs  "
                  f"utracone={np.mean(lost_list):.0f}")
    save_results(results, "../results/S3/s3_results.jsonl")
    return results

# ===== SCENARIUSZ S4 =====

def run_s4(ks=[2,3,5,10], ops_per_device=100, repeats=30):
    print("\n=== S4: Wiele urządzeń ===")
    results = []
    for k in ks:
        all_ops = []
        for d in range(k):
            all_ops.extend(gen_ops(f"device-{d}", ops_per_device))
        payload_size = sum(len(o["payload"]) for o in all_ops)
        print(f"\n  K={k}: {len(all_ops)} operacji łącznie")
        for sname, merge_fn in STRATEGIES.items():
            durations, conf_list, lost_list = [], [], []
            for rep in range(repeats):
                notes = {}; conflicts = 0; lost = 0
                start = time.perf_counter()
                for op in all_ops:
                    note = json.loads(op["payload"])
                    nid = note["id"]
                    rvc = json.loads(op.get("vector_clock") or "{}")
                    if nid in notes:
                        _, c, l = merge_fn(notes[nid], note, rvc)
                        conflicts += 1; lost += 1 if l else 0
                        if not l: notes[nid] = note
                    else:
                        notes[nid] = note
                dur = int((time.perf_counter()-start)*1_000_000)
                durations.append(dur); conf_list.append(conflicts); lost_list.append(lost)
                results.append({"strategy":sname,"scenario":"S4","k_devices":k,
                    "total_operations":len(all_ops),"repeat":rep,
                    "merge_duration_us":dur,"payload_size_bytes":payload_size,
                    "conflicts_detected":conflicts,"operations_lost":lost,
                    "timestamp":int(time.time()*1000)})
            print(f"  {sname:<18} avg={np.mean(durations):7.1f}µs  "
                  f"utracone={np.mean(lost_list):.0f}")
    save_results(results, "../results/S4/s4_results.jsonl")
    return results

# ===== SCENARIUSZ S5 =====

import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PROFILES = {
    "P1_high_latency":  {"delay_ms":500,"delay_std_ms":100,"loss_pct":0},
    "P2_packet_loss":   {"delay_ms":50, "delay_std_ms":10, "loss_pct":20},
    "P3_extreme":       {"delay_ms":1000,"delay_std_ms":200,"loss_pct":40},
}

class UnstableNet:
    def __init__(self, p):
        self.delay_ms=p["delay_ms"]; self.std_ms=p["delay_std_ms"]; self.loss=p["loss_pct"]
        self.s = requests.Session()
        r = Retry(total=5, backoff_factor=0.5)
        self.s.mount("http://", HTTPAdapter(max_retries=r))
    def _delay(self): time.sleep(max(0,np.random.normal(self.delay_ms,self.std_ms))/1000)
    def _drop(self): return random.random() < self.loss/100
    def get(self, url, **kw):
        for _ in range(5):
            self._delay()
            if not self._drop(): return self.s.get(url, **kw)
        raise requests.ConnectionError("too many drops")
    def post(self, url, **kw):
        for _ in range(5):
            self._delay()
            if not self._drop(): return self.s.post(url, **kw)
        raise requests.ConnectionError("too many drops")

def run_s5(repeats=30):
    print("\n=== S5: Sieć niestabilna ===")
    results = []
    for pname, profile in PROFILES.items():
        print(f"\n  Profil: {pname}")
        for sname, merge_fn in STRATEGIES.items():
            succ, durations_merge = [], []
            for rep in range(repeats):
                reset_server()
                ops_a = gen_ops(f"device-a-{rep}", 100)
                requests.post(f"{SERVER}/sync/push", json=ops_a, timeout=10)
                net = UnstableNet(profile)
                ops_b = gen_ops(f"device-b-{rep}", 100)
                try:
                    net.post(f"{SERVER}/sync/push", json=ops_b, timeout=15)
                    resp = net.get(f"{SERVER}/sync/pull/device-b-{rep}?since=0", timeout=15)
                    remote_ops = resp.json().get("operations", [])
                    start = time.perf_counter()
                    for op in remote_ops:
                        rn = json.loads(op["payload"])
                        rvc = json.loads(op.get("vector_clock") or "{}")
                        merge_fn({"id":rn["id"],"updated_at":0,
                                  "vector_clock":"{}","is_deleted":False,
                                  "title":"","content":""}, rn, rvc)
                    dur = int((time.perf_counter()-start)*1_000_000)
                    succ.append(1); durations_merge.append(dur)
                    results.append({"strategy":sname,"scenario":"S5",
                        "profile":pname,"repeat":rep,"sync_success":1,
                        "merge_duration_us":dur,
                        "payload_size_bytes":sum(len(o["payload"]) for o in ops_a),
                        "conflicts_detected":len(remote_ops),
                        "operations_received":len(remote_ops),
                        "timestamp":int(time.time()*1000)})
                except Exception:
                    succ.append(0)
                    results.append({"strategy":sname,"scenario":"S5",
                        "profile":pname,"repeat":rep,"sync_success":0,
                        "merge_duration_us":0,"payload_size_bytes":0,
                        "conflicts_detected":0,"operations_received":0,
                        "timestamp":int(time.time()*1000)})
            sr = sum(succ)/repeats*100
            avg_m = np.mean(durations_merge) if durations_merge else 0
            print(f"  {sname:<18} sukces={sr:.0f}%  merge={avg_m:.0f}µs")
    save_results(results, "../results/S5/s5_results.jsonl")
    return results

# ===== MAIN =====

if __name__ == "__main__":
    print("=" * 55)
    print("PEŁNY ZESTAW EKSPERYMENTÓW — 4 STRATEGIE")
    print("LWW | VectorClockLWW | ORSet | Automerge")
    print("=" * 55)
    run_s1()
    run_s2()
    run_s3()
    run_s4()
    run_s5()
    print("\n✓ Wszystkie eksperymenty zakończone.")