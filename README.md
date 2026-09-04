# Gmail Auto-Unsubscribe

A safe, interactive Gmail unsubscribe tool that:

- Logs in with **Google OAuth 2.0** (no password stored)
- Scans the last **90 days** of email for senders with `List-Unsubscribe` headers
- Asks for **explicit confirmation** before unsubscribing each sender
- Sends the **proper one-click HTTPS POST** (RFC 8058) or mailto unsubscribe
- **Trashes** existing emails from unsubscribed senders (30-day recovery window)
- Auto-skips **sensitive senders** (banks, government, financial) by default
- Supports **dry-run mode** to preview actions without executing them
- Writes an **audit log** of every action

## One-Time Setup

### 1. Create Google Cloud OAuth credentials

1. Visit https://console.cloud.google.com/
2. Create a new project (or select an existing one)
3. **APIs & Services** → **Library** → search "Gmail API" → **Enable**
4. **APIs & Services** → **OAuth consent screen**:
   - User type: **External**
   - App name: anything (e.g. "Gmail Unsub")
   - Scopes: leave default
   - **Test users**: add the Gmail address you want to manage
5. **APIs & Services** → **Credentials** → **Create credentials** → **OAuth client ID**
   - Application type: **Desktop app**
   - Name: anything
6. Click **Download JSON** and save it as `credentials.json` in this project root

### 2. Install dependencies

```bash
cd gmail-unsub
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Run

```bash
python -m gmail_unsub
```

On first run, your browser opens to Google's OAuth screen. Grant the requested permissions. A `token.json` file is created (gitignored) so subsequent runs skip the browser step.

## Usage

```
python -m gmail_unsub [options]

Options:
  --days N           Scan window in days (default: 90)
  --max N            Max senders to show (default: 200)
  --max-emails N     Max emails to fetch (default: 2000)
  --dry-run          Show plan, don't unsubscribe or trash
  --no-trash         Unsubscribe only, don't trash existing
  --config PATH      Use custom config.json
```

### Interactive keys

- `y` — yes, unsubscribe this sender
- `n` — no, skip
- `a` — always (auto-confirm for this sender AND unsubscribe immediately)
- `s` — skip the rest
- `q` — quit

## Configuration

Copy `config.example.json` to `config.json` and edit:

```json
{
  "scan_days": 90,
  "sensitive_keywords": [
    "bank", "chase", "wells", "fargo", "irs", "hmrc", "tax",
    "gov", "account", "statement", "receipt", "invoice",
    "paypal", "venmo", "stripe", "security", "verify", "login", "alert"
  ],
  "always_trust_senders": [],
  "never_trust_senders": []
}
```

`always_trust_senders` is auto-populated when you press `a`.

## Security

- **Scope**: `gmail.modify` (read, send, modify; **cannot permanently delete**)
- **No passwords** are stored; only an OAuth refresh token in `token.json`
- `credentials.json` and `token.json` are gitignored
- Dry-run mode is the default for first-time users
- Sensitive senders are auto-skipped unless you explicitly override

## What gets unsubscribed

Only senders that include a `List-Unsubscribe` header in their messages (the **proper** way to unsubscribe, per RFC 8058). Senders without it (individuals, banks, work) are never touched automatically.

## Audit log

Every run writes `unsub-log-YYYY-MM-DD.json` with:

- Timestamp
- Sender address
- Unsubscribe method
- HTTP status (if HTTPS)
- Number of emails trashed
- Dry-run flag

## Troubleshooting

**"Access blocked: This app's request is invalid"**
- Make sure you added yourself as a test user in the OAuth consent screen.

**"The OAuth client was not found"**
- Re-download `credentials.json` and ensure it's for a **Desktop** application type.

**"Insufficient Permission"**
- Delete `token.json` and re-authenticate. The scope may have changed.

**Unsubscribe fails with 403/404**
- The sender's URL expired or is invalid. The script reports the failure and continues.

## Disclaimer

This tool sends real unsubscribe requests and trashes real emails. While the trash is recoverable for 30 days, always review carefully. Use dry-run first.
