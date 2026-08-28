# RazorRecon

RazorRecon is a compact AI Finance Controller demo for Razorpay reconciliation. It keeps financial truth deterministic and uses a constrained investigator only to explain exceptions from read-only evidence.

## Features

- Reproducible synthetic benchmark with 540 payments and labelled exception cases.
- Deterministic reconciliation for ID matching, settlement linkage, fee/tax adjustment, refunds, duplicates, timing windows, bank UTR checks, missing settlements, and amount discrepancies.
- Machine-readable reason codes for every reconciliation result.
- Read-only investigation tools with guardrails against fabricated evidence or direct financial mutation.
- Gemini investigation for exception explanations when `LLM_API_KEY` is configured, with deterministic fallback when it is not.
- Human review actions for resolve/escalate, written to an audit trail.
- Evaluation metrics calculated from actual benchmark runs.
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

Demo flow:

1. Review dashboard metrics from the automatically loaded synthetic benchmark.
2. Optionally click **Load benchmark** and **Run reconciliation** to reset/replay the demo.
3. Review records, matched count, match rate, exceptions, accuracy, precision, recall, throughput, and unresolved value.
4. Open the **Exceptions** tab.
5. Open **Investigation**, select an exception, and click **Investigate**.
6. Review investigation source, tool calls, evidence, confidence, recommendation, and audit history.
7. Click **Resolve** or **Escalate** to record a human decision.

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

Last verified benchmark run:

```text
records processed: 540
matched: 360
exceptions: 180
match rate: 66.67%
throughput: 5725.73 records/second
exception precision: 100.0%
exception recall: 100.0%
reconciliation accuracy: 100.0%
false positives: 0
false negatives: 0
```

## Tests

```bash
python -m pytest
```

The tests cover benchmark labels, deterministic reconciliation, exact-match false-positive safety, wrong mapping detection, approved investigation tools, Gemini/fallback behavior, evidence recording, insufficient-evidence routing, mutation-tool blocking, and Razorpay pagination.

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
