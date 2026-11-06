"""
Pełny zestaw eksperymentów S1–S5 dla czterech strategii scalania:
LWW | VectorClockLWW | ORSet | Automerge (symulacja).

Metryki konfliktów i utraty liczy wspólny harness (klasa Replica) na podstawie
prawdziwej historii przyczynowej operacji — funkcje strategii zwracają tylko
wynik scalenia i nie mają wpływu na to, jak są oceniane.

- conflicts_detected — liczba operacji przychodzących, które są współbieżne
  (w sensie zegarów wektorowych) z co najmniej jedną „żywą” wersją notatki.
- operations_lost — liczba operacji, których efektu nie ma w stanie końcowym repliki,
  chociaż żadna późniejsza operacja ich nie nadpisała (żadna ich „nie widziała”).
  Edycja jest zachowana, jeśli jej treść jest w wyniku (content lub _conflicts);
  usunięcie jest zachowane, jeśli wynik jest usunięty.

Uruchomienie (serwer potrzebny dla S3 i S5):
    python run_all_experiments.py [--repeats 30] [--scenarios S1,S2,S3,S4,S5] [--out ../results]
"""
import argparse
import json
import os
import random
import time
import uuid

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SERVER = "http://localhost:8000"
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")


def now_ms():
    return int(time.time() * 1000)


def reset_server():
    try:
        requests.delete(f"{SERVER}/sync/reset", timeout=5)
    except Exception as e:
        print(f"Reset error: {e}")


# ===== ZEGARY WEKTOROWE =====

def vc_parse(note):
    vc = note.get("vector_clock")
    if isinstance(vc, dict):
        return vc
    return json.loads(vc) if vc else {}


def vc_leq(a, b):
    return all(v <= b.get(k, 0) for k, v in a.items())


def vc_relation(a, b):
    """Relacja a względem b: 'before' (a -> b), 'after', 'equal', 'concurrent'."""
    le, ge = vc_leq(a, b), vc_leq(b, a)
    if le and ge:
        return "equal"
    if le:
        return "before"
    if ge:
        return "after"
    return "concurrent"


def vc_max(a, b):
    return {k: max(a.get(k, 0), b.get(k, 0)) for k in set(a) | set(b)}


# ===== STRATEGIE SCALANIA =====
# Każda funkcja: (lokalna notatka, zdalna notatka) -> wynikowa notatka.

def merge_lww(local, remote):
    """Wygrywa nowszy znacznik czasu."""
    return dict(remote) if remote["updated_at"] > local["updated_at"] else dict(local)


def merge_vc(local, remote):
    """Zegary wektorowe; przy operacjach współbieżnych fallback do LWW."""
    rel = vc_relation(vc_parse(local), vc_parse(remote))
    if rel == "before":
        return dict(remote)
    if rel in ("after", "equal"):
        return dict(local)
    return merge_lww(local, remote)


def merge_orset(local, remote):
    """
    OR-Set (add-wins) na poziomie istnienia notatki: przy współbieżnym
    usunięciu i edycji wygrywa edycja. Usunięcie, które „widziało” edycję
    (nastąpiło po niej przyczynowo), wygrywa. Treść przy dwóch współbieżnych
    edycjach OR-Set nie rozstrzyga — fallback do LWW.
    """
    rel = vc_relation(vc_parse(local), vc_parse(remote))
    if rel == "before":
        return dict(remote)
    if rel in ("after", "equal"):
        return dict(local)
    if local["is_deleted"] != remote["is_deleted"]:
        return dict(remote if local["is_deleted"] else local)
    return merge_lww(local, remote)


VERSION_KEYS = ("title", "content", "is_deleted", "updated_at",
                "vector_clock", "author", "op_id")


def _versions(note):
    if "_versions" in note:
        return note["_versions"]
    return [{k: note[k] for k in VERSION_KEYS}]


def _lamport_key(v):
    # Automerge wybiera „zwycięzcę” po (licznik, actorId) — deterministycznie
    return (sum(vc_parse(v).values()), v["author"])


def merge_automerge(local, remote):
    """
    Symulacja Automerge: rejestr wielowartościowy (multi-value register).
    Zachowuje wszystkie współbieżne wartości, odrzuca tylko te nadpisane
    przyczynowo. Zwycięzca widoczny w `content` wybierany deterministycznie,
    pozostałe wartości trafiają do `_conflicts`. Usunięcie nie jest wartością:
    współbieżny zapis „przeżywa” usunięcie.
    """
    cands = _versions(local) + _versions(remote)
    live = []
    for v in cands:
        vv = vc_parse(v)
        if any(vc_relation(vv, vc_parse(w)) == "before" for w in cands):
            continue
        if any(vc_relation(vv, vc_parse(w)) == "equal" for w in live):
            continue
        live.append(v)
    values = [v for v in live if not v["is_deleted"]]
    pool = values or live
    winner = max(pool, key=_lamport_key)
    merged = {**local, **{k: winner[k] for k in VERSION_KEYS}}
    merged["_conflicts"] = [v["content"] for v in values if v is not winner]
    merged["_versions"] = pool
    return merged


STRATEGIES = {
    "LWW": merge_lww,
    "VectorClockLWW": merge_vc,
    "ORSet": merge_orset,
    "Automerge": merge_automerge,
}


# ===== HARNESS: replika + metryki =====

def _preserved(v, result):
    if v["is_deleted"]:
        return bool(result.get("is_deleted"))
    if result.get("is_deleted"):
        return False
    return v["content"] == result["content"] or v["content"] in result.get("_conflicts", [])


class Replica:
    """Lokalna kopia notatek + wzorcowa historia przyczynowa do liczenia metryk."""

    def __init__(self, merge_fn):
        self.merge_fn = merge_fn
        self.notes = {}
        self.frontier = {}     # note_id -> wersje, których nic nie nadpisało
        self.reset_stats()

    def reset_stats(self):
        self.conflicts = 0
        self.merge_ns = 0

    def apply(self, incoming):
        nid = incoming["id"]
        v_in = {"op_id": incoming["op_id"], "content": incoming["content"],
                "is_deleted": incoming["is_deleted"], "vc": vc_parse(incoming)}

        # 1. Wzorcowa historia (niezależna od strategii)
        front = self.frontier.get(nid, [])
        rels = [vc_relation(f["vc"], v_in["vc"]) for f in front]
        if any(r == "concurrent" for r in rels):
            self.conflicts += 1
        if any(r in ("after", "equal") for r in rels):
            new_front = front                       # operacja przestarzała / duplikat
        else:
            new_front = [f for f, r in zip(front, rels) if r != "before"] + [v_in]
        self.frontier[nid] = new_front

        # 2. Scalenie wybraną strategią
        held = self.notes.get(nid)
        if held is None:
            result = dict(incoming)
        else:
            t = time.perf_counter_ns()
            result = self.merge_fn(held, incoming)
            self.merge_ns += time.perf_counter_ns() - t
            result["vector_clock"] = json.dumps(vc_max(vc_parse(held), vc_parse(incoming)))
        self.notes[nid] = result
        return result

    def lost_ops(self):
        """
        Utrata liczona na stanie końcowym: operacje, których nic nie nadpisało
        przyczynowo, a ich efektu nie ma w bieżącym stanie repliki. Operacja
        nadpisana później przez następcę, który ją „widział”, nie jest stratą.
        """
        return sum(1 for nid, front in self.frontier.items()
                   for f in front if not _preserved(f, self.notes[nid]))


def time_merge_us(fn, local, remote, inner=1000):
    """Średni czas pojedynczego scalenia (µs) — pętla, bo jedno wywołanie ≈ rozdzielczość zegara."""
    t = time.perf_counter_ns()
    for _ in range(inner):
        fn(local, remote)
    return (time.perf_counter_ns() - t) / inner / 1000


# ===== GENERATORY =====

def make_note(nid, author, vc, ts, deleted=False):
    op_id = uuid.uuid4().hex
    return {
        "id": nid,
        "title": f"{author} {op_id[:6]}",
        "content": f"Treść od {author} ({op_id[:8]})",
        "created_at": ts,
        "updated_at": ts,
        "is_deleted": deleted,
        "vector_clock": json.dumps(vc),
        "author": author,
        "op_id": op_id,
    }


def gen_device_ops(rng, device, n, shared_ids, private_ratio, t0, skew_ms):
    """
    N edycji wykonanych offline przez jedno urządzenie. Zegar wektorowy per
    notatka: urządzenie zna tylko własną historię, więc edycje różnych
    urządzeń tej samej notatki są współbieżne. Znaczniki czasu rosną
    monotonicznie w obrębie urządzenia, z przesunięciem zegara `skew_ms`.
    """
    counters = {}
    notes = []
    for i in range(n):
        if private_ratio and rng.random() < private_ratio:
            nid = f"private-{device}-{rng.randrange(len(shared_ids))}"
        else:
            nid = rng.choice(shared_ids)
        counters[nid] = counters.get(nid, 0) + 1
        ts = t0 + skew_ms + i * 10 + rng.randint(0, 9)
        notes.append(make_note(nid, device, {device: counters[nid]}, ts))
    return notes


def to_op(note):
    return {
        "id": uuid.uuid4().hex,
        "note_id": note["id"],
        "operation_type": "DELETE" if note["is_deleted"] else "UPDATE",
        "payload": json.dumps(note),
        "vector_clock": note["vector_clock"],
        "timestamp": note["updated_at"],
        "device_id": note["author"],
    }


def interleave(rng, streams):
    """Losowa kolejność nadejścia operacji z zachowaniem kolejności w obrębie urządzenia."""
    idx = [0] * len(streams)
    remaining = [i for i, s in enumerate(streams) if s]
    out = []
    while remaining:
        i = rng.choice(remaining)
        out.append(streams[i][idx[i]])
        idx[i] += 1
        if idx[i] == len(streams[i]):
            remaining.remove(i)
    return out


def push_pull(ops, as_device, session=None):
    s = session or requests
    s.post(f"{SERVER}/sync/push", json=ops, timeout=30)
    resp = s.get(f"{SERVER}/sync/pull/{as_device}?since=0", timeout=30)
    return [json.loads(o["payload"]) for o in resp.json().get("operations", [])]


# ===== WYNIKI =====

def save_results(results, out_dir, sub, name):
    path = os.path.join(out_dir, sub, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    print(f"  ✓ Zapisano {len(results)} wyników → {os.path.normpath(path)}")


def summarize(results, group_keys):
    groups = {}
    for r in results:
        groups.setdefault(tuple(r[k] for k in group_keys), []).append(r)
    for key, rows in groups.items():
        ok = [r for r in rows if r.get("sync_success", 1)]
        label = " ".join(str(k) for k in key)
        line = f"  {label:<38}"
        if ok:
            line += (f" czas={np.mean([r['merge_duration_us'] for r in ok]):9.2f}µs"
                     f"  konflikty={np.mean([r['conflicts_detected'] for r in ok]):6.1f}"
                     f"  utracone={np.mean([r['operations_lost'] for r in ok]):6.1f}")
        if "edit_wins" in rows[0]:
            line += f"  edit_wins={sum(r['edit_wins'] for r in rows)}/{len(rows)}"
        if "sync_success" in rows[0]:
            line += f"  sukces={len(ok) / len(rows) * 100:.0f}%"
        print(line)


# ===== S1: konflikt edycji =====

def run_s1(repeats, out_dir):
    print("\n=== S1: Konflikt edycji ===")
    results = []
    for rep in range(repeats):
        rng = random.Random(1000 + rep)
        t0 = now_ms()
        nid = uuid.uuid4().hex
        base = make_note(nid, "base", {"base": 1}, t0 - 10_000)
        a = make_note(nid, "device-a", {"base": 1, "device-a": 1}, t0 + rng.randint(0, 50))
        # concurrent: B edytuje bazę niezależnie od A (zegar B późniejszy)
        b = make_note(nid, "device-b", {"base": 1, "device-b": 1}, t0 + rng.randint(51, 100))
        # causal_skew: B pobrał wersję A i dopiero wtedy edytował,
        # ale zegar systemowy B spóźnia się o 5 s
        b_causal = make_note(nid, "device-b", {"base": 1, "device-a": 1, "device-b": 1},
                             a["updated_at"] - 5000)
        for variant, remote in (("concurrent", b), ("causal_skew", b_causal)):
            for sname, fn in STRATEGIES.items():
                r = Replica(fn)
                r.apply(base)
                r.apply(a)                       # stan urządzenia A
                r.reset_stats()
                dur = time_merge_us(fn, r.notes[nid], remote)
                res = r.apply(remote)
                winner = "remote" if res["op_id"] == remote["op_id"] else "local"
                if res.get("_conflicts"):
                    winner += "+conflicts"
                results.append({
                    "strategy": sname, "scenario": "S1", "variant": variant,
                    "n_operations": 2, "repeat": rep,
                    "merge_duration_us": dur,
                    "payload_size_bytes": len(json.dumps(remote)),
                    "conflicts_detected": r.conflicts,
                    "operations_lost": r.lost_ops(),
                    "winner": winner,
                    "timestamp": now_ms(),
                })
    summarize(results, ["variant", "strategy"])
    save_results(results, out_dir, "S1", "s1_results.jsonl")
    return results


# ===== S2: konflikt delete/update =====

def run_s2(repeats, out_dir):
    print("\n=== S2: Konflikt delete/update ===")
    results = []
    for rep in range(repeats):
        rng = random.Random(2000 + rep)
        t0 = now_ms()
        nid = uuid.uuid4().hex
        base = make_note(nid, "base", {"base": 1}, t0 - 10_000)
        delete_later = rng.random() < 0.5        # losowo: które zdarzenie ma późniejszy zegar
        t_del, t_edit = (t0 + 50, t0) if delete_later else (t0, t0 + 50)
        note_del = make_note(nid, "device-a", {"base": 1, "device-a": 1}, t_del, deleted=True)
        note_edit = make_note(nid, "device-b", {"base": 1, "device-b": 1}, t_edit)
        for sname, fn in STRATEGIES.items():
            r = Replica(fn)
            r.apply(base)
            r.apply(note_del)                    # A usunął notatkę
            r.reset_stats()
            dur = time_merge_us(fn, r.notes[nid], note_edit)
            res = r.apply(note_edit)             # B ją edytował
            ew = 0 if res["is_deleted"] else 1
            results.append({
                "strategy": sname, "scenario": "S2", "n_operations": 2, "repeat": rep,
                "merge_duration_us": dur,
                "payload_size_bytes": len(json.dumps(note_edit)),
                "conflicts_detected": r.conflicts,
                "operations_lost": r.lost_ops(),
                "delete_later": delete_later,
                "delete_wins": 1 - ew, "edit_wins": ew,
                "timestamp": now_ms(),
            })
    summarize(results, ["strategy"])
    save_results(results, out_dir, "S2", "s2_results.jsonl")
    return results


# ===== S3: skalowalność po okresie offline =====

def run_s3(repeats, out_dir, ns=(10, 50, 100, 500, 1000)):
    print("\n=== S3: Skalowalność offline ===")
    results = []
    for n in ns:
        for rep in range(repeats):
            rng = random.Random(3000 + n * 100 + rep)
            t0 = now_ms()
            pool = [f"note-{i}" for i in range(max(1, n // 10))]
            a_notes = gen_device_ops(rng, "device-a", n, pool, 0, t0, 0)
            b_notes = gen_device_ops(rng, "device-b", n, pool, 0, t0, rng.randint(-50, 50))
            reset_server()
            remote = push_pull([to_op(x) for x in b_notes], "device-a")
            payload_size = sum(len(json.dumps(x)) for x in remote)
            for sname, fn in STRATEGIES.items():
                r = Replica(fn)
                for x in a_notes:                # lokalne edycje A
                    r.apply(x)
                r.reset_stats()
                for x in remote:                 # scalenie edycji B
                    r.apply(x)
                results.append({
                    "strategy": sname, "scenario": "S3", "n_operations": n, "repeat": rep,
                    "merge_duration_us": r.merge_ns / 1000,
                    "payload_size_bytes": payload_size,
                    "conflicts_detected": r.conflicts,
                    "operations_lost": r.lost_ops(),
                    "operations_received": len(remote),
                    "timestamp": now_ms(),
                })
    summarize(results, ["n_operations", "strategy"])
    save_results(results, out_dir, "S3", "s3_results.jsonl")
    return results


# ===== S4: wiele urządzeń =====

def run_s4(repeats, out_dir, ks=(2, 3, 5, 10), ops_per_device=100, shared_notes=10):
    print("\n=== S4: Wiele urządzeń ===")
    results = []
    for k in ks:
        for rep in range(repeats):
            rng = random.Random(4000 + k * 100 + rep)
            t0 = now_ms()
            pool = [f"shared-note-{i}" for i in range(shared_notes)]
            streams = [gen_device_ops(rng, f"device-{d}", ops_per_device, pool, 0.5,
                                      t0, rng.randint(-100, 100)) for d in range(k)]
            arrival = interleave(rng, streams)
            payload_size = sum(len(json.dumps(x)) for x in arrival)
            for sname, fn in STRATEGIES.items():
                r = Replica(fn)
                for x in arrival:
                    r.apply(x)
                results.append({
                    "strategy": sname, "scenario": "S4", "k_devices": k,
                    "total_operations": len(arrival), "repeat": rep,
                    "merge_duration_us": r.merge_ns / 1000,
                    "payload_size_bytes": payload_size,
                    "conflicts_detected": r.conflicts,
                    "operations_lost": r.lost_ops(),
                    "timestamp": now_ms(),
                })
    summarize(results, ["k_devices", "strategy"])
    save_results(results, out_dir, "S4", "s4_results.jsonl")
    return results


# ===== S5: niestabilna sieć =====

PROFILES = {
    "P1_high_latency": {"delay_ms": 500, "delay_std_ms": 100, "loss_pct": 0},
    "P2_packet_loss":  {"delay_ms": 50,  "delay_std_ms": 10,  "loss_pct": 20},
    "P3_extreme":      {"delay_ms": 1000, "delay_std_ms": 200, "loss_pct": 40},
}


class UnstableNet:
    """Symulacja opóźnień i utraty żądań po stronie klienta (max 5 prób na żądanie)."""

    def __init__(self, p, rng):
        self.p, self.rng = p, rng
        self.s = requests.Session()
        self.s.mount("http://", HTTPAdapter(max_retries=Retry(total=5, backoff_factor=0.5)))

    def _send(self, method, url, **kw):
        for _ in range(5):
            time.sleep(max(0.0, self.rng.gauss(self.p["delay_ms"], self.p["delay_std_ms"])) / 1000)
            if self.rng.random() >= self.p["loss_pct"] / 100:
                return getattr(self.s, method)(url, **kw)
        raise requests.ConnectionError("too many drops")

    def get(self, url, **kw):
        return self._send("get", url, **kw)

    def post(self, url, **kw):
        return self._send("post", url, **kw)


def run_s5(repeats, out_dir, ops_count=100, shared_notes=10):
    """
    Sieć jest symulowana raz na powtórzenie, a ten sam wynik synchronizacji
    jest scalany każdą strategią — sukces synchronizacji nie zależy od
    strategii, więc wszystkie widzą identyczne warunki.
    """
    print("\n=== S5: Sieć niestabilna ===")
    results = []
    for pname, profile in PROFILES.items():
        for rep in range(repeats):
            rng = random.Random(5000 + rep * 10 + list(PROFILES).index(pname))
            t0 = now_ms()
            pool = [f"note-{i}" for i in range(shared_notes)]
            a_notes = gen_device_ops(rng, "device-a", ops_count, pool, 0, t0, 0)
            b_notes = gen_device_ops(rng, "device-b", ops_count, pool, 0, t0, rng.randint(-50, 50))
            reset_server()
            requests.post(f"{SERVER}/sync/push", json=[to_op(x) for x in a_notes], timeout=10)
            net = UnstableNet(profile, rng)
            start = time.perf_counter()
            try:
                net.post(f"{SERVER}/sync/push", json=[to_op(x) for x in b_notes], timeout=15)
                resp = net.get(f"{SERVER}/sync/pull/device-b?since=0", timeout=15)
                remote = [json.loads(o["payload"]) for o in resp.json().get("operations", [])]
                success = 1
            except Exception:
                remote, success = [], 0
            sync_ms = (time.perf_counter() - start) * 1000

            for sname, fn in STRATEGIES.items():
                row = {"strategy": sname, "scenario": "S5", "profile": pname, "repeat": rep,
                       "sync_success": success, "sync_duration_ms": sync_ms,
                       "timestamp": now_ms()}
                if success:
                    r = Replica(fn)
                    for x in b_notes:
                        r.apply(x)
                    r.reset_stats()
                    for x in remote:
                        r.apply(x)
                    row.update({
                        "merge_duration_us": r.merge_ns / 1000,
                        "payload_size_bytes": sum(len(json.dumps(x)) for x in remote),
                        "conflicts_detected": r.conflicts,
                        "operations_lost": r.lost_ops(),
                        "operations_received": len(remote),
                    })
                else:
                    row.update({"merge_duration_us": 0, "payload_size_bytes": 0,
                                "conflicts_detected": 0, "operations_lost": 0,
                                "operations_received": 0})
                results.append(row)
    summarize(results, ["profile", "strategy"])
    save_results(results, out_dir, "S5", "s5_results.jsonl")
    return results


# ===== MAIN =====

SCENARIOS = {"S1": run_s1, "S2": run_s2, "S3": run_s3, "S4": run_s4, "S5": run_s5}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=30)
    ap.add_argument("--scenarios", default="S1,S2,S3,S4,S5")
    ap.add_argument("--out", default=RESULTS_DIR)
    args = ap.parse_args()
    chosen = [s.strip().upper() for s in args.scenarios.split(",") if s.strip()]

    if {"S3", "S5"} & set(chosen):
        try:
            requests.get(f"{SERVER}/health", timeout=3)
        except Exception:
            raise SystemExit(f"Serwer {SERVER} nie odpowiada — uruchom: uvicorn main:app --port 8000")

    print("=" * 55)
    print("PEŁNY ZESTAW EKSPERYMENTÓW — 4 STRATEGIE")
    print("LWW | VectorClockLWW | ORSet | Automerge")
    print("=" * 55)
    for s in chosen:
        SCENARIOS[s](args.repeats, args.out)
    print("\n✓ Wszystkie eksperymenty zakończone.")
