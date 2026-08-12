use tauri::Manager;
use std::sync::{Arc, Mutex};
use uuid::Uuid;

mod db;
mod sync;
mod models;

use db::Database;
use models::{Note, Operation};
use sync::{LWWStrategy, SyncStrategy, Resolution, now_ms};

pub struct AppState {
    pub db: Arc<Database>,
    pub strategy: Arc<dyn SyncStrategy>,
    pub device_id: String,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let app_dir = app.path().app_data_dir()
                .expect("Brak katalogu danych");
            std::fs::create_dir_all(&app_dir)?;
            let db_path = std::env::var("NOTES_DB")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|_| app_dir.join("notes.db"));

            let db = Arc::new(
                Database::new(db_path.to_str().unwrap())
                    .expect("Błąd inicjalizacji bazy")
            );

            let device_id = db.get_meta("device_id")
                .unwrap_or(None)
                .unwrap_or_else(|| {
                    let id = Uuid::new_v4().to_string();
                    db.set_meta("device_id", &id).ok();
                    id
                });

            let strategy: Arc<dyn SyncStrategy> = match std::env::var("SYNC_STRATEGY")
                .unwrap_or_else(|_| "LWW".to_string())
                .as_str()
            {
                "VectorClockLWW" => Arc::new(sync::VectorClockLWWStrategy {
                    device_id: device_id.clone(),
                }),
                "ORSet" => Arc::new(sync::ORSetStrategy),
                _ => Arc::new(LWWStrategy),
            };

            let state = AppState {
                db,
                strategy,
                device_id,
            };

            app.manage(Mutex::new(state));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_notes,
            create_note,
            update_note,
            delete_note,
            sync_notes,
        ])
        .run(tauri::generate_context!())
        .expect("błąd uruchamiania aplikacji");
}

#[tauri::command]
fn get_notes(state: tauri::State<Mutex<AppState>>) -> Vec<Note> {
    let state = state.lock().unwrap();
    state.db.get_notes().unwrap_or_default()
}

#[tauri::command]
fn create_note(
    state: tauri::State<Mutex<AppState>>,
    title: String,
    content: String,
) -> Result<Note, String> {
    let state = state.lock().unwrap();
    let now = now_ms();
    let note = Note {
        id: Uuid::new_v4().to_string(),
        title,
        content,
        created_at: now,
        updated_at: now,
        is_deleted: false,
        vector_clock: None,
    };
    state.db.upsert_note(&note).map_err(|e| e.to_string())?;
    let op = Operation {
        id: Uuid::new_v4().to_string(),
        note_id: note.id.clone(),
        operation_type: "CREATE".to_string(),
        payload: serde_json::to_string(&note).unwrap(),
        vector_clock: None,
        timestamp: now,
        device_id: state.device_id.clone(),
        synced: false,
    };
    state.db.save_operation(&op).map_err(|e| e.to_string())?;
    Ok(note)
}

#[tauri::command]
fn update_note(
    state: tauri::State<Mutex<AppState>>,
    id: String,
    title: String,
    content: String,
) -> Result<Note, String> {
    let state = state.lock().unwrap();
    let now = now_ms();
    let note = Note {
        id,
        title,
        content,
        created_at: now,
        updated_at: now,
        is_deleted: false,
        vector_clock: None,
    };
    state.db.upsert_note(&note).map_err(|e| e.to_string())?;
    let op = Operation {
        id: Uuid::new_v4().to_string(),
        note_id: note.id.clone(),
        operation_type: "UPDATE".to_string(),
        payload: serde_json::to_string(&note).unwrap(),
        vector_clock: None,
        timestamp: now,
        device_id: state.device_id.clone(),
        synced: false,
    };
    state.db.save_operation(&op).map_err(|e| e.to_string())?;
    Ok(note)
}

#[tauri::command]
fn delete_note(
    state: tauri::State<Mutex<AppState>>,
    id: String,
) -> Result<(), String> {
    let state = state.lock().unwrap();
    let now = now_ms();
    let note = Note {
        id: id.clone(),
        title: String::new(),
        content: String::new(),
        created_at: now,
        updated_at: now,
        is_deleted: true,
        vector_clock: None,
    };
    state.db.upsert_note(&note).map_err(|e| e.to_string())?;
    let op = Operation {
        id: Uuid::new_v4().to_string(),
        note_id: id,
        operation_type: "DELETE".to_string(),
        payload: serde_json::to_string(&note).unwrap(),
        vector_clock: None,
        timestamp: now,
        device_id: state.device_id.clone(),
        synced: false,
    };
    state.db.save_operation(&op).map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
async fn sync_notes(state: tauri::State<'_, Mutex<AppState>>) -> Result<String, String> {
    let (device_id, last_sync, unsynced_ops, db, strategy) = {
        let s = state.lock().map_err(|e| e.to_string())?;
        let last_sync: i64 = s.db
            .get_meta("last_sync").unwrap_or(None)
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);
        let unsynced_ops = s.db.get_unsynced_operations().unwrap_or_default();
        (s.device_id.clone(), last_sync, unsynced_ops, s.db.clone(), s.strategy.clone())
    };

    let client = reqwest::Client::new();
    let server = "http://localhost:8000";
    let pushed = unsynced_ops.len();

    if !unsynced_ops.is_empty() {
        client.post(format!("{}/sync/push", server))
            .json(&unsynced_ops)
            .send().await
            .map_err(|e| format!("Push error: {}", e))?;
        for op in &unsynced_ops {
            db.mark_synced(&op.id).map_err(|e| e.to_string())?;
        }
    }

    let resp = client
        .get(format!("{}/sync/pull/{}?since={}", server, device_id, last_sync))
        .send().await
        .map_err(|e| format!("Pull error: {}", e))?;

    let json: serde_json::Value = resp.json().await
        .map_err(|e| format!("Parse error: {}", e))?;

    let remote_ops: Vec<Operation> = serde_json::from_value(
        json["operations"].clone()
    ).unwrap_or_default();

    let server_time = json["server_time"].as_i64().unwrap_or_else(now_ms);
    let pulled = remote_ops.len();

    let merge_start = std::time::Instant::now();
    let mut conflicts = 0u32;

    for op in &remote_ops {
        let remote_note: Note = match serde_json::from_str(&op.payload) {
            Ok(n) => n,
            Err(_) => continue,
        };
        match db.get_note_by_id(&remote_note.id).unwrap_or(None) {
            Some(local_note) => {
                conflicts += 1;
                match strategy.resolve_conflict(&local_note, &remote_note) {
                    sync::Resolution::Remote => { db.upsert_note(&remote_note).ok(); }
                    sync::Resolution::Local => {}
                    sync::Resolution::Merged(m) => { db.upsert_note(&m).ok(); }
                }
            }
            None => { db.upsert_note(&remote_note).ok(); }
        }
    }

    let merge_duration_us = merge_start.elapsed().as_micros() as u64;
    let payload_size: usize = remote_ops.iter().map(|op| op.payload.len()).sum();

    let metrics = sync::SyncMetrics {
        strategy: strategy.name().to_string(),
        scenario: std::env::var("SCENARIO").unwrap_or_else(|_| "manual".to_string()),
        merge_duration_us,
        payload_size_bytes: payload_size,
        conflicts_detected: conflicts,
        operations_lost: 0,
        metadata_size_bytes: 0,
        timestamp: now_ms(),
    };
    sync::record_metrics(&metrics);

    db.set_meta("last_sync", &server_time.to_string())
        .map_err(|e| e.to_string())?;

    Ok(format!("↑ {} | ↓ {} | konflikty: {}", pushed, pulled, conflicts))
}