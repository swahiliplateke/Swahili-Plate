# Swahili Plate

## Run the connected demo locally

The local app uses SQLite and the development-only admin secret `1234`.

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for the customer site and `http://127.0.0.1:8000/admin` for the operations dashboard. The dashboard polls for new orders and needs the development secret above. Local records live in `swahili_plate.db`.

## Deploy on Render

1. Push this folder to a Git repository and create a new Render Blueprint from `render.yaml`.
2. Set a unique, long `ADMIN_TOKEN` in the Render service environment. Never use the local development token in production.
3. Set `ORDER_NOTIFICATION_WEBHOOK` to a trusted HTTPS automation/provider endpoint if branch SMS or WhatsApp notifications are required. It receives the branch phone and order payload. Without this, orders still appear in the live admin dashboard but no message is sent to branch phones.
4. Wait for `/api/health` to report `status: ok`, then use the deployed service URL for `/` and `/admin`.

The Render Blueprint provisions PostgreSQL and supplies its connection URL to the service. The order queue, delivery updates, menu, branch settings, and customer reviews are stored centrally, so they are shared across devices. Keep the customer site and admin dashboard on this same origin so the API and browser security controls work as configured.

## Verify

```powershell
& ".\.venv\Scripts\python.exe" -m unittest discover -s tests -v
```