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
│   ├── plot_results.py         # wykresy + summary.csv z wyników
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

### 3. Eksperymenty i wykresy

Potrzebne są dwa terminale, oba w katalogu `server/`. Na Windows zamiast `python` można użyć `py`.

**Terminal 1: serwer** (zostaw uruchomiony):

```bash
cd server
pip install -r requirements.txt
python -m uvicorn main:app --port 8000
```

**Terminal 2: eksperymenty, potem wykresy:**

```bash
cd server
python run_all_experiments.py      # ok. 10 min, z czego najdłużej trwa S5 (symulowane opóźnienia)
python plot_results.py             # wykresy + tabela → results/plots/
```

Nadpisywane są pliki `results/S*/s*_results.jsonl`. Żeby zachować poprzedni przebieg, użyj `--out` z innym katalogiem i wtedy `plot_results.py --results <ten katalog>`. Szybki test bez czekania: `python run_all_experiments.py --repeats 3 --out ../results_test`.

`plot_results.py` tworzy w `results/plots/`:

| Plik | Zawartość |
|---|---|
| `s1_utrata.png` | S1: odsetek powtórzeń z utraconą edycją, oba warianty |
| `s2_edit_wins.png` | S2: jak często edycja wygrywa z usunięciem |
| `s3_czas.png`, `s3_utrata.png` | S3: czas scalania (mediana i rozstęp międzykwartylowy, skala log) oraz utracone operacje w zależności od N |
| `s4_czas.png`, `s4_utrata.png` | S4: to samo w zależności od liczby urządzeń K |
| `s5_siec.png` | S5: skuteczność i czas synchronizacji dla profili sieci |
| `summary.csv` | tabela wszystkich liczb z wykresów (separator `;`, otwiera się w Excelu) |

`--format pdf` lub `--format svg` zapisuje wykresy jako grafikę wektorową, co jest lepsze do pracy w LaTeX/Wordzie.

Opcje `run_all_experiments.py`: `--repeats N` (domyślnie 30), `--scenarios S1,S3` (wybrane scenariusze), `--out <katalog>` (domyślnie `results/` w katalogu głównym repozytorium). Serwer jest potrzebny tylko w S3 i S5. Każde powtórzenie ma stałe ziarno losowania, a wszystkie strategie dostają w nim identyczne dane wejściowe.

Skrypty `s1_s2_conflicts.py`, `s3_scalability.py`, `s4_multi_device.py` i `s5_network.py` to starsze wersje z poprzednią, niespójną metodą liczenia utraty. Nie należy ich używać do wyników.

## Scenariusze

| ID | Scenariusz | Parametry |
|---|---|---|
| S1 | Konflikt edycji. `concurrent`: A i B niezależnie edytują tę samą notatkę. `causal_skew`: B edytuje po pobraniu wersji A, ale jego zegar systemowy spóźnia się o 5 s | 30 powtórzeń × 2 warianty |
| S2 | Konflikt delete/update: A usuwa, B współbieżnie edytuje; losowo, które zdarzenie ma późniejszy znacznik czasu | 30 powtórzeń; mierzone `edit_wins` / `delete_wins` |
| S3 | Skalowalność: A i B wykonują offline po N edycji na wspólnej puli N/10 notatek, potem A scala edycje B pobrane z serwera | N ∈ {10, 50, 100, 500, 1000} |
| S4 | K urządzeń po 100 edycji (50% na 10 wspólnych notatkach, 50% na prywatnych), operacje docierają w losowej kolejności | K ∈ {2, 3, 5, 10} |
| S5 | Synchronizacja przez niestabilną sieć (symulacja po stronie klienta); jeden przebieg sieci na powtórzenie, scalany każdą strategią | P1: 500 ms; P2: 50 ms + 20% utraty; P3: 1000 ms + 40% utraty |

### Metryki

Konflikty i utratę liczy wspólny mechanizm (`Replica` w [run_all_experiments.py](server/run_all_experiments.py)) na podstawie prawdziwej historii przyczynowej operacji, zapisanej w zegarach wektorowych. Funkcje strategii zwracają tylko wynik scalenia, więc każda strategia jest oceniana tak samo.

- `conflicts_detected`: liczba przychodzących operacji współbieżnych z co najmniej jedną aktualną wersją notatki.
- `operations_lost`: liczba operacji, których efektu nie ma w stanie końcowym, chociaż żadna późniejsza operacja ich nie nadpisała. Edycja jest zachowana, jeśli jej treść jest w wyniku (`content` lub `_conflicts`). Usunięcie jest zachowane, jeśli notatka jest usunięta. W S2 usunięcie i edycja wykluczają się, więc każda strategia traci dokładnie jedną operację; tu porównuje się `edit_wins`.
- `merge_duration_us`: czas samych wywołań funkcji scalającej. W S1 i S2 to średnia z 1000 wywołań, bo pojedyncze trwa około 1 µs.

Każdy wiersz w `results/S*/*.jsonl` to jeden pomiar z polami m.in. `strategy`, `scenario`, `merge_duration_us`, `payload_size_bytes`, `conflicts_detected`, `operations_lost`; S5 dodatkowo ma `sync_success` i `sync_duration_ms`. Katalog `results/S1_S2/` i plik `results/S3/s3_results_v2.jsonl` pochodzą ze starszych skryptów.

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
