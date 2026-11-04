# DWH Studio

[![CI](https://github.com/Tuz34/dwhstudio/actions/workflows/ci.yml/badge.svg)](https://github.com/Tuz34/dwhstudio/actions/workflows/ci.yml)

**Metadata-driven data warehouse automation for Oracle® Database and Oracle Data Integrator**

DWH Studio reads source metadata from an Oracle database, proposes target tables and column
mappings, generates the matching Oracle Data Integrator (ODI) mappings, and runs data quality,
reconciliation, and audit checks around every load. Every step that changes a database or an ODI
repository goes through an explicit approval gate, and a missing or failing gate stops the load
instead of letting it continue. An optional AI assistant (local models by default, cloud optional)
helps with mapping suggestions, but it never bypasses those gates.

## Architecture

- **Backend:** Flask API in `app/odi-dwh`, with route modules in `routes/` and domain modules at the
  top level (`schema_extractor.py`, `mapping_generator.py`, `scd2_manager.py`, `watermark_manager.py`,
  `data_quality.py`, `reconciliation_service.py`).
- **Frontend:** React and Vite single-page app in `app/odi-dwh/frontend/`.
- **Metadata extraction:** reads tables, columns, and keys from the source Oracle schemas.
- **Mapping generation:** builds source-to-target mappings and DDL proposals from that metadata.
- **ODI automation:** Groovy scripts in `app/odi-dwh/odi_automation/` create datastores, mappings,
  scenarios, and load plans through the ODI SDK; `odi_orchestrator.py` drives them.
- **Quality gates:** data quality rules, reconciliation, and pre/post-load checks.
- **Approval gate and fail-closed loading:** persistent actions (DDL, ODI create/update, scenario
  runs) require an explicit approval; if a gate is absent or reports a failure, the action is refused.
- **Watermark and incremental logic:** per-table watermarks drive delta loads; fact tables can be
  appended by business date.

```
 Browser (React/Vite)
        |
   Flask API  --- routes/ --- approval gates / quality gates
        |
  +-----+--------------------+
  |                          |
Oracle metadata        ODI Groovy automation  --->  ODI repository + agent
extraction / DDL              (odi_automation/)            |
  |                                                        v
  +--------------------> Oracle Database (ODS / DWH / audit schemas)
```

## Problem and motivation

Building ODI mappings and the surrounding warehouse tables by hand is repetitive and error-prone:
column lists drift, load patterns are applied inconsistently, and checks are added late. DWH Studio
keeps the pattern definition in metadata, generates the repetitive objects from it, and records
evidence for each change so reviews can focus on decisions instead of typing.

## Tech stack

- Python 3.12, Flask, `python-oracledb`, pandas
- React, Vite, Tailwind CSS
- Oracle Database 19c
- Oracle Data Integrator 12c with Groovy automation
- Optional AI providers: local Ollama or an OpenAI-compatible local server, optional Anthropic API

## Quick start

Prerequisites (honest list, none of this is optional for the full pipeline):

- An Oracle Database 19c installation you can reach.
- An Oracle Data Integrator 12c installation with a JEE or standalone agent and a repository.
- Python 3.12 and Node.js for the frontend.
- You supply your own Oracle JDBC driver and ODI SDK jars from your own installation. None are
  shipped in this repository. There is no Docker one-liner.

```bash
git clone https://github.com/Tuz34/dwhstudio.git
cd dwhstudio/app/odi-dwh
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt -r requirements-dev.txt   # Linux/macOS
cp .env.example .env              # then edit the values
python app.py

# in another terminal
cd frontend
npm install
npm run dev
```

## Configuration

Two templates cover the Flask, Oracle, ODI and AI settings and the Groovy connection file. Copy them and replace each `CHANGE_ME`:

- `app/odi-dwh/.env.example` copied to `.env`: Flask session key, deployment profile, Oracle
  connection, ODI repository and agent, AI providers, paths and logging.
- `app/odi-dwh/odi_automation/odi_connection.properties.example` copied to
  `odi_connection.properties`: ODI master repository and login used by the Groovy scripts.

Both real files are git-ignored. Never commit them.

## Data model

Examples from the code and tests (Oracle sample schemas `HR` and `SH` as sources):

- **ODS layer:** Type 1 current-state copies such as `DWH.ODS_SH_PRODUCTS` and `DWH.ODS_HR_EMPLOYEES`.
- **Dimensions:** `DWH.DIM_PRODUCT` as a Type 2 slowly changing dimension with surrogate key,
  effective-from and effective-to dates, and a current flag.
- **Facts:** `DWH.ODS_SH_SALES` loaded append-only by business date (`TIME_ID`), including a
  partitioned-fact contract.
- **Watermarks:** incremental strategies track a watermark column per source table.
- **Audit schema:** alert log, mapping log, data lineage, mapping snapshot, and mapping run state.

## Tests and quality gates

GitHub Actions runs these gates, a frontend build, and a secret scan for every pull request and main push; its pytest step deselects the 13 environment-dependent cases tracked in ODI-80.

From `app/odi-dwh`:

```bash
.venv/Scripts/python.exe python_quality_gate.py
.venv/Scripts/python.exe -m pytest -q
```

From the repository root:

```bash
python tools/workspace/appsec_gate.py --no-write
```

## Roadmap

Post-publication items: transaction and atomicity hardening of the ODI and Oracle execution paths
(for example making the incremental append and the watermark update one unit), plus the other
planned items listed in [SECURITY.md](SECURITY.md).

## Security

See [SECURITY.md](SECURITY.md) for how to report issues, the supported deployment model, and known
limitations.

## Knowledge sources

The local retrieval index can use ODI best-practice material. Oracle whitepapers are not
redistributed here. Read the Oracle documentation at <https://docs.oracle.com/> and regenerate the
`knowledge_sources/odi` folder locally from material you are licensed to use.

## License and trademarks

Licensed under Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

This is an independent project and is not affiliated with or endorsed by Oracle.

Oracle, Java, MySQL, and NetSuite are registered trademarks of Oracle and/or its affiliates. Other names may be trademarks of their respective owners.
