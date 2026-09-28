# AFTERMATH

AFTERMATH records incident observations, investigation actions, outcomes, root causes, and lessons. It compares new incidents with organization-scoped historical records and only makes a historical recommendation when service, environment, error, and corroborating symptom or dependency evidence match.

## Local development

Use Python 3.10+ and Node.js 18+.

1. The checked-in local `backend/.env` enables the explicit loopback-only demo workspace: `AFTERMATH_ENVIRONMENT=development`, `AFTERMATH_LOCAL_DEMO=true`, and `AFTERMATH_DEMO_ORGANIZATION_ID=local-demo`. Local memory is explicitly `AFTERMATH_MEMORY_MODE=local` (SQLite). No token is needed in this mode. To use token authentication locally instead, set `AFTERMATH_LOCAL_DEMO=false` and configure `AFTERMATH_API_TOKENS` as a JSON object mapping a random bearer token of at least 32 characters to an organization ID.
2. Install and start the API:

   ```sh
   cd backend
   python -m venv .venv
   # Activate .venv for your shell, then:
   pip install -r requirements.txt
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

3. Start the frontend in another shell:

   ```sh
   cd frontend
   npm ci
   npm run dev -- --port 5173
   ```

4. Open `http://localhost:5173`. The UI identifies the explicit local path as **DEMO WORKSPACE**. This demo identity is accepted only for loopback requests outside production; production continues to require a valid organization bearer token. `VITE_API_BASE_URL` may point to another API URL.
5. Check `/health/live` and `/health/ready`. The app labels local SQLite memory as **LOCAL DEVELOPMENT**; it does not claim Hindsight is connected. No incident or memory fixtures are seeded automatically.

The default SQLite file is `backend/data/aftermath.sqlite3`; set `AFTERMATH_DATABASE_PATH` to a durable path. Existing JSON files are preserved as legacy data and are **not imported automatically**. This prevents example/test memories from silently entering new recommendations. Back them up before any planned migration.

## Hindsight and Gemini configuration

To use live Hindsight, set `AFTERMATH_MEMORY_MODE=hindsight`, `HINDSIGHT_BASE_URL`, `HINDSIGHT_BANK_ID`, and any required `HINDSIGHT_API_KEY`. The API checks Hindsight connectivity at startup. An unavailable Hindsight service reports `MEMORY_UNAVAILABLE`; it does not silently read or write SQLite instead. Production startup refuses to run without a verified Hindsight connection.

Set `GEMINI_API_KEY` to enable optional structured Gemini inferences. Gemini output is supplemental: every inference is accepted only when each supplied quote exactly occurs in a current or relevant historical evidence source. The server, not the model, determines recommendation state. Without a key, the evidence rules remain active and the runtime endpoint reports that mode.

## Authentication and deployment limits

`AFTERMATH_API_TOKENS` maps bearer tokens to organization IDs. All incident, timeline, and memory access is scoped to that organization. Use a different high-entropy token for each organization and keep the mapping in a secret manager. This is a service-token foundation; it does not provide end-user identities, roles, token rotation, or revocation. Put the API behind an identity-aware gateway until those controls exist.

Set `AFTERMATH_ENVIRONMENT=production`, set `AFTERMATH_LOCAL_DEMO=false`, configure explicit comma-separated `AFTERMATH_CORS_ORIGINS`, a durable database path, API tokens, and live Hindsight. Wildcard CORS is rejected. The demo authentication bypass is disabled in production regardless of its setting. SQLite with WAL is intended for a single API host with a local durable volume; use a managed relational database before running multiple API replicas. Backups, restore drills, TLS termination, monitoring, key rotation, and capacity/load validation remain deployment responsibilities.

## API

Protected endpoints require `Authorization: Bearer <token>`:

- `GET /api/runtime` — organization, memory mode, and reasoning mode
- `GET|POST /api/incidents`, `GET /api/incidents/{id}`
- `POST /api/incidents/{id}/analyze`
- `POST /api/incidents/{id}/actions`
- `GET /api/incidents/{id}/timeline`
- `POST /api/incidents/{id}/resolve`
- `GET /api/memories`

Public probes: `GET /health/live`, `GET /health/ready`.

## Tests

The backend regression suite uses a temporary SQLite file and isolated tenant tokens. It does not import, modify, or seed `backend/data`:

```sh
cd backend
python -m unittest discover -s tests -v
```
