"""
Testy jednostkowe funkcji scalania i harnessu z run_all_experiments.py.

Sprawdzają przypadki, których wynik można przewidzieć analitycznie
(scenariusze S1 i S2), oraz poprawność liczenia konfliktów i utraty.

Uruchomienie (z katalogu server/):
    python -m unittest test_strategies -v
"""
import unittest

from run_all_experiments import (
    STRATEGIES, Replica, make_note, merge_automerge, merge_lww,
    merge_orset, merge_vc, vc_relation,
)


def s1(variant):
    """Stan bazowy, wersja A i wersja B jak w run_s1()."""
    base = make_note("n", "base", {"base": 1}, 0)
    a = make_note("n", "device-a", {"base": 1, "device-a": 1}, 1000)
    if variant == "concurrent":
        b = make_note("n", "device-b", {"base": 1, "device-b": 1}, 1050)
    else:  # causal_skew: B widział A, ale jego zegar spóźnia się o 5 s
        b = make_note("n", "device-b", {"base": 1, "device-a": 1, "device-b": 1}, 1000 - 5000)
    return base, a, b


def run(fn, *notes):
    r = Replica(fn)
    for x in notes[:-1]:
        r.apply(x)
    r.reset_stats()
    res = r.apply(notes[-1])
    return r, res


class VectorClockTests(unittest.TestCase):
    def test_relations(self):
        self.assertEqual(vc_relation({"a": 1}, {"a": 2}), "before")
        self.assertEqual(vc_relation({"a": 2}, {"a": 1}), "after")
        self.assertEqual(vc_relation({"a": 1}, {"a": 1}), "equal")
        self.assertEqual(vc_relation({"a": 1}, {"b": 1}), "concurrent")


class S1Tests(unittest.TestCase):
    def test_concurrent_loses_one_version_except_automerge(self):
        expected = {"LWW": 1, "VectorClockLWW": 1, "ORSet": 1, "Automerge": 0}
        for name, fn in STRATEGIES.items():
            r, _ = run(fn, *s1("concurrent"))
            self.assertEqual(r.conflicts, 1, name)
            self.assertEqual(r.lost_ops(), expected[name], name)

    def test_causal_skew_only_lww_loses(self):
        expected = {"LWW": 1, "VectorClockLWW": 0, "ORSet": 0, "Automerge": 0}
        for name, fn in STRATEGIES.items():
            r, _ = run(fn, *s1("causal_skew"))
            self.assertEqual(r.conflicts, 0, name)
            self.assertEqual(r.lost_ops(), expected[name], name)

    def test_automerge_keeps_loser_in_conflicts(self):
        _, a, b = s1("concurrent")
        res = merge_automerge(a, b)
        self.assertEqual({res["content"], *res["_conflicts"]}, {a["content"], b["content"]})

    def test_automerge_is_order_independent(self):
        _, a, b = s1("concurrent")
        self.assertEqual(merge_automerge(a, b)["content"], merge_automerge(b, a)["content"])


class S2Tests(unittest.TestCase):
    def notes(self, delete_later):
        base = make_note("n", "base", {"base": 1}, 0)
        t_del, t_edit = (1050, 1000) if delete_later else (1000, 1050)
        d = make_note("n", "device-a", {"base": 1, "device-a": 1}, t_del, deleted=True)
        e = make_note("n", "device-b", {"base": 1, "device-b": 1}, t_edit)
        return base, d, e

    def test_orset_and_automerge_always_keep_edit(self):
        for delete_later in (True, False):
            for fn in (merge_orset, merge_automerge):
                _, res = run(fn, *self.notes(delete_later))
                self.assertFalse(res["is_deleted"])

    def test_lww_and_vc_follow_timestamp(self):
        for fn in (merge_lww, merge_vc):
            _, res = run(fn, *self.notes(delete_later=True))
            self.assertTrue(res["is_deleted"])
            _, res = run(fn, *self.notes(delete_later=False))
            self.assertFalse(res["is_deleted"])

    def test_every_strategy_loses_exactly_one_operation(self):
        for name, fn in STRATEGIES.items():
            r, _ = run(fn, *self.notes(delete_later=True))
            self.assertEqual(r.lost_ops(), 1, name)


class HarnessTests(unittest.TestCase):
    def test_causal_successor_is_not_a_loss(self):
        v1 = make_note("n", "a", {"a": 1}, 0)
        v2 = make_note("n", "a", {"a": 2}, 10)
        for name, fn in STRATEGIES.items():
            r, _ = run(fn, v1, v2)
            self.assertEqual((r.conflicts, r.lost_ops()), (0, 0), name)

    def test_duplicate_delivery_does_not_change_state_or_loss(self):
        # Uwaga: duplikat jest liczony ponownie jako konflikt (licznik conflicts),
        # ale nie zmienia stanu ani utraty. W scenariuszach S1–S5 duplikaty nie występują.
        _, a, b = s1("concurrent")
        for name, fn in STRATEGIES.items():
            r1, r2 = Replica(fn), Replica(fn)
            for x in (a, b):
                r1.apply(x)
            for x in (a, b, b):
                r2.apply(x)
            self.assertEqual(r1.notes["n"]["content"], r2.notes["n"]["content"], name)
            self.assertEqual(r1.lost_ops(), r2.lost_ops(), name)


if __name__ == "__main__":
    unittest.main()
