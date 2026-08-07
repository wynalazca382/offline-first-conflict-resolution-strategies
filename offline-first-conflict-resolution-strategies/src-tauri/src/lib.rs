use tauri::Manager;
use std::sync::{Arc, Mutex};
use uuid::Uuid;

mod db;
mod sync;
mod models;

use db::Database;
use models::Note;
use sync::{LWWStrategy, SyncStrategy, now_ms};

// Stan aplikacji współdzielony między komendami
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
            let db_path = app_dir.join("notes.db");

            let db = Arc::new(
                Database::new(db_path.to_str().unwrap())
                    .expect("Błąd inicjalizacji bazy")
            );

            // Pobierz lub wygeneruj device_id
            let device_id = db.get_meta("device_id")
                .unwrap_or(None)
                .unwrap_or_else(|| {
                    let id = Uuid::new_v4().to_string();
                    db.set_meta("device_id", &id).ok();
                    id
                });

            let state = AppState {
                db,
                strategy: Arc::new(LWWStrategy),
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
        id,
        title: String::new(),
        content: String::new(),
        created_at: now,
        updated_at: now,
        is_deleted: true,
        vector_clock: None,
    };
    state.db.upsert_note(&note).map_err(|e| e.to_string())
}