# Handling PHI: what this MVP does and does not do

Everything in the chart is protected health information. The design assumptions
below are deliberate; the gaps are listed honestly so nobody mistakes this for a
production posture.

## What the code already does right

**Nothing leaves the machine by default.** OCR is local (tesseract). Coding is
local (CSV terminology + rules). The LLM extractor is off unless you set
`CA_LLM_ENABLED=1` *and* provide a key.

**The raw MRN is never stored.** `parse_header()` hashes it to a `P-xxxxxxxxxx`
pseudonym. Re-identification stays in your source-of-truth system; this
application only needs to correlate.

**Every change is logged.** `AuditEvent` records ingestion, coding, every code
edit, every finding disposition, and every export, with actor and timestamp.

**Dismissals require a reason.** The API refuses `status: dismissed` without
`dismiss_reason`. A finding that was waved away without explanation is the same
as one that was never raised.

**The browser makes no third-party calls** once you run `make vendor`. Until
then, pdf.js loads from a CDN in development only, and the app tells you.

**The container runs as a non-root user** and stores data on a mounted volume.

## What you must add before real data

### Access control
There is none. No login, no roles, no per-user scoping. Anyone who reaches port
8000 sees every chart. Minimum viable: OIDC/SAML SSO, a `coder` / `auditor` /
`compliance` role split, and encounter-level assignment so coders see their own
queue.

### Encryption
- **At rest**: `storage/` holds the original PDFs in the clear and the SQLite
  file holds full extracted chart text. Use an encrypted volume, or move to
  S3 + SSE-KMS and Postgres with TDE.
- **In transit**: terminate TLS in front of uvicorn. Do not run this on plain
  HTTP outside localhost.

### Network posture
The CORS allow-list in `main.py` is set to localhost. Widen it deliberately, per
origin. Never use a wildcard on a PHI-bearing API.

### Retention
Nothing is ever deleted. Define a retention period, implement a purge job, and
make sure it covers `storage/`, the `documents.text` column, and the audit trail
(which usually has a *longer* required retention than the chart itself).

### Audit-trail integrity
The trail is append-only by convention, not by enforcement — a database admin
can edit it. For it to carry weight in an OIG audit or a False Claims Act
dispute it needs write-once storage or per-record signing.

### Business associate agreements
Any vendor that touches a chart is a business associate: your cloud provider,
your OCR service if you replace tesseract, and absolutely any LLM API. Signed
BAA first, integration second.

### Logging hygiene
`logging` is at INFO and rule exceptions log a stack trace. Audit your log
statements before production — a traceback that includes chart text turns your
log aggregator into an uncontrolled PHI repository.

## The regulatory frame this operates in

This is **decision support**. It proposes and it flags; a certified coder
decides. That distinction matters:

- **Do not auto-submit.** An automated code assignment with no human in the loop
  changes the compliance analysis entirely and is not what this system is built
  for.
- **Coder override must always be available and must never be penalised.** If
  coders feel pressure to accept suggestions, the tool has become a source of
  systematic error rather than a check on it.
- **Watch the drift.** A suggestion engine that consistently proposes higher
  levels is an upcoding pattern, and it will look like one to an auditor
  regardless of intent. Sample your accepted-suggestion rate by code and by
  coder, monthly.
- **The `em_level_above_documentation` rule protects you**, and it should never
  be disabled to reduce noise. If it is too noisy, fix the estimator.

## Measuring before you trust it

Run the system against charts your coders have already finalised, then measure:

- **Suggestion precision and recall**, per code. Aggregate numbers hide the
  codes that matter.
- **Finding precision**, per rule. Any rule dismissed more than ~30% of the time
  is training coders to ignore the panel — retune it or switch it off in
  `rules.yaml`. The dismissal reasons are captured for exactly this analysis.
- **Time per chart**, before and after. If it goes up, the tool is not working
  regardless of how good the accuracy numbers look.
