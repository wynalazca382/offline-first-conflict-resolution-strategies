from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import time
import os

app = FastAPI(title="Sync Server")
DB_PATH = "server.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operations (
            id              TEXT PRIMARY KEY,
            note_id         TEXT NOT NULL,
            operation_type  TEXT NOT NULL,
            payload         TEXT NOT NULL,
            vector_clock    TEXT,
            timestamp       INTEGER NOT NULL,
            device_id       TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

class Operation(BaseModel):
    id: str
    note_id: str
    operation_type: str
    payload: str
    vector_clock: Optional[str] = None
    timestamp: int
    device_id: str

@app.post("/sync/push")
def push_operations(operations: List[Operation]):
    conn = get_db()
    for op in operations:
        conn.execute("""
            INSERT OR IGNORE INTO operations
            (id, note_id, operation_type, payload, vector_clock, timestamp, device_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (op.id, op.note_id, op.operation_type,
              op.payload, op.vector_clock, op.timestamp, op.device_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "received": len(operations)}

@app.get("/sync/pull/{device_id}")
def pull_operations(device_id: str, since: int = 0):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM operations
        WHERE device_id != ? AND timestamp > ?
        ORDER BY timestamp ASC
    """, (device_id, since)).fetchall()
    conn.close()
    return {
        "operations": [dict(r) for r in rows],
        "server_time": int(time.time() * 1000)
    }

@app.get("/health")
def health():
    return {"status": "ok", "time": int(time.time() * 1000)}