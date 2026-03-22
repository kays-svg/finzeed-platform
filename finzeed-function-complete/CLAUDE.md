# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Azure Functions Python application for Finzeed — an AI-powered SME working capital financing platform targeting Egyptian SMEs. Provides credit assessment, bank statement analysis, and a chat interface.

## Development Setup

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run locally (requires Azure Functions Core Tools)
func start

# Test Document Intelligence extraction methods
python test_extraction_methods.py
```

Required environment variables (set in `local.settings.json` for local dev):
- `SQL_CONNECTION_STRING` — Azure SQL Server connection
- `AzureWebJobsStorage` — Blob storage connection string
- `DOCUMENT_INTELLIGENCE_ENDPOINT` / `DOCUMENT_INTELLIGENCE_KEY` — Azure Form Recognizer
- `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_KEY` — Azure OpenAI service
- `AZURE_OPENAI_DEPLOYMENT` — LLM deployment name (defaults to `finzeed-chat`)

## Deployment

```bash
func azure functionapp publish <APP_NAME>
```

A pre-built `function.zip` also exists for direct upload deployment.

## Architecture

### Entry Points

There are two implementations of the same function:

1. **`function_app.py`** — Primary entry point using the Azure Functions v2 programming model. Single route: `POST /api/finzeed_ai_functions`.
2. **`finzeed_ai/__init__.py`** — Alternative implementation using v1 bindings (`function.json`), with more extensive logging for production debugging.

### Request Flow

The main handler dispatches based on content type:
- **Chat requests** (JSON with `"chat": true`) → `handle_chat()` → Azure OpenAI
- **File uploads** (multipart/form-data with bank statement PDFs) → `handle_form_data_request()` → Document Intelligence + OpenAI analysis
- **Credit applications** (JSON) → `handle_credit_application()` → direct assessment

### Core Processing Pipeline

1. **Document Processing**: PDF bank statements → Azure Document Intelligence (text extraction) → OpenAI (transaction extraction with regex fallback via `parse_bank_transactions()`)
2. **Credit Assessment** (`perform_ai_assessment()`): Revenue-tiered decision engine:
   - Tier 1 (10M+ EGP): 18-month, max 5M credit, 85% confidence
   - Tier 2 (5M-10M): 12-month, max 3M credit, 75% confidence
   - Tier 3 (3M-5M): 6-month, max 1.5M credit, 65% confidence
   - Below 3M: UNDER_REVIEW
   - Interest rate: 3.5% monthly (42% annually) — hardcoded across all tiers
3. **Bank Verification**: Compares declared revenue vs bank inflows; flags >30% discrepancies
4. **Persistence** (`save_application_to_db()`): Writes to Users, Applications, and Documents tables in Azure SQL

### Azure Services Used

- **Azure Functions** — Serverless compute (10-min timeout, 100 concurrent requests)
- **Azure Blob Storage** — Document storage
- **Azure Document Intelligence** — PDF text extraction (prebuilt-read model)
- **Azure OpenAI** — Chat completions and bank statement analysis
- **Azure SQL Database** — Application data persistence (via pyodbc)
- **Application Insights** — Monitoring (20 items/sec sampling)

## Key Conventions

- All monetary values are in Egyptian Pounds (EGP)
- CORS is handled manually in each response (no middleware)
- Database connections use pyodbc with SQL Server
- The chat system prompt contains Finzeed product details (credit limits EGP 250K-5M, tenor 6-18 months)
