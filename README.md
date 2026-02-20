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
