# BaezLabs Financial Agent — Automated Credit Card Management with Claude API

An automated agent that reads your Mexican bank statement emails (PDF attachments and HTML bodies), extracts structured financial data using Claude AI, syncs it to Notion, and creates Google Calendar payment reminders — all with zero manual data entry.

Built for personal finance automation in the Mexican banking ecosystem, it handles multiple banks and cards, deduplicates records across runs, and cross-references payments against statement balances.

## Architecture

```
Gmail (bank statement emails)
        │
        ▼
  PDF / HTML extraction
        │
        ▼
  Claude API (claude-sonnet-4-6)
  ┌─────────────────────────────┐
  │  Structured JSON extraction │
  │  · Balance / Debt / Min pay │
  │  · Cut date / Due date      │
  │  · Payment receipts         │
  └─────────────────────────────┘
        │
        ├──────────────────────▶ Notion
        │                        · Cards database (balances, due dates)
        │                        · Payments database (payment history)
        │
        └──────────────────────▶ Google Calendar
                                 · Alert event (N days before due)
                                 · Due-date event (day of)
```

## Features

- **Multi-bank support** — Scotiabank (password-protected PDF), Liverpool, Banregio, Hey Banco, Amex
- **Dual extraction modes** — PDF attachments (with RFC-based password unlocking) and HTML email body parsing
- **Claude-powered parsing** — structured JSON extraction tolerant of varying statement formats
- **Notion sync** — updates balance, total debt, minimum payment, credit limit, cut date, and due date per card
- **Calendar reminders** — creates two events per card: an advance alert and a due-date event; deduplicates automatically
- **Payment tracking** — reads payment receipt emails and logs them to a separate Notion database
- **3-layer deduplication**:
  1. Gmail query deduplication (seen message IDs within a run)
  2. In-memory deduplication (same card + date within a run)
  3. Notion query deduplication (persists across runs — checks before inserting)
- **Balance cross-reference** — after importing statements and payments, adjusts `Balance Actual` in Notion to reflect post-cut payments

## Prerequisites

- Python 3.11+
- A Google Cloud project with Gmail API and Google Calendar API enabled
- OAuth 2.0 credentials (`credentials.json`) downloaded from Google Cloud Console
- An Anthropic API key
- A Notion integration token with access to your cards and payments databases
- Bank statements delivered to Gmail (automatic for most Mexican banks)

## Setup

**1. Enable Google APIs and download credentials**

In [Google Cloud Console](https://console.cloud.google.com/):
- Enable **Gmail API** and **Google Calendar API**
- Create OAuth 2.0 credentials (Desktop app)
- Download as `credentials.json` and place it in the project directory

**2. Configure environment variables**

```bash
cp .env.example .env
# Edit .env with your actual values
```

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `NOTION_TOKEN` | Notion integration secret (`secret_...`) |
| `NOTION_CARDS_DB` | Notion database ID for credit cards |
| `NOTION_PAYMENTS_DB` | Notion database ID for payment records |
| `RFC_TITULAR` | Your RFC (used as PDF password for Scotiabank) |
| `GOOGLE_CALENDAR_ID` | Calendar ID, usually `primary` |

To find a Notion database ID: open the database in Notion, copy the URL, and extract the 32-character hex string before the `?`.

**3. Install dependencies**

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**4. Authenticate with Google**

```bash
python financial_agent.py --mode statements
```

On first run, a browser window opens for OAuth consent. After approval, `token.json` is saved and reused automatically on subsequent runs.

**5. Configure your banks**

Edit `BANK_CONFIG` in `financial_agent.py` to match your cards:

```python
"Scotiabank": {
    "last_4_map": {
        "1234": "Scotia Gold",   # last 4 digits → display name
        "5678": "Scotia Platinum",
    },
},
```

## Usage

```bash
# Process bank statements → update Notion + create Calendar reminders
python financial_agent.py --mode statements

# Process payment receipt emails → log to Notion payments DB
python financial_agent.py --mode payments

# Run everything: statements + payments + cross-reference
python financial_agent.py --mode all
```

`--mode all` (default) runs all three stages in sequence: imports statements, imports payment receipts, then cross-references payments against statement balances and updates `Balance Actual` in Notion.

## Bank Configuration

The `BANK_CONFIG` dictionary defines how each bank is handled. Key fields:

| Field | Description |
|---|---|
| `source` | `"pdf"` for PDF attachments, `"email_body"` for HTML/text email content |
| `gmail_query` | Gmail search query to locate statement emails |
| `pdf_password` | Password for encrypted PDFs (typically your RFC) |
| `last_4_map` | Dictionary mapping last-4-digit strings to human-readable card names |
| `skip_if_no_email` | If `true`, warns but does not fail when no email is found |

To add a new bank, add an entry to `BANK_CONFIG` and, if it sends payment receipt emails, add a corresponding entry to `PAYMENT_SOURCES`.

## Supported Statement Formats

| Bank | Format | Notes |
|---|---|---|
| Scotiabank | Password-protected PDF | Password = RFC (tries uppercase, lowercase, truncated variants) |
| Liverpool | HTML email body | Filters navigation and promotional content |
| Banregio | HTML/plain text email body | |
| Hey Banco | HTML email body | |
| Amex | Unprotected PDF | Download from Amex app → forward to yourself with subject "Amex estado cuenta" |

For any bank sending HTML emails, the agent prefers plain-text parts when available and falls back to BeautifulSoup HTML cleaning.

## Deduplication (3 Layers)

1. **Gmail level** — tracks message IDs within a run; a bank with multiple matching queries won't process the same email twice.
2. **In-memory level** — tracks `(last4, fecha)` tuples during a run; prevents duplicate entries if the same payment appears in multiple emails.
3. **Notion level** — before creating a payment page, queries Notion for an existing entry with the same card and date; skips creation if found. This makes the agent safe to run multiple times daily.

## Crontab Example

```cron
# Run every day at 7:00 AM
0 7 * * * cd /path/to/financial_agent && source venv/bin/activate && python financial_agent.py --mode all >> logs/run.log 2>&1
```

## Tech Stack

| Component | Library / Service |
|---|---|
| AI extraction | [Anthropic Claude API](https://docs.anthropic.com) (`claude-sonnet-4-6`) |
| Email / Calendar | Google APIs (`google-api-python-client`, `google-auth-oauthlib`) |
| Database | [Notion API](https://developers.notion.com) (`notion-client`) |
| PDF handling | `pikepdf` (decrypt) + `pypdf` (text extraction) |
| HTML parsing | `beautifulsoup4` + `lxml` |

## License

MIT — see [LICENSE](LICENSE).

---

Built by [BaezLabs](https://baezlabs.com)
