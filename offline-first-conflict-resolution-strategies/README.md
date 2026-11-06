# Offline-first: strategie rozwiązywania konfliktów

Projekt badawczy porównujący strategie rozwiązywania konfliktów synchronizacji w aplikacjach offline-first. Składa się z trzech części:

- **aplikacja desktopowa** (Tauri 2 + Rust + SQLite): prosty notatnik, który działa offline i synchronizuje się ręcznie z serwerem,
- **serwer synchronizacji** (Python + FastAPI + SQLite): przechowuje log operacji przesłanych przez urządzenia,
- **zestaw eksperymentów** (Python): mierzy czas scalania, wykryte konflikty i utracone operacje w scenariuszach S1–S5.

## Porównywane strategie

| Strategia | Opis | Aplikacja (Rust) | Eksperymenty (Python) |
|---|---|:---:|:---:|
| `LWW` | Last-Writer-Wins: wygrywa wersja z nowszym `updated_at` | ✓ | ✓ |
| `VectorClockLWW` | Zegary wektorowe wykrywają relację *happens-before*; przy operacjach współbieżnych rozstrzyga znacznik czasu | ✓ | ✓ |
| `ORSet` | Observed-Remove Set (add-wins): edycja wygrywa z usunięciem | ✓ (uproszczona) | ✓ |
| `Automerge` | Symulacja CRDT w stylu Automerge: deterministyczny tiebreak po ID aktora, zachowanie obu wersji | — | ✓ (symulacja) |

Implementacja strategii w aplikacji: [src-tauri/src/sync.rs](src-tauri/src/sync.rs). Wariant `Automerge` nie korzysta z biblioteki Automerge, tylko symuluje jej zachowanie w [server/run_all_experiments.py](server/run_all_experiments.py).

## Struktura repozytorium

```
├── index.html, src/            # frontend (Vanilla TS + Vite)
├── src-tauri/                  # backend aplikacji (Rust)
│   └── src/
│       ├── lib.rs              # komendy Tauri: CRUD notatek + sync_notes
│       ├── db.rs               # lokalna baza SQLite (notes, operations, sync_metadata)
│       ├── models.rs           # Note, Operation
│       └── sync.rs             # strategie (trait SyncStrategy) + zapis metryk
├── server/
│   ├── main.py                 # serwer FastAPI
│   ├── run_all_experiments.py  # wszystkie scenariusze S1–S5 dla 4 strategii
│   ├── s1_s2_conflicts.py      # pojedyncze scenariusze (starsze wersje)
│   ├── s3_scalability.py
│   ├── s4_multi_device.py
│   ├── s5_network.py
│   └── test_conflict.py        # ręczny test konfliktu dwóch urządzeń
└── results/                    # wyniki eksperymentów (JSONL)
```

## Wymagania

- [Node.js](https://nodejs.org/) 18+
- [Rust](https://www.rust-lang.org/tools/install) oraz [wymagania systemowe Tauri 2](https://tauri.app/start/prerequisites/)
- Python 3.10+

## Uruchomienie

### 1. Serwer synchronizacji

```bash
cd server
pip install -r requirements.txt
uvicorn main:app --port 8000
```

Serwer nasłuchuje na `http://localhost:8000` (ten adres jest wpisany na sztywno w aplikacji i skryptach).

### 2. Aplikacja desktopowa

```bash
npm install
npm run tauri dev
```

Zmienne środowiskowe:

| Zmienna | Domyślnie | Opis |
|---|---|---|
| `SYNC_STRATEGY` | `LWW` | `LWW`, `VectorClockLWW` lub `ORSet` |
| `NOTES_DB` | `<app_data_dir>/notes.db` | ścieżka do lokalnej bazy; inna wartość pozwala uruchomić kilka „urządzeń” na jednym komputerze |
| `SCENARIO` | `manual` | nazwa scenariusza w zapisanych metrykach |

Przykład: dwa urządzenia z różnymi bazami (PowerShell):

```powershell
$env:NOTES_DB="device-a.db"; $env:SYNC_STRATEGY="VectorClockLWW"; npm run tauri dev
```

Przycisk synchronizacji wysyła niezsynchronizowane operacje (`POST /sync/push`), pobiera operacje innych urządzeń od ostatniej synchronizacji (`GET /sync/pull/{device_id}?since=...`) i scala je wybraną strategią. Metryki każdej synchronizacji trafiają do `src-tauri/results/<strategia>/<scenariusz>.jsonl`.

### 3. Eksperymenty

Przy uruchomionym serwerze:

```bash
cd server
pip install requests numpy
python run_all_experiments.py
```

Skrypt trzeba uruchomić z katalogu `server/`, bo zapisuje wyniki do `../results/`.

## Scenariusze

| ID | Scenariusz | Parametry |
|---|---|---|
| S1 | Podstawowy konflikt edycji: dwa urządzenia edytują tę samą notatkę | 30 powtórzeń |
| S2 | Konflikt delete/update: A usuwa, B edytuje | 30 powtórzeń; mierzone `edit_wins` / `delete_wins` |
| S3 | Skalowalność: scalanie N operacji po okresie offline | N ∈ {10, 50, 100, 500, 1000} |
| S4 | Wiele urządzeń synchronizujących te same notatki | K ∈ {2, 3, 5, 10}, 100 operacji na urządzenie |
| S5 | Niestabilna sieć (symulacja opóźnień i utraty pakietów) | P1: 500 ms; P2: 50 ms + 20% utraty; P3: 1000 ms + 40% utraty |

Każdy wiersz w `results/S*/*.jsonl` to jeden pomiar z polami m.in. `strategy`, `scenario`, `merge_duration_us`, `payload_size_bytes`, `conflicts_detected`, `operations_lost`.

## API serwera

| Metoda | Endpoint | Opis |
|---|---|---|
| `POST` | `/sync/push` | przyjmuje listę operacji (duplikaty po `id` są ignorowane) |
| `GET` | `/sync/pull/{device_id}?since=<ms>` | zwraca operacje innych urządzeń nowsze niż `since` oraz `server_time` |
| `GET` | `/health` | status serwera |
| `DELETE` | `/sync/reset` | czyści wszystkie operacje (używane przez eksperymenty) |

## Ograniczenia

- `ORSetStrategy` w aplikacji przy konflikcie pojedynczej notatki zawsze wybiera wersję zdalną; pełne scalanie zbioru (`ORSetState::merge`) jest zaimplementowane, ale nie jest jeszcze podpięte do synchronizacji.
- Aplikacja nie uzupełnia jeszcze zegarów wektorowych przy tworzeniu i edycji notatek (`vector_clock: None`), więc `VectorClockLWW` w aplikacji w praktyce działa jak LWW. Eksperymenty w Pythonie generują zegary samodzielnie.
- Pole `operations_lost` w metrykach aplikacji jest zawsze `0`; utratę operacji liczą tylko skrypty eksperymentów.
