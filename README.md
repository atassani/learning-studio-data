# Learning Studio Data

Data files used by Learning Studio, so they can be updated and maintained independently from the app code.

## Structure
- `data/areas.json`
- `data/questions-fdl.json`
- `data/questions-ipc.json`
- `data/questions-logica1.json`
- `test-data/areas-mcq-tests.json`
- `test-data/questions-mcq-tests.json`
- `test-data/questions-logica1.json`
- `test-data/questions-ipc.json`
- `test-data/questions-fdl.json`

## Versioning
Each JSON file includes:
- `schemaVersion` (integer): bump only when the JSON shape changes.
- `updatedAt` (ISO date): bump when content changes.

## Notes
- Data is static and public.
- Each questions file includes metadata (`area`, `type`, `shortName`) and a `questions` array.
  - Each item in `questions` includes:
    - `section` (string)
    - `number` (integer)
    - `question` (string)
    - `options` (array of strings)
    - `answer` (string)
    - `explanation` (string)

## Deploy
- Script: `scripts/deploy-data.sh`
- What it does:
  - syncs `data/` to `s3://studio-data.humblyproud.com/`
  - creates a CloudFront invalidation only for `/studio-data/*`

## Local Development
- Install dependencies:
  - `npm install`
- Serve production-like data:
  - `npm run dev:data`
  - URL: `http://localhost:4173`
- Serve stable test data:
  - `npm run dev:data:test`
  - URL: `http://localhost:4174`

## Data Audit
- Validate language wiring and translation coverage:
  - `npm run audit:lang`
- Fail when translation coverage is below threshold:
  - `npm run audit:lang:strict`

## Python Scripts
- Folder: `scripts/`
- Python scripts are managed as a standalone project with `scripts/pyproject.toml`.
- Install with dependency groups so future scripts can add their own requirements without conflicts.

### Translate JSON (`scripts/translate_json.py`)
- Purpose: translate JSON string values while preserving JSON structure.
- HTML in translated strings is preserved, so fields like `explanation` can safely contain tags such as `<ol>`, `<li>`, `<p>`, and `<br>`.

Setup:
1. `cd scripts`
2. `python3 -m venv .venv`
3. `source .venv/bin/activate`
4. `pip install -e ".[translate]"`

API key (no manual export required):
1. Create `scripts/.env.translate` (gitignored) with your key:
   - `DEEPL_API_KEY="<your-key-here>"`
2. The script auto-loads, in this order:
   - `scripts/.env.translate`
   - `scripts/.env`
3. Optional override:
   - `--env-file /absolute/or/relative/path/to/envfile`

Run:
- `python translate_json.py --in ../data/questions-ipc.json --out ../data/questions-ipc-en.json --source es --target en`
- `python translate_json.py --in ../data/questions-ipc.json --out ../data/questions-ipc-ca.json --source es --target ca`

Optional installed entrypoint (after `pip install -e ".[translate]"`):
- `translate-json --in ../data/questions-ipc.json --out ../data/questions-ipc-en.json --source es --target en`

### Lógica II PDF Import (`scripts/preguntas_logica2.py`)
- Purpose: convert `PREGUNTAS TEST DF LÓGICA II AMPLIADO.pdf` into `data/questions-log2.json`.
- Requires: `pdftotext` available in `PATH`.
- Output shape: same metadata structure as the other questions JSON files, with `explanation` stored as HTML so lists and paragraph breaks from the PDF can be preserved.

Run with default paths:
1. `cd /Users/toni.tassani/code/humblyproud-multiproject/learning-studio-data`
2. `python3 scripts/preguntas_logica2.py`

Run with explicit paths:
1. `python3 scripts/preguntas_logica2.py --pdf "/Users/toni.tassani/code/humblyproud-multiproject/learning-studio-data/PREGUNTAS TEST DF LÓGICA II AMPLIADO.pdf" --output "/Users/toni.tassani/code/humblyproud-multiproject/learning-studio-data/data/questions-log2.json"`

Notes:
- The script normalizes logic notation to consistent symbols such as `∀`, `∃`, `→`, `∧`, and `∨`.
- If a question has conflicting or missing answers, the script prefixes the question text with `ANOMALY` for manual review.
