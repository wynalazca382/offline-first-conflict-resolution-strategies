import requests
import json
import time

SERVER = "http://localhost:8000"
NOTE_ID = "test-note-001"
NOW = int(time.time() * 1000)

# Urządzenie A tworzy notatkę
requests.post(f"{SERVER}/sync/push", json=[{
    "id": f"op-a-1",
    "note_id": NOTE_ID,
    "operation_type": "CREATE",
    "payload": json.dumps({
        "id": NOTE_ID,
        "title": "Wersja A",
        "content": "Edytowane przez urządzenie A",
        "created_at": NOW,
        "updated_at": NOW,
        "is_deleted": False,
        "vector_clock": None
    }),
    "vector_clock": None,
    "timestamp": NOW,
    "device_id": "device-a"
}])

# Urządzenie B edytuje tę samą notatkę z późniejszym znacznikiem
time.sleep(0.1)
NOW_B = int(time.time() * 1000)

requests.post(f"{SERVER}/sync/push", json=[{
    "id": f"op-b-1",
    "note_id": NOTE_ID,
    "operation_type": "UPDATE",
    "payload": json.dumps({
        "id": NOTE_ID,
        "title": "Wersja B",
        "content": "Edytowane przez urządzenie B",
        "created_at": NOW,
        "updated_at": NOW_B,
        "is_deleted": False,
        "vector_clock": None
    }),
    "vector_clock": None,
    "timestamp": NOW_B,
    "device_id": "device-b"
}])

print("Operacje wysłane na serwer.")
print(f"Timestamp A: {NOW}")
print(f"Timestamp B: {NOW_B}")
print(f"B jest nowszy o: {NOW_B - NOW}ms")
print()
print("Teraz kliknij Synchronizuj w aplikacji.")
print("Strategia LWW powinna wybrać: Wersja B (nowszy timestamp)")