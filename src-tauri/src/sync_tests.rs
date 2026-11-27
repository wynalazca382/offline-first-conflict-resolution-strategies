//! Testy jednostkowe strategii z sync.rs: reguły rozstrzygania konfliktów
//! oraz własności algebraiczne łączenia stanów OR-Set (podstawa SEC).
use crate::models::{Note, Operation};
use crate::sync::*;

fn note(content: &str, updated_at: i64, clock: &str, deleted: bool) -> Note {
    Note {
        id: "n1".into(),
        title: "t".into(),
        content: content.into(),
        created_at: 0,
        updated_at,
        is_deleted: deleted,
        vector_clock: if clock.is_empty() { None } else { Some(clock.into()) },
    }
}

fn is_remote(r: Resolution) -> bool { matches!(r, Resolution::Remote) }

// ===== LWW =====

#[test]
fn lww_newer_timestamp_wins() {
    let s = LWWStrategy;
    assert!(is_remote(s.resolve_conflict(&note("a", 1, "", false), &note("b", 2, "", false))));
    assert!(!is_remote(s.resolve_conflict(&note("a", 2, "", false), &note("b", 1, "", false))));
}

#[test]
fn lww_tie_keeps_local() {
    let s = LWWStrategy;
    assert!(!is_remote(s.resolve_conflict(&note("a", 5, "", false), &note("b", 5, "", false))));
}

// ===== VectorClockLWW =====

fn vc() -> VectorClockLWWStrategy { VectorClockLWWStrategy { device_id: "A".into() } }

#[test]
fn vc_causally_later_wins_despite_older_timestamp() {
    // B widział wersję A, ale jego zegar systemowy się spóźnia (S1-causal_skew)
    let local = note("a", 10_000, r#"{"A":1}"#, false);
    let remote = note("b", 5_000, r#"{"A":1,"B":1}"#, false);
    assert!(is_remote(vc().resolve_conflict(&local, &remote)));
    assert!(!is_remote(LWWStrategy.resolve_conflict(&local, &remote)));
}

#[test]
fn vc_concurrent_falls_back_to_timestamp() {
    let local = note("a", 1, r#"{"A":1}"#, false);
    let remote = note("b", 2, r#"{"B":1}"#, false);
    assert!(is_remote(vc().resolve_conflict(&local, &remote)));
    assert!(!is_remote(vc().resolve_conflict(&remote, &local)));
}

#[test]
fn prepare_operation_increments_own_component() {
    let mut op = Operation {
        id: "o".into(), note_id: "n1".into(), operation_type: "UPDATE".into(),
        payload: String::new(), vector_clock: Some(r#"{"A":2,"B":7}"#.into()),
        timestamp: 0, device_id: "A".into(), synced: false,
    };
    vc().prepare_operation(&mut op);
    let c = VectorClockLWWStrategy::parse_clock(&op.vector_clock);
    assert_eq!(c["A"], 3);
    assert_eq!(c["B"], 7);
    assert!(op.timestamp > 0);
}

#[test]
fn merge_clocks_is_componentwise_max() {
    let m = VectorClockLWWStrategy::parse_clock(&merge_clocks(&Some(r#"{"A":3,"B":1}"#.into()), &Some(r#"{"B":4,"C":2}"#.into())));
    assert_eq!((m["A"], m["B"], m["C"]), (3, 4, 2));
    assert_eq!(merge_clocks(&None, &None), None);
}

// ===== OR-Set: reguła add-wins w aplikacji =====

fn orset() -> ORSetStrategy { ORSetStrategy { device_id: "A".into() } }

#[test]
fn orset_concurrent_edit_beats_delete_regardless_of_timestamp() {
    let deleted = note("", 100, r#"{"A":1}"#, true);
    let edited = note("b", 1, r#"{"B":1}"#, false);
    assert!(is_remote(orset().resolve_conflict(&deleted, &edited)));
    assert!(!is_remote(orset().resolve_conflict(&edited, &deleted)));
}

#[test]
fn orset_delete_that_saw_the_edit_wins() {
    let edited = note("b", 100, r#"{"B":1}"#, false);
    let deleted = note("", 1, r#"{"A":1,"B":1}"#, true);
    assert!(is_remote(orset().resolve_conflict(&edited, &deleted)));
}

#[test]
fn orset_concurrent_edits_fall_back_to_lww() {
    let local = note("a", 1, r#"{"A":1}"#, false);
    let remote = note("b", 2, r#"{"B":1}"#, false);
    assert!(is_remote(orset().resolve_conflict(&local, &remote)));
}

// ===== OR-Set: własności join (ORSetState::merge) =====

fn canon(s: &ORSetState) -> (Vec<(String, Vec<String>)>, Vec<String>) {
    let mut e: Vec<_> = s.entries.iter()
        .map(|(k, v)| { let mut v = v.clone(); v.sort(); v.dedup(); (k.clone(), v) })
        .collect();
    e.sort();
    let mut t = s.tombstones.clone();
    t.sort();
    t.dedup();
    (e, t)
}

fn sample() -> (ORSetState, ORSetState, ORSetState) {
    let mut a = ORSetState::default();
    a.add("x"); a.add("y"); a.remove("y");
    let mut b = a.clone();
    b.add("y"); b.remove("x"); b.add("z");
    let mut c = ORSetState::default();
    c.add("x"); c.add("w");
    (a, b, c)
}

#[test]
fn orset_merge_is_commutative() {
    let (a, b, _) = sample();
    assert_eq!(canon(&a.merge(&b)), canon(&b.merge(&a)));
}

#[test]
fn orset_merge_is_associative() {
    let (a, b, c) = sample();
    assert_eq!(canon(&a.merge(&b).merge(&c)), canon(&a.merge(&b.merge(&c))));
}

#[test]
fn orset_merge_is_idempotent() {
    let (a, b, _) = sample();
    assert_eq!(canon(&a.merge(&a)), canon(&a));
    let ab = a.merge(&b);
    assert_eq!(canon(&ab.merge(&ab)), canon(&ab));
}

#[test]
fn orset_add_wins_over_concurrent_remove() {
    let mut base = ORSetState::default();
    base.add("x");
    let mut r1 = base.clone();
    r1.remove("x");               // usuwa tagi, które widział
    let mut r2 = base.clone();
    r2.add("x");                  // współbieżne dodanie z nowym tagiem
    let m = r1.merge(&r2);
    assert!(m.contains("x"));
    assert_eq!(m.entries["x"].len(), 1);
}
