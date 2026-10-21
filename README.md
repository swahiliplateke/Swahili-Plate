# Swahili Plate

## Run the connected demo locally

The local app uses SQLite and the development-only admin secret `1234`.

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/customer/` for the customer app and `http://127.0.0.1:8000/admin/` for the separate operations app. The root URL redirects to the customer app. On Android, open either URL in Chrome and choose **Install app** or **Add to Home screen** from the browser menu. On iPhone or iPad, open the URL in Safari, tap **Share**, then **Add to Home Screen**. Remote installs require HTTPS; localhost works for local testing. Both apps cache their page shell, while orders, menu updates, and other API data require an internet connection.

The customer app is scoped to `/customer/` and the admin app to `/admin/`, so their service workers do not overlap and they install independently with separate home-screen entries. The `/admin` URL redirects to `/admin/`. The installable web apps target Android Chrome versions commonly used on Android 8 (2018) through current Android releases, and current iOS Safari. These are PWAs, not native App Store or Google Play packages.

The dashboard polls for new orders and needs the development secret above. Local records live in `swahili_plate.db`.

## Deploy on Render

Create a Render **Web Service** from this repository. Set the build command to `pip install -r requirements.txt` and the start command to `python app.py`. The application binds to `0.0.0.0` and reads Render's `PORT` environment variable. Do not use the local development command with `--reload` in production.

Set `APP_ENV=production`, add a managed PostgreSQL database and set `DATABASE_URL` to its internal connection URL, and configure separate strong `ADMIN_TOKEN` and `MASTER_ADMIN_TOKEN` secrets. Set `PUBLIC_BASE_URL` to the deployed HTTPS origin. Configure provider credentials in Render's environment settings, not in committed files. The service fails at startup if production is configured without PostgreSQL or a strong admin token. Verify Render's health check at `/api/health` after deployment.

## Customer accounts

Customers can create an account from the customer header or checkout, verify their email, and sign in to view orders placed while signed in. They can request a password reset by email. Verification and reset links expire after one hour, are stored as hashes, and can be used once. A successful password reset revokes existing sessions. Google accounts are matched by Google's verified email address. Account passwords are stored as PBKDF2-SHA256 hashes, and browser sessions use an HttpOnly, SameSite=Lax cookie that expires after 30 days. Guest checkout and phone-based delivery tracking remain available; customers do not need an account to place an order. Account verification and password reset require working SMTP settings.

To enable Google sign-in, create an OAuth 2.0 Client ID for a **Web application** in Google Cloud Console. Add the deployed site origin as an authorized JavaScript origin and the exact callback URL as an authorized redirect URI. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` in the backend environment (or the ignored local `.env` file). The callback URL is `https://your-domain.example/api/customer/google/callback` in production; for local development, use the exact local host and port, such as `http://127.0.0.1:8000/api/customer/google/callback`. Google requires the configured redirect URI to match exactly. Keep the client secret on the server and out of the browser code and repository. When these values are missing, the Google button explains that setup is incomplete while guest checkout and email account sign-in remain available.

## Master admin and revenue history

Set `MASTER_ADMIN_TOKEN` to a separate random secret of at least 32 characters to enable the master account. The master signs in on the same admin page using that secret and gets a **Total Revenue** tab. Keep `MASTER_ADMIN_TOKEN` different from `ADMIN_TOKEN`; regular admins retain order and delivery access but cannot open the revenue report. Production startup rejects missing or short admin secrets and refuses SQLite so production orders use durable PostgreSQL storage.

Revenue counts orders marked **Completed**, grouped by the order date in Nairobi time for the current and previous five calendar months. Cancelled and unfinished orders are excluded. The app stores the refreshed six-month totals in its existing settings table and rebuilds them from saved order records at startup and after order changes.

## Customer order notifications

Customer email updates are sent by SMTP. To send through a Gmail account, use that account's app password and set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_FROM_EMAIL` in the service environment. Do not put the app password in the HTML or commit it to the repository.

The same SMTP account sends customer email verification and password reset links. Set `PUBLIC_BASE_URL` to the public HTTPS origin (for example `https://your-domain.example`) so those links point to the deployed site. The server reports account email configuration as unavailable when the SMTP values are missing; guest checkout remains available.

WhatsApp updates use a Twilio approved WhatsApp sender and an approved Content Template. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` (for example `whatsapp:+254...`), and `TWILIO_WHATSAPP_CONTENT_SID`. The template must contain these seven variables in order: customer name, order number, status message, rider name, rider phone, branch, and status. Customers choose the WhatsApp updates checkbox at checkout; only opted-in orders are messaged. WhatsApp template sends use Twilio's `ContentSid` and `ContentVariables` fields, as required for business initiated WhatsApp notifications.

Once configured, the service sends an order-received message on checkout and a new update when staff changes the order or delivery status. Assigning a rider moves a delivery to **Out for delivery**, saves the rider's name and phone, and sends the dispatch update automatically. The customer delivery tracker shows the same status and rider details. The admin page shows which notification providers are configured, and can retry the latest customer notification.

## M-Pesa online payments

Checkout supports Daraja STK Push. The customer enters a phone number, submits the order, then approves the amount in the M-Pesa prompt. Payment is marked paid only after the Daraja callback matches the stored order amount and phone number. Duplicate callbacks are safe; an unclear provider timeout remains in review so the customer is not prompted twice automatically. The M-Pesa option remains disabled until all provider settings are present.

Set `MPESA_ENV` to `sandbox` or `production`, then provide `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE`, `MPESA_PASSKEY`, and `MPESA_TRANSACTION_TYPE` (`CustomerPayBillOnline` or `CustomerBuyGoodsOnline`). Set `PUBLIC_BASE_URL` to the public HTTPS site origin and `MPESA_CALLBACK_TOKEN` to a separate random secret of at least 32 characters. Daraja must be able to reach `https://your-domain.example/api/payments/mpesa/callback/<callback-token>`. Test with sandbox credentials and an externally reachable HTTPS callback before switching to production credentials. M-Pesa is an asynchronous service; local browser testing alone cannot confirm a real payment.

For production, set `APP_ENV=production`, use a managed PostgreSQL `DATABASE_URL`, and set separate random `ADMIN_TOKEN` and `MASTER_ADMIN_TOKEN` values. The PostgreSQL driver is included in `requirements.txt`. Configure database backups and test a restore in the hosting provider. Keep SMTP, Twilio, Google, and Daraja secrets in the backend's secret manager, never in the customer HTML or source control.

The customer route serves locally defined CSS and does not load Tailwind from its CDN. FastAPI serves `Customer Site.html` at `/customer/`. This repository cannot verify the production host or its backup schedule.

Menu and branch images are served from the generated `Assets( Swahili plate)/web-ready/` folder. It contains resized copies of the supplied product and branch photos; the original photos remain untouched. The customer site's HEVC featured clip and large story film use H.264/AAC MP4 copies from this folder for broader mobile playback and smaller downloads; the supplied videos remain untouched. Videos load and play only while near the active section, and visitors can enable sound with the on-video button.

## Verify

```powershell
& ".\.venv\Scripts\python.exe" -m unittest discover -s tests -v
```
