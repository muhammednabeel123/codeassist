# CodeAssist

A working MVP of the system you described: **a coder scans a chart, the app reads
it, proposes codes with evidence, then audits the claim for problems before it
goes out.**

Browser-based, runs locally, no cloud dependency. Handles both born-digital PDFs
and scanned/faxed paper — it decides per page and OCRs only what needs it.

```
make install          # python deps (see the note about tesseract + poppler)
make samples          # generate three synthetic charts to play with
make run              # http://127.0.0.1:8000
make test             # 58 tests
```

Then drag `samples/01_office_visit_digital.pdf` onto the drop zone.

---

## The shape of the thing

```
                    ┌──────────────┐
   scanned chart ──▶│  INGESTION   │  per-page: text layer? → use it
        (PDF)       │              │           no text layer? → OCR it
                    └──────┬───────┘  keeps page + char offsets for every word
                           │
                    ┌──────▼───────┐
                    │ SEGMENTATION │  chief complaint / HPI / PMH / family hx /
                    │              │  assessment / plan / procedures …
                    └──────┬───────┘  each section carries a credibility weight
                           │
                    ┌──────▼───────┐
                    │  ASSERTION   │  present · negated · uncertain ·
                    │  DETECTION   │  historical · hypothetical · not-the-patient
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │  SUGGESTION  │  ICD-10-CM candidates, each with a
                    │              │  quote from the chart
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
      coder edits ─▶│    AUDIT     │  24 rules: bundling, modifiers, necessity,
        the codes   │    ENGINE    │  specificity, demographics, units, CDI gaps
                    └──────┬───────┘  re-runs on every edit
                           │
                    ┌──────▼───────┐
                    │ DX EXPORT    │  blocked while any blocker is open
                    └──────────────┘
```

**Scope: diagnoses only.** This deployment codes ICD-10-CM and nothing else —
see `SUPPORTED_CODE_SYSTEMS` in `backend/app/config.py`. Procedure coding
(CPT/HCPCS) and the E/M level estimate are switched off: nothing in the pipeline
proposes one, the LLM extractor is told to return diagnoses only and drops
anything else, and the API refuses a hand-entered code from any other system.
The export is therefore a diagnosis list with claim pointers, not a complete
claim — whatever billing system consumes it supplies its own service lines.

The procedure-side audit rules (bundling, MUE, modifier 25/50, necessity, E/M
level) stay registered and enabled even so. They are silent with no procedure
lines on the claim, but a legacy or imported CPT line still gets checked rather
than waved through.

The two halves you asked for are deliberately separate. **Suggestion** answers
"what codes does this chart support?" **Audit** answers "what is wrong with the
claim as it now stands?" — and it runs against whatever codes are on the claim,
whether the engine proposed them or a human typed them. That means the audit is
just as useful to a shop that already codes by hand.

## What the coder sees

Two panes. Left is the chart. Right is the work.

- **Codes tab** — every suggestion with a confidence score and, underneath it,
  the exact sentence it came from. Click the quote and the left pane highlights
  it. Accept, reject, change units, add modifiers, or search the terminology to
  add a code by hand.
- **Audit tab** — findings grouped by severity, each with what's wrong, what to
  do about it, and the rule citation. Dismissing one requires a written reason,
  which lands in the audit trail.
- **Claim tab** — the assembled claim with diagnosis pointers. Export is
  **refused** while any blocking finding is open.

The left pane defaults to the extracted text rather than the rendered PDF,
because highlighting works identically there for scanned and digital pages. The
original PDF is one click away.

## Why the suggestions are trustworthy enough to use

Autocoding fails in production for three predictable reasons. Each has a
countermeasure here:

**It codes things the note denies.** "Denies chest pain" becomes R07.9; "mother
had breast cancer" becomes the patient's cancer. `coding/context.py` implements
NegEx/ConText-style assertion detection with scope termination, so a mention is
labelled *negated*, *uncertain*, *historical*, *hypothetical* or *not-the-patient*
before it can become a code. A code whose only support is one of those is a
**blocker**, not a suggestion.

**It ignores where in the note something appears.** A diagnosis under Past
Medical History is not necessarily this encounter's diagnosis. Every section
carries a weight (`sections.py`), and family history is hard-coded as never
codable.

**You can't tell why it did anything.** Every suggestion stores its evidence
spans — page, character range, quote, and the reason. No evidence, no code.

## Why the audit is the more valuable half

Suggestion saves keystrokes. The audit is where the money and the compliance
exposure live. The 24 rules in `rules/rules.yaml` cover:

| Category | Examples |
|---|---|
| Documentation | unsupported code, negated-only evidence, missing signature/attestation |
| Bundling | NCCI PTP conflicts, and separately, edits where a modifier override is permitted |
| Modifiers | E/M + same-day minor procedure needs 25; bilateral service under-reported |
| Specificity | unspecified code used, more-specific code available, laterality documented but not coded |
| Medical necessity | procedure with no diagnosis on the claim that supports it; unlinked service lines |
| Demographics | sex- and age-restricted codes vs the chart |
| Units | MUE exceeded, duplicate lines without distinguishing modifiers |
| E/M level | coded level above (or below) what the documented MDM supports |
| Sequencing | screening Z code not first-listed |
| CDI | documentation too vague for a specific code → drafts a **non-leading** provider query |
| Data quality | low-confidence OCR page warning |

The procedure-facing rows above (bundling, modifiers, necessity, units, E/M
level) only have something to say when a procedure line is on the claim, which
in a diagnosis-only deployment means an imported or legacy one. They are kept
enabled so that case is still caught.

Two design choices worth calling out:

- **Severity and thresholds live in YAML, not code.** Payer policy changes
  mid-quarter; compliance staff should be able to retune a rule or switch it off
  without a deploy.
- **A rule that throws does not abort the audit.** It emits a "could not be
  evaluated — review manually" finding instead. Silent partial audits are how
  bad claims get out.

Every rule carries a citation (ICD-10-CM guideline section, NCCI manual, CPT
Appendix A, CMS signature requirements) so a coder can check the system's work
rather than defer to it.

## The under-coding half

Auditing only for over-coding produces a tool that costs the practice money.
Rules that look the other way: long-term drug use codes (Z79.-) missing when the
med list shows them, and BMI (Z68.-) missing alongside an obesity diagnosis.
Both are `info` severity — they prompt a look, they don't nag.

Two more under-coding rules exist but are inert here, because they are about
procedures: E/M level *below* what's documented (silent without an E/M line) and
`billable_service_not_captured`, which is switched off in `rules.yaml` since its
only output is "add this CPT line".

## Provider queries

When documentation is too vague for a specific code, the finding offers to draft
a query. `audit/cdi.py` builds it mechanically to be compliant: it lists the
clinical indicators actually found in the record, offers the full set of
plausible answers, and **always** includes "clinically undetermined", "other",
and "not present". Omitting those options is what makes a query leading, and it
is the single most common way CDI programmes get themselves in trouble.

## Reference data and the CPT licensing problem

`backend/reference/*.csv` ships a small **demo subset** with paraphrased
descriptors:

| File | What it is |
|---|---|
| `icd10cm.csv` | diagnosis codes, keywords, sex/age constraints, unspecified flag |
| `cpt.csv`, `hcpcs.csv` | procedures, global days, MUE, E/M level mapping |
| `ncci_ptp.csv` | procedure-to-procedure bundling edits |
| `necessity.csv` | procedure → acceptable diagnosis prefixes (LCD/NCD style) |
| `specificity.csv` | unspecified code → better code, and the trigger phrases |

Only `icd10cm.csv` drives suggestion. The procedure tables are still loaded, but
solely so the audit rules can evaluate a procedure line that arrives from
somewhere else; nothing reads them to propose a code.

**ICD-10-CM is published free by CMS/NCHS. CPT is copyrighted by the AMA and
HCPCS Level II descriptors have their own terms.** You cannot ship real CPT
descriptors without a licence. The loader (`coding/terminology.py`) reads plain
CSV precisely so your organisation can drop its licensed quarterly release into
`reference/` and change nothing else. Code sets update on a schedule — ICD-10-CM
1 October, CPT 1 January, NCCI quarterly — and a coding tool running last year's
tables is worse than no tool.

The demo tables cover roughly 80 diagnoses and 60 procedures. That is enough to
demonstrate every rule and nowhere near enough for production.

## Optional LLM extractor

Off by default (`CA_LLM_ENABLED=0`). When enabled, an LLM reads the note in
parallel with the dictionary matcher and the two results merge. It has better
recall on prose that doesn't use textbook phrasing. Three guardrails make it
usable:

1. **Closed vocabulary** — any code the model returns that isn't in the loaded
   ICD-10-CM table is dropped and reported. Models invent well-formed, plausible,
   nonexistent codes with total confidence.
2. **Verified evidence** — the model must return the exact supporting substring.
   We search for it in the document; if it isn't there, the finding is discarded
   as a hallucination.
3. **Diagnoses only** — the prompt asks for ICD-10-CM and the parser accepts
   nothing else. An instruction in a prompt is not an enforcement mechanism.

Sending charts to a third-party API needs a signed BAA. That is a decision for
your compliance officer, not a config flag someone flips.

## API

| | |
|---|---|
| `POST /api/documents` | upload a PDF → extract, code, audit |
| `GET /api/encounters` | worklist, sorted by risk |
| `GET /api/encounters/{id}` | full detail: codes, findings, evidence |
| `POST /api/encounters/{id}/codes` | add a diagnosis by hand; 400 for any other code system |
| `PATCH /api/codes/{id}` | accept / reject / units / modifiers → triggers re-audit |
| `PATCH /api/findings/{id}` | resolve or dismiss (reason required) |
| `POST /api/findings/{id}/query` | draft a provider query |
| `GET /api/encounters/{id}/export` | diagnoses as JSON or CSV; 409 while blockers are open |
| `GET /api/encounters/{id}/trail` | append-only audit trail |
| `GET /api/codes/search?q=` | ICD-10-CM lookup |
| `GET /api/rules` | the active rule set and its citations |

Interactive docs at `/docs`.

## Layout

```
backend/app/
  pipeline/extract.py    per-page text-layer probe + OCR fallback, offsets
  pipeline/sections.py   clinical section segmentation, demographics parsing
  coding/context.py      assertion detection (negation, family hx, uncertainty)
  coding/terminology.py  CSV loaders for all reference tables
  coding/suggest.py      ICD-10-CM candidate generation and diagnosis ranking
  coding/llm.py          optional LLM extractor with hallucination guards
  audit/engine.py        rule framework, context, YAML config
  audit/rules.py         the 24 rules
  audit/cdi.py           compliant provider query drafting
  service.py             ingest → code → audit orchestration
  api/routes.py          HTTP layer
backend/reference/*.csv  terminology and edit tables (swap for your licensed set)
backend/rules/rules.yaml severity, thresholds, on/off — tuned without a deploy
frontend/index.html      the whole workspace, one file, no build step
```

## Before this touches real patient data

This is an MVP. It is architecturally sound and deliberately incomplete on the
operational side. What must be added:

**Security and compliance**
- Authentication, per-user sessions, and role separation (coder / auditor /
  compliance). There is none today — anyone who can reach the port sees
  everything.
- Encryption at rest for `storage/` and the database, TLS in transit.
- A signed BAA with every vendor in the path, including your OCR and any LLM.
- Retention and purge policy. Charts should not sit on disk indefinitely.
- The audit trail exists but needs tamper-evidence (append-only storage or
  signing) to be worth anything in a dispute.

**Scale and operations**
- OCR is synchronous. A 2-page scan takes ~8 seconds; a 60-page inpatient record
  will time out an HTTP request. Move ingestion to a worker queue (Celery/RQ +
  Redis) with per-page parallelism and a progress channel.
- Postgres instead of SQLite once more than one coder is working.
- The terminology loader is `lru_cache`'d at process start; a quarterly code-set
  update currently needs a restart.

**Accuracy**
- The dictionary matcher needs real terminology curation. Exact-phrase matching
  is precise and brittle: "poorly controlled type 2 diabetes mellitus" will not
  match a keyword of "poorly controlled diabetes". The fix is a curated synonym
  set per code, which is ongoing work, not a one-time build.
- OCR preprocessing is minimal (greyscale + autocontrast). Real fax traffic
  wants deskew, denoise, and adaptive thresholding — OpenCV, and worth the effort.
- Handwriting is not handled at all. Tesseract will not read a physician's
  handwritten progress note. That needs a different class of model, and it
  should be treated as a separate project.
- No inpatient DRG/MS-DRG grouping, no risk-adjustment/HCC scoring, no payer-
  specific policy layer.

**Measurement**
- Before trusting this on volume, run it against a set of charts your coders
  have already coded and measure precision and recall per code, per rule. A
  suggestion engine at 70% precision costs coders more time than it saves.
  Rules whose findings get dismissed >30% of the time should be retuned or
  turned off — the dismissal reasons are captured for exactly this purpose.

## Disclaimer

Decision support, not a coding authority. Every suggestion and every finding is
a prompt for a qualified human to make a decision. Final code assignment is the
responsibility of the certified coder and the rendering provider. All sample
data is synthetic.
