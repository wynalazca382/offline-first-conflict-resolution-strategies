use rusqlite::{Connection, Result, params, OptionalExtension};
use crate::models::{Note, Operation};
use std::sync::Mutex;

pub struct Database {
    conn: Mutex<Connection>,
}

impl Database {
    pub fn new(db_path: &str) -> Result<Self> {
        let conn = Connection::open(db_path)?;
        conn.execute_batch("
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS notes (
                id           TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                content      TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL,
                is_deleted   INTEGER NOT NULL DEFAULT 0,
                vector_clock TEXT
            );
            CREATE TABLE IF NOT EXISTS operations (
                id              TEXT PRIMARY KEY,
                note_id         TEXT NOT NULL,
                operation_type  TEXT NOT NULL,
                payload         TEXT NOT NULL,
                vector_clock    TEXT,
                timestamp       INTEGER NOT NULL,
                device_id       TEXT NOT NULL,
                synced          INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sync_metadata (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL
            );
        ")?;
        Ok(Self { conn: Mutex::new(conn) })
    }

    pub fn get_notes(&self) -> Result<Vec<Note>> {
    let conn = self.conn.lock().unwrap();
    let mut stmt = conn.prepare(
        "SELECT id, title, content, created_at, updated_at, is_deleted, vector_clock
         FROM notes WHERE is_deleted = 0 ORDER BY updated_at DESC"
    )?;
    let notes = stmt.query_map([], |row| {
        Ok(Note {
            id: row.get(0)?,
            title: row.get(1)?,
            content: row.get(2)?,
            created_at: row.get(3)?,
            updated_at: row.get(4)?,
            is_deleted: row.get::<_, i64>(5)? != 0,
            vector_clock: row.get(6)?,
        })
    })?.collect::<Result<Vec<_>>>()?;
    Ok(notes)
}

    pub fn upsert_note(&self, note: &Note) -> Result<()> {
    let conn = self.conn.lock().unwrap();
    conn.execute(
        "INSERT INTO notes (id, title, content, created_at, updated_at, is_deleted, vector_clock)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
         ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            content = excluded.content,
            updated_at = excluded.updated_at,
            is_deleted = excluded.is_deleted,
            vector_clock = excluded.vector_clock",
        params![
            note.id, note.title, note.content,
            note.created_at, note.updated_at,
            note.is_deleted as i64,
            note.vector_clock
        ],
    )?;
    Ok(())
}

    pub fn save_operation(&self, op: &Operation) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO operations
             (id, note_id, operation_type, payload, vector_clock, timestamp, device_id, synced)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                op.id, op.note_id, op.operation_type, op.payload,
                op.vector_clock, op.timestamp, op.device_id,
                op.synced as i64
            ],
        )?;
        Ok(())
    }

    pub fn get_unsynced_operations(&self) -> Result<Vec<Operation>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, note_id, operation_type, payload, vector_clock,
                    timestamp, device_id, synced
             FROM operations WHERE synced = 0 ORDER BY timestamp ASC"
        )?;
        let ops = stmt.query_map([], |row| {
            Ok(Operation {
                id: row.get(0)?,
                note_id: row.get(1)?,
                operation_type: row.get(2)?,
                payload: row.get(3)?,
                vector_clock: row.get(4)?,
                timestamp: row.get(5)?,
                device_id: row.get(6)?,
                synced: row.get::<_, i64>(7)? != 0,
            })
        })?.collect::<Result<Vec<_>>>()?;
        Ok(ops)
    }

    pub fn mark_synced(&self, operation_id: &str) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "UPDATE operations SET synced = 1 WHERE id = ?1",
            params![operation_id],
        )?;
        Ok(())
    }

    pub fn get_meta(&self, key: &str) -> Result<Option<String>> {
        let conn = self.conn.lock().unwrap();
        let result = conn.query_row(
            "SELECT value FROM sync_metadata WHERE key = ?1",
            params![key],
            |row| row.get(0),
        ).optional()?;
        Ok(result)
    }

    pub fn set_meta(&self, key: &str, value: &str) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO sync_metadata (key, value) VALUES (?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![key, value],
        )?;
        Ok(())
    }
}

pub fn init(db_path: &str) -> Result<()> {
    Database::new(db_path)?;
    Ok(())
}