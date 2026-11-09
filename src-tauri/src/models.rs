use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Note {
    pub id: String,
    pub title: String,
    pub content: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub is_deleted: bool,
    pub vector_clock: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Operation {
    pub id: String,
    pub note_id: String,
    pub operation_type: String,
    pub payload: String,
    pub vector_clock: Option<String>,
    pub timestamp: i64,
    pub device_id: String,
    #[serde(default)]
    pub synced: bool,
}