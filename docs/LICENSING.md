# Code sets: licensing and update cadence

Read this before pointing the system at production data. Getting it wrong is a
legal problem, not a technical one.

## What is free

**ICD-10-CM** (diagnoses) is published by CDC/NCHS and CMS and may be
redistributed. Download the annual release from the CMS ICD-10 page. It updates
**1 October** each year, with occasional April addenda.

**NCCI edits** (Procedure-to-Procedure pairs, Medically Unlikely Edits) are
published by CMS and updated **quarterly**. They are free to download.

**LCD/NCD coverage policies** are published by CMS and the Medicare
Administrative Contractors. Commercial payer policies are not — those you get
from your contracts.

## What is not free

**CPT** (Current Procedural Terminology) is copyrighted by the **American
Medical Association**. Code numbers, descriptors, guidelines, and modifiers are
all covered. You need a licence from the AMA to use CPT content in software,
and the licence terms differ for internal use versus distribution. Budget for
it; it is a real line item.

**HCPCS Level II** codes are published by CMS and are free, but some descriptors
incorporate AMA content and some HCPCS-adjacent data sets (like the RUC RVU
files' CPT descriptors) carry the AMA terms.

**SNOMED CT** requires an affiliate licence (free in UMLS member countries
including the US, but a licence nonetheless).

**Encoder-grade terminology** — the curated synonym sets that make autocoding
actually work — is generally a commercial product. Building your own is
possible and is the bulk of the ongoing effort in a project like this.

## What ships in this repository

`backend/reference/*.csv` contains a small demo subset:

- ICD-10-CM codes with real code numbers and paraphrased descriptions
- CPT/HCPCS **code numbers** with **paraphrased, non-verbatim** descriptions
- A handful of illustrative NCCI-style pairs and necessity policies that were
  written by hand for demonstration, **not** extracted from the CMS files

This is enough to exercise every rule and is not suitable for production. Do not
treat any of it as authoritative.

## Swapping in your licensed release

The loader reads plain CSV. Match these column names and drop the files in
`backend/reference/`:

| File | Required columns |
|---|---|
| `icd10cm.csv` | `code, description, unspecified, sex, age_min, age_max, keywords` |
| `cpt.csv` / `hcpcs.csv` | `code, description, keywords, global_days, mue, sex, age_min, age_max, category, bilateral_eligible, professional_component, em_level, em_type` |
| `ncci_ptp.csv` | `column1, column2, modifier_allowed, rationale` |
| `necessity.csv` | `code, allowed_icd10_prefixes, policy` |
| `specificity.csv` | `from_code, to_code, trigger_keywords, prompt, severity` |

`keywords` and `allowed_icd10_prefixes` are `|`-separated.

Point `CA_REFERENCE_DIR` at a directory outside the repository if your licence
forbids committing the content — which it probably does.

## Update cadence to build into your operations

| Set | Effective | Source |
|---|---|---|
| ICD-10-CM | 1 October | CMS / NCHS |
| CPT | 1 January | AMA |
| HCPCS Level II | quarterly | CMS |
| NCCI PTP + MUE | quarterly | CMS |
| MS-DRG grouper | 1 October | CMS |

A coding tool running last year's tables is worse than no tool: it produces
confident, wrong, deleted-code suggestions. Treat the reference update as a
release with its own regression test, not as a data drop.
