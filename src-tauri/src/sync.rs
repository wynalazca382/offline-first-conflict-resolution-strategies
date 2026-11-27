use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};
use serde::{Deserialize, Serialize};
use crate::models::{Note, Operation};

// ===== Typy pomocnicze =====

pub type VectorClock = HashMap<String, u64>;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SyncMetrics {
    pub strategy: String,
    pub scenario: String,
    pub merge_duration_us: u64,
    pub payload_size_bytes: usize,
    pub conflicts_detected: u32,
    pub operations_lost: u32,
    pub metadata_size_bytes: usize,
    pub timestamp: i64,
}

#[derive(Debug, Clone)]
pub enum Resolution {
    Local,
    Remote,
    Merged(Note),
}

// ===== Trait =====

pub trait SyncStrategy: Send + Sync {
    fn name(&self) -> &str;
    fn prepare_operation(&self, op: &mut Operation);
    fn resolve_conflict(&self, local: &Note, remote: &Note) -> Resolution;
}

// ===== Helpers =====

pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

pub fn parse_clock(s: &Option<String>) -> VectorClock {
    s.as_deref()
        .and_then(|v| serde_json::from_str(v).ok())
        .unwrap_or_default()
}

/// Maksimum po składnikach — zegar wersji, która „widziała” obie wersje.
pub fn merge_clocks(a: &Option<String>, b: &Option<String>) -> Option<String> {
    let (ca, cb) = (parse_clock(a), parse_clock(b));
    if ca.is_empty() && cb.is_empty() {
        return None;
    }
    let mut out = ca;
    for (k, v) in cb {
        let e = out.entry(k).or_insert(0);
        if v > *e { *e = v; }
    }
    Some(serde_json::to_string(&out).unwrap())
}

// ===== LWW =====

pub struct LWWStrategy;

impl SyncStrategy for LWWStrategy {
    fn name(&self) -> &str { "LWW" }

    fn prepare_operation(&self, op: &mut Operation) {
        op.timestamp = now_ms();
    }

    fn resolve_conflict(&self, local: &Note, remote: &Note) -> Resolution {
        if remote.updated_at > local.updated_at {
            Resolution::Remote
        } else {
            Resolution::Local
        }
    }
}

// ===== LWW + Vector Clocks =====

pub struct VectorClockLWWStrategy {
    pub device_id: String,
}

impl VectorClockLWWStrategy {
    pub(crate) fn happens_before(a: &VectorClock, b: &VectorClock) -> bool {
        let mut strictly_less = false;
        let keys: std::collections::HashSet<_> =
            a.keys().chain(b.keys()).collect();
        for key in keys {
            let va = a.get(key).copied().unwrap_or(0);
            let vb = b.get(key).copied().unwrap_or(0);
            if va > vb { return false; }
            if va < vb { strictly_less = true; }
        }
        strictly_less
    }
}

impl SyncStrategy for VectorClockLWWStrategy {
    fn name(&self) -> &str { "VectorClockLWW" }

    fn prepare_operation(&self, op: &mut Operation) {
        op.timestamp = now_ms();
        let mut clock: VectorClock = parse_clock(&op.vector_clock);
        *clock.entry(self.device_id.clone()).or_insert(0) += 1;
        op.vector_clock = Some(serde_json::to_string(&clock).unwrap());
    }

    fn resolve_conflict(&self, local: &Note, remote: &Note) -> Resolution {
        let lc = parse_clock(&local.vector_clock);
        let rc = parse_clock(&remote.vector_clock);
        if Self::happens_before(&lc, &rc) { return Resolution::Remote; }
        if Self::happens_before(&rc, &lc) { return Resolution::Local; }
        // współbieżne — fallback do znacznika czasu
        if remote.updated_at > local.updated_at {
            Resolution::Remote
        } else {
            Resolution::Local
        }
    }
}

// ===== OR-Set =====

#[derive(Clone, Serialize, Deserialize, Default)]
pub struct ORSetState {
    pub entries: HashMap<String, Vec<String>>,
    pub tombstones: Vec<String>,
}

impl ORSetState {
    pub fn add(&mut self, id: &str) -> String {
        let tag = uuid::Uuid::new_v4().to_string();
        self.entries
            .entry(id.to_string())
            .or_default()
            .push(tag.clone());
        tag
    }

    pub fn remove(&mut self, id: &str) {
        if let Some(tags) = self.entries.remove(id) {
            self.tombstones.extend(tags);
        }
    }

    pub fn merge(&self, remote: &ORSetState) -> ORSetState {
        let mut tombstones = self.tombstones.clone();
        tombstones.extend(remote.tombstones.iter().cloned());
        tombstones.sort();
        tombstones.dedup();

        let all_ids: std::collections::HashSet<_> = self
            .entries.keys()
            .chain(remote.entries.keys())
            .cloned()
            .collect();

        let mut entries = HashMap::new();
        for id in all_ids {
            let tags: Vec<String> = self.entries
                .get(&id).iter().flat_map(|v| v.iter())
                .chain(remote.entries.get(&id).iter().flat_map(|v| v.iter()))
                .filter(|t| !tombstones.contains(t))
                .cloned()
                .collect();
            if !tags.is_empty() {
                entries.insert(id, tags);
            }
        }
        ORSetState { entries, tombstones }
    }

    pub fn contains(&self, id: &str) -> bool {
        self.entries.contains_key(id)
    }
}

pub struct ORSetStrategy {
    pub device_id: String,
}

impl SyncStrategy for ORSetStrategy {
    fn name(&self) -> &str { "ORSet" }

    fn prepare_operation(&self, op: &mut Operation) {
        op.timestamp = now_ms();
        let mut clock: VectorClock = parse_clock(&op.vector_clock);
        *clock.entry(self.device_id.clone()).or_insert(0) += 1;
        op.vector_clock = Some(serde_json::to_string(&clock).unwrap());
    }

    // Reguła add-wins na poziomie istnienia notatki (jak merge_orset w Pythonie):
    // usunięcie przegrywa ze współbieżną edycją, dwie współbieżne edycje — LWW.
    fn resolve_conflict(&self, local: &Note, remote: &Note) -> Resolution {
        let lc = parse_clock(&local.vector_clock);
        let rc = parse_clock(&remote.vector_clock);
        if VectorClockLWWStrategy::happens_before(&lc, &rc) { return Resolution::Remote; }
        if VectorClockLWWStrategy::happens_before(&rc, &lc) || (lc == rc && !lc.is_empty()) { return Resolution::Local; }
        if local.is_deleted != remote.is_deleted {
            return if local.is_deleted { Resolution::Remote } else { Resolution::Local };
        }
        if remote.updated_at > local.updated_at { Resolution::Remote } else { Resolution::Local }
    }
}

use std::fs::OpenOptions;
use std::io::Write;

pub fn record_metrics(metrics: &SyncMetrics) {
    let dir = std::path::Path::new("results").join(&metrics.strategy);
    std::fs::create_dir_all(&dir).ok();
    let path = dir.join(format!("{}.jsonl", metrics.scenario));
    let mut file = OpenOptions::new()
        .append(true)
        .create(true)
        .open(path)
        .unwrap();
    writeln!(file, "{}", serde_json::to_string(metrics).unwrap()).ok();
}