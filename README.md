# RazorRecon

Deployed Link : https://razorrecon.streamlit.app/

> First load can take ~30s: the free-tier host sleeps after a period of inactivity and wakes on the first visit, then generates and reconciles the 5,000-record benchmark. Reloading during that window is safe - the app detects a half-finished startup and rebuilds rather than showing partial results.

RazorRecon is a compact AI Finance Controller demo for Razorpay reconciliation. It keeps financial truth deterministic and uses a constrained investigator only to explain exceptions from read-only evidence.

## Architecture

![RazorRecon reconciliation pipeline: Razorpay Test Mode API and a synthetic benchmark feed data ingestion, which feeds deterministic reconciliation in Python — the financial source of truth. Matched payments close automatically; exceptions go to Gemini AI investigation, which works only from read-only verified evidence and falls back deterministically on any failure. Every case ends in human review (resolve or escalate), and every step is written to the audit trail.](assets/architecture.svg)

## Features

- Reproducible synthetic benchmark with 5,000 varied payments, a fixed seed, and separate development and held-out splits.
- Deterministic reconciliation for ID matching, settlement linkage, fee/tax adjustment, refunds, duplicates, timing windows, bank UTR checks, missing settlements, and amount discrepancies.
- Machine-readable reason codes for every reconciliation result.
- Exposure classification that separates money genuinely at risk from settlements that have simply not arrived yet.
- Read-only investigation tools with guardrails against fabricated evidence or direct financial mutation.
- Gemini investigation for exception explanations when `LLM_API_KEY` is configured, with deterministic fallback when it is not.
- Human review actions for resolve/escalate, written to an audit trail.
- Held-out evaluation metrics and exception taxonomy calculated from actual benchmark runs.
- Streamlit demo dashboard, exception queue, investigation view, evaluation page, and audit table.
- Razorpay Test Mode connector separated from synthetic benchmark data.

## Verified Razorpay API Assumptions

Verified against official Razorpay documentation on 2026-08-27:

- Fetch payments: `GET /v1/payments` with `from`, `to`, `count`, and `skip`.
- Fetch settlements: `GET /v1/settlements/` with `from`, `to`, `count`, and `skip`.
- Fetch settlement reconciliation: `GET /v1/settlements/recon/combined` with required `year`, `month`, optional `day`, `count`, and `skip`.
- Settlement recon rows can include `payment`, `refund`, `transfer`, and `adjustment` transaction types.

Docs:

- https://razorpay.com/docs/api/payments/fetch-all-payments/
- https://razorpay.com/docs/api/settlements/fetch-all/
- https://razorpay.com/docs/api/settlements/fetch-recon/

## Verified Gemini API Assumptions

Verified against official Google AI documentation on 2026-08-28:

- Model: `gemini-3.5-flash-lite`, selected as a stable low-cost Flash-Lite model with structured output support.
- API: `POST https://generativelanguage.googleapis.com/v1beta/interactions`.
- Structured JSON output is requested with top-level `response_format`.

Docs:

- https://ai.google.dev/gemini-api/docs/models
- https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite
- https://ai.google.dev/gemini-api/docs/structured-output?lang=rest

## Setup

```bash
pip install -r requirements.txt
```

Optional environment variables:

```text
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
LLM_API_KEY=
DATABASE_URL=razorrecon.sqlite
```

Razorpay credentials must be Test Mode credentials. Live-money actions are not implemented.

## Run The Demo

```bash
python -m streamlit run app.py
```

Then open the Streamlit URL, usually `http://localhost:8501`.

The app loads and reconciles the synthetic benchmark automatically when the local SQLite database is empty, so a deployed instance works without Razorpay account data.

On startup the app compares row counts rather than checking whether tables are merely non-empty. A page reload during the cold-start run kills that script run, and because the database connection is cached across runs the next run can see the abandoned transaction's partial rows. Treating that as a finished run would report a high match rate with an empty exception queue. When the counts disagree, the app discards the partial write and rebuilds.

Demo flow:

1. Review dashboard metrics from the automatically loaded synthetic benchmark.
2. Optionally click **Load benchmark** and **Run reconciliation** to reset/replay the demo.
3. Review records, matched count, match rate, exceptions, accuracy, precision, recall, throughput, and unresolved exposure by class.
4. Open the **Exceptions** tab for the taxonomy and the queue.
5. Open **Investigation**, filter by exception type and status, pick a case, and click **Investigate**.
6. Review investigation source, tool calls, evidence, confidence, recommendation, and audit history.
7. Click **Resolve** or **Escalate** to record a human decision.

Wide tables are paged to the first 500 rows so a free-tier host is not serialising 5,000 rows to the browser on every interaction. The complete figures are always available from the CLI benchmark.

## Benchmark Methodology

The included benchmark is entirely synthetic. It is designed to make reconciliation behavior measurable and reproducible; it is not a claim about production accuracy.

- Default size: 5,000 payment records.
- Fixed random seed: `42`.
- Split: 4,000 development-style records and 1,000 held-out records (80/20, stratified by scenario).
- Variation: payment amounts, fee rates, payment methods, timestamps, settlement timing, discrepancies, and identifiers are generated from the seeded random generator. Records are not duplicated copies of a smaller fixture.
- Ground truth: stored separately for every payment with its split, scenario, expected status, and expected exception type.
- Independence boundary: reconciliation queries only financial source columns. It does not receive the ground-truth label or expected result. Ground truth is joined only after deterministic results have been written, for evaluation and test compatibility.

The held-out set is scored independently after reconciliation. Reported metrics include total records, matches, exceptions, match rate, exact classification accuracy, exception precision and recall, false positives, false negatives, measured throughput, unresolved exception count/value, and exception counts by type.

Metric definitions:

- **Accuracy**: percentage whose matched/exception status is correct and, for exceptions, whose deterministic exception type exactly matches ground truth.
- **Precision**: true exception detections divided by all predicted exceptions.
- **Recall**: detected ground-truth exceptions divided by all ground-truth exceptions.
- **Unresolved value**: sum of the absolute reconciliation differences for open, human-review, or escalated exceptions. It is an exposure indicator, not a ledger balance, and it is reported split by exposure class rather than as one number.
- **Throughput**: always measured over the whole reconciliation run. A split is scored afterwards from stored results, so it has no separate timing of its own.

### Exposure Classes

A reconciliation difference does not mean the same thing for every exception type, so summing them into a single "unresolved value" overstates risk. Each type is classified:

- **Amount at risk** - the ledger disagrees about money that has already moved: `AMOUNT_MISMATCH`, `BANK_UTR_AMOUNT_MISMATCH`, `REFUND_AMOUNT_MISMATCH`, `SETTLEMENT_AMOUNT_DISCREPANCY`, `DUPLICATE_RECONCILIATION_RECORD`.
- **Awaiting settlement** - no settlement or reconciliation row exists yet, so the difference is the entire payment value. This is pipeline lag, not loss: `MISSING_SETTLEMENT`, `MISSING_RECONCILIATION_RECORD`.
- **Structural** - the amounts reconcile exactly but the linkage between records is wrong, so the difference is legitimately zero: `WRONG_MAPPING`, `TIMING_WINDOW_EXCEEDED`, `PAYMENT_ID_MISMATCH`.

On the default 5,000-record benchmark this separates roughly INR 1.86 lakh of genuine amount-at-risk from roughly INR 1.38 crore that is only awaiting settlement. An unrecognised exception type is classified as amount at risk rather than quietly discounted.

Exception taxonomy includes `WRONG_MAPPING`, `MISSING_RECONCILIATION_RECORD`, `AMOUNT_MISMATCH`, `DUPLICATE_RECONCILIATION_RECORD`, `MISSING_SETTLEMENT`, `TIMING_WINDOW_EXCEEDED`, `REFUND_AMOUNT_MISMATCH`, `SETTLEMENT_AMOUNT_DISCREPANCY`, and `BANK_UTR_AMOUNT_MISMATCH`. The engine also emits `PAYMENT_ID_MISMATCH`, which the synthetic generator does not produce; it is reachable on Razorpay connector data where a reconciliation row's entity does not match the captured payment. Counts, percentages, and unresolved values are calculated from each run; no example totals are hardcoded.

For `WRONG_MAPPING`, the generated evidence preserves the intended semantics: `entity_id` is the original payment ID and `payment_id` is the incorrect payment ID.

## AI Safety Boundary

Financial truth comes only from deterministic Python reconciliation. Gemini runs only after a verified exception is selected, receives read-only evidence, and cannot mutate payments, settlements, reconciliation records, or bank entries. Any unresolved financial decision remains a human action recorded in the audit trail.

If `LLM_API_KEY` is absent, the API request fails, the response is malformed, or structured-output validation fails, RazorRecon continues with its evidence-grounded deterministic fallback. The UI always identifies the investigation source as either `Gemini AI` or `Deterministic fallback`.

## Streamlit Community Cloud Deployment

Use `app.py` as the Streamlit entry point.

The deployed app defaults to synthetic benchmark mode. It does not require Razorpay credentials, Razorpay account data, a pre-existing SQLite database, or CLI setup by the judge before first use.

Required repository files for deployment:

- `app.py`
- `core.py`
- `requirements.txt`
- `evaluation.py`
- `tests/test_reconciliation.py`
- `README.md`
- `assets/architecture.svg`
- `.env.example`
- `.gitignore`

Do not commit `.env`, `.streamlit/secrets.toml`, local SQLite databases, logs, caches, or API keys.

Optional Streamlit Secrets:

```toml
RAZORPAY_KEY_ID = "rzp_test_..."
RAZORPAY_KEY_SECRET = "..."
LLM_API_KEY = "..."
DATABASE_URL = "razorrecon.sqlite"
```

All secrets are optional for the public demo. Without Razorpay keys, synthetic benchmark mode remains available. Without `LLM_API_KEY`, investigations use deterministic fallback.

Deployment steps:

1. Clone the repository.
2. Install requirements with `pip install -r requirements.txt`.
3. Optionally configure local environment variables in `.env`.
4. Run tests with `python -m pytest`.
5. Run the benchmark with `python evaluation.py`.
6. Run locally with `python -m streamlit run app.py`.
7. Push this repository to GitHub without `.env`, `.streamlit/secrets.toml`, or `razorrecon.sqlite`.
8. Go to `https://share.streamlit.io`.
9. Click **Create app**.
10. Select the GitHub repository and branch.
11. Set the main file path to `app.py`.
12. Open **Advanced settings**.
13. Paste any optional secrets in TOML format.
14. Deploy.

## CLI Benchmark

```bash
python evaluation.py
```

The CLI uses an isolated in-memory SQLite database, generates the default seeded dataset, and prints both the complete benchmark and held-out report in a demo-ready format. Running it does not modify `razorrecon.sqlite`.

Results should be described as: "On the included synthetic held-out benchmark..." They must not be presented as production accuracy.

## Tests

```bash
python -m pytest
```

The tests cover 5,000-record generation, fixed-seed reproducibility, held-out independence and metrics, required exception classes, false-positive/false-negative safety, wrong mapping semantics, exposure-class partitioning, bank-entry referential integrity, rebuild of a partially written result set, approved investigation tools, Gemini structured validation and failure fallback, evidence recording, insufficient-evidence routing, mutation-tool blocking, and Razorpay pagination.

## File Map

- `app.py` - Streamlit UI and demo workflow.
- `core.py` - SQLite schema, benchmark generator, reconciliation engine, Razorpay connector, investigator tools, evaluation, and audit logic.
- `evaluation.py` - CLI benchmark runner.
- `tests/test_reconciliation.py` - focused tests for critical financial and guardrail behavior.
- `.env.example` - required configuration shape.
- `.gitignore` - excludes secrets, local DBs, caches, and generated artifacts.

## Known Limitations

- Gemini investigation requires `LLM_API_KEY`; without it, the deterministic evidence-grounded fallback remains fully usable.
- Razorpay Test Mode sync requires valid Test Mode keys and actual account data; the benchmark path remains separate and reliable for demos.
- No autonomous refunds, payouts, or financial mutations exist.
- Synthetic benchmark behavior does not establish performance or accuracy on production Razorpay or bank data.
- The benchmark scores 100% accuracy, precision, and recall by construction: every scenario the generator produces is drawn from the failure modes the deterministic rules already cover. That result demonstrates rule coverage and guards against regressions. It is not evidence of accuracy on real data, which contains failure modes this generator does not create.
- Throughput is machine-dependent and is measured for the complete local benchmark run, not claimed as a service-level guarantee.
