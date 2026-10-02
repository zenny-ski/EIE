# Email Intelligence Engine (EIE)

Reads inbound emails, classifies them, extracts booking details, drafts replies, and routes each one
to the right system, either automatically or through human review. Every decision is written to an
append-only audit trail. Built to the Cabi by SKIL EIE FRD v1.1.

```
Outlook (Graph API + webhook, IMAP fallback)
        │
        ▼
 Classify + extract + draft reply   (Claude or Gemini; a failure goes to the queue as a system error)
        │     └─ requester check against known booking contacts (FR-007)
        ▼
 Thread check ──► same bucket as the thread's record → appended
              ──► different bucket → Split Event (new linked record, or "possible split" for review)
        │
        ▼
 Confidence routing ──► Feedback     → WhatsApp Feedback Platform
                    ──► Escalation   → Escalation Management System
                    ──► New booking  → IndeCab (after human approval; field tiers enforced)
                    ──► Unclassified → CCP Review Queue (+ notification)
        │
        ▼
 SQLite: emails · review_items · replies · audit_log (append-only)
        │
        ▼
 Web GUI: dashboard KPIs · filters · review queue · replies · audit · roles
```

## Routing rules

| Category              | High (≥85)       | Medium (50–84)          | Low (<50) |
|-----------------------|------------------|-------------------------|-----------|
| Feedback / Escalation | Auto-created     | Pre-filled review form  | CCP queue |
| New booking           | Human approval → IndeCab | Human approval → IndeCab | CCP queue |
| Unclassified          | CCP queue        | CCP queue               | CCP queue |

Thresholds are set per bucket in **Settings** (defaults: `CONFIDENCE_HIGH` / `CONFIDENCE_MEDIUM` and the
`CONFIDENCE_HIGH_FEEDBACK`-style overrides in `.env`). An auto-create that still fails after
three attempts (2s, then 4s backoff) goes to the CCP queue. The rule-based classifier (used when
`USE_LLM=false`) caps its confidence at 70, so its results always get a human look.

**When the AI model fails** (FRD 13.2). A temporary outage (rate limit, overload) retries the email on later
checks for about 8 minutes. After that, or straight away for any other error or an exhausted daily quota, the
email goes to the CCP queue flagged as a **system error**, with no proposed bucket and zero confidence (clearly
different from a low-confidence guess). The keyword rules still read what they can (booking IDs, phone numbers)
to pre-fill the reviewer's form, and the reviewer assigns the bucket. Staff get one alert per distinct error, not
one email per message.

**Email content.** The original HTML body is kept next to the plain text (the email page has a "Show original
formatting" switch; it is shown in a locked-down frame, with scripts, remote images and links off). Text is read
from PDF (text layer only), Word `.docx`, plain text, CSV and HTML attachments, at most five per email and 2 MB
each, and given to the classifier as supporting context. There is no OCR: scanned PDFs and images are listed but
not read. Set `ATTACHMENT_TEXT=false` if attachment contents must not be sent to the AI provider.

**Threads.** Every message is classified on its own. A message in the same bucket as its thread's record is
appended to it; a different bucket is a Split Event that inherits the booking ID and links back to the
parent (both audit trails note it). A split that isn't a clear match goes to the queue as a "possible
split": split to a new record, keep it with the existing one, or attach it to another ticket.

**IndeCab fields** (FRD 10.3). Essential gaps (pickup location, pickup date/time) block the push.
Mandatory gaps (client, cost centre, Concur ID, passenger name and phone, drop location, vehicle class)
need an acknowledged "push with gaps" and are sent marked as missing. Semi-Mandatory (flight or train
number) never blocks. Pickup date and pickup time are separate fields; an email that gives them together
("25 Sept at 7am") fills both. "Request missing details" drafts a reply worded for the most serious tier.
The tiers are edited in **Settings** until they can be read per client group from IndeCab.

## Settings (admins)

The **Settings** page changes, without a restart and with an audit entry for every change: confidence
thresholds (global and per bucket), notification recipients, forward-to-team addresses, the reply signature
and a reply template per bucket, the wording of the missing-details replies per tier, and the IndeCab field
tiers. `.env` only provides the defaults; a saved setting wins, and "Reset to default" removes it. Reply
templates can use `{name}`, `{subject}`, `{booking_id}`, `{summary}` and `{company}`; leave a bucket blank to keep
the AI-written draft. Assigning a bucket to an unclassified email regenerates its draft from that bucket's template.

Dates and periods ("today", the email filters, the KPI cards) use `EIE_TIMEZONE` (default `Asia/Kolkata`).

## Roles

Set `EIE_USERS=name:role:token,...` to require sign-in (the GUI asks for the token once).

| Role | Can do |
|------|--------|
| Super Admin / Admin | everything, including loading booking contacts |
| Module Manager / Executive | queue triage, confirmations, HITL + IndeCab push, replies, forwards, dismiss, fetch |
| Escalation Manager | Escalation confirmations and their emails only; no bookings, no replies |

With `EIE_USERS` empty there is no login and everyone is an admin. Fine for a local demo only.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env    # then fill in credentials
```

Sending replies, forwards and notifications through Graph needs the **Mail.Send** application
permission; without it they fall back to SMTP (`SMTP_*`). `DRY_RUN=true` records everything without
sending or calling downstream systems.

## Usage

```powershell
.\.venv\Scripts\python -m eie gui                  # web GUI at http://127.0.0.1:8000
.\.venv\Scripts\python -m eie demo                 # process samples/sample_emails.json
.\.venv\Scripts\python -m eie run                  # one pass over the mailbox
.\.venv\Scripts\python -m eie poll --interval 60   # keep polling
.\.venv\Scripts\python -m eie emails               # processed emails
.\.venv\Scripts\python -m eie review list
.\.venv\Scripts\python -m eie review approve 3 --by alice [--payload edited.json] [--notes "..."]
.\.venv\Scripts\python -m eie review approve 4 --by alice --category feedback   # route a CCP item
.\.venv\Scripts\python -m eie review claim 4 --by alice
.\.venv\Scripts\python -m eie review dismiss 4 --by alice --reason "spam"
.\.venv\Scripts\python -m eie review forward 4 --by alice --team Invoicing
.\.venv\Scripts\python -m eie contacts contacts.csv   # booking_id,role,name,email,phone
.\.venv\Scripts\python -m eie subscribe --url https://your-public-host   # Graph webhook
.\.venv\Scripts\python -m eie audit [--email-id 3] [--full]
.\.venv\Scripts\python -m pytest -q
```

The webhook endpoint is `/api/graph/webhook`. With `GRAPH_WEBHOOK_SECRET` and an https `PUBLIC_BASE_URL` set,
the GUI creates the Graph subscription itself and renews it before it expires (`python -m eie subscribe --url ...`
does it once by hand). Polling keeps running, so a lapsed subscription loses nothing. If the mailbox
can't be reached, an alert is audited and emailed (at most once per half hour); unread mail stays in the
mailbox and is picked up on the next check.

## Layout

```
eie/
  config.py            settings from .env
  models.py            Email, categories, ClassificationResult, RoutingDecision
  ingestion/           graph.py, imap.py, file.py (demo), FallbackSource
  classification/      llm.py, gemini.py, rules.py (offline mode; reads what it can on a model failure)
  attachments.py       text from PDF / docx / text attachments for the classifier
  routing/             router.py (confidence rules), destinations.py (payloads, field tiers, retry)
  pipeline.py          read → classify → thread check → route → dispatch/queue → notify → audit
  review.py            approve / reject / claim / dismiss / forward / possible-split resolutions
  reply.py             Mailer (Graph / SMTP) and ReplyService
  notify.py            queue and ingestion-failure notifications
  verification.py      requester check against booking contacts
  templates.py         reply templates and drafts (per bucket, per missing-field tier)
  adminconfig.py       admin-editable settings: validation, persistence, audit
  subscription.py      creates and renews the Graph webhook subscription
  auth.py              roles and tokens
  storage.py           SQLite schema, review queue, KPIs, audit log
  web/                 FastAPI API + single-page GUI
  app.py               wiring
  cli.py
```

## Not built yet

- Real API contracts for the WhatsApp Feedback Platform, Escalation system and IndeCab (currently a
  generic JSON POST). Appending a thread message to an existing downstream record, and writing the
  confirming user into the IndeCab booking (FR-025), need those APIs.
- Reading requester data and per-client-group field tiers from IndeCab (for now: the `booking_contacts`
  table and the defaults in `INDECAB_TIERS`).
- Reply templates and field tiers are global; the FRD asks for them per client group, which needs client groups
  from IndeCab.
- PII masking, and the CCP integration (the GUI is standalone for now).
- The FRD defers a profanity check on replies and a correction path for wrongly confirmed records.
#   E I E  
 