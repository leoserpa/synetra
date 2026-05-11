# Synetra

[Leia em Português](README.md) · [Read in English](README_EN.md)

> ETL pipeline for fundamental analysis of CVM open data. Polars engine, Domain-Driven Design layout.

![version](https://img.shields.io/badge/version-1.1.0-blueviolet)
![python](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python)
![engine](https://img.shields.io/badge/Engine-Polars%20%2B%20Rust-yellow)
![tests](https://img.shields.io/badge/tests-80%20passed-success)
![typed](https://img.shields.io/badge/mypy-strict-informational)
![lint](https://img.shields.io/badge/ruff-passing-brightgreen)
![data](https://img.shields.io/badge/Data-CVM%20Open%20Data-orange)

---

## What it is

Synetra reads raw CVM filings (DFP and FRE from 2010 onwards), applies sector-aware accounting rules (industrial, financial, insurance) and produces a wide historical series with fundamental indicators ready for quantitative analysis.

Financial logic is pure and isolated in `synetra/domain/`. Download, cache and I/O live at the edges. That keeps every formula easy to test and audit on its own.

When enabled, Synetra attaches B3 quotes via Yahoo Finance to compute historical multiples (P/E, P/BV, Market Cap) and a snapshot of the latest trading session.

## Table of contents

- [Stack](#stack)
- [What the pipeline produces](#what-the-pipeline-produces)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [How to run](#how-to-run)
- [Configuration (`config.toml`)](#configuration-configtoml)
- [Project structure](#project-structure)
- [Indicators](#indicators)
- [Testing and quality](#testing-and-quality)
- [Performance](#performance)
- [Outputs](#outputs)
- [Further reading](#further-reading)

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| Data engine | Polars 1.40+ (Rust) |
| Async HTTP | httpx 0.28+ |
| Config validation | Pydantic 2.13+ |
| Structured logging | loguru |
| Market data | yfinance |
| Package manager | uv |
| Quality tooling | ruff · mypy strict · pytest · hypothesis · bandit · pip-audit |

## What the pipeline produces

- Wide historical CSV (`TICKER × YEAR × indicators`) from 2010 to the latest filed fiscal year.
- Current snapshot with the latest trading price and recomputed multiples (`snapshot_atual.csv`).
- Per-ticker data quality report (`data_quality_report.csv`).
- Automatic temporal audit (gaps, ROE outliers, zero-revenue records).
- Per-step execution metrics via `PipelineMetrics` (timing, rows processed, market coverage).

## Architecture

```mermaid
graph TB
    subgraph "External sources"
        CVM["CVM Open Data (ZIP)"]
        YF["Yahoo Finance (optional)"]
    end

    subgraph "Infra (edges)"
        DL["downloader.py<br/>(async HTTPX + cache)"]
        LD["loader.py<br/>(ZIP → Parquet)"]
        YC["market/yahoo_client.py"]
        CACHE[(".synetra_cache/<br/>Parquet)"]
    end

    subgraph "Domain (pure)"
        DOM["domain/indicators.py<br/>(Tiers 1-5)"]
        TR["transformer.py<br/>(orchestrator)"]
    end

    subgraph "Output"
        CSV1["serie_historica_financeira.csv"]
        CSV2["snapshot_atual.csv"]
        DQ["data_quality/<br/>data_quality_report.csv"]
        OBS["observability.py<br/>(metrics)"]
    end

    CVM --> DL --> CACHE --> LD --> TR
    YF --> YC --> TR
    DOM -->|pure expressions| TR
    TR --> CSV1
    TR --> CSV2
    TR --> DQ
    TR --> OBS
```

Three principles drive the layout:

1. **Isolated domain.** Everything under `synetra/domain/` is a pure Polars expression. No I/O, no config dependency.
2. **Thin edges.** Downloader, loader and yahoo_client are the only modules that touch the network or disk.
3. **Immutable config.** `config.toml` is read once, validated by Pydantic (`frozen=True`) and passed around via injection.

## Prerequisites

- Python 3.12 or newer
- [uv](https://github.com/astral-sh/uv) to manage the environment
- ~500 MB of disk space for the Parquet cache (16 years of DFP + FRE)
- Internet access for the first run. After that the cache covers the rest.

## How to run

```bash
# 1. Install dependencies
uv sync

# 2. Adjust targets in config.toml (years, tickers, sectors)
#    Ticker ↔ CNPJ map lives in mapa_tickers.csv

# 3. Run the pipeline
uv run python main.py
```

Output files land in the project root:
- `serie_historica_financeira.csv`
- `snapshot_atual.csv` (if `[market].enabled = true`)
- `data_quality_report.csv`
- `synetra.log` (rotated at 10 MB)

### Handy commands

```bash
uv run pytest                     # full test suite
uv run pytest -k indicators       # run tests by keyword
uv run ruff check .               # lint
uv run ruff format .              # format
uv run mypy synetra               # strict type check
uv run bandit -r synetra          # security scanner
uv run pip-audit                  # dependency CVE audit
```

## Configuration (`config.toml`)

The file is split into blocks. Each one is validated by a Pydantic model in `synetra/config.py`.

| Block | Purpose |
|---|---|
| `[urls]` | CVM URL patterns (must contain `{year}`) |
| `[pipeline]` | Document types, year range, concurrency, force refresh |
| `[market]` | Toggle Yahoo Finance and cache/batch parameters |
| `[contas.industrial]` | CVM account code → canonical name map (industrial companies) |
| `[contas.financeiro]` | Same for banks and credit institutions |
| `[contas.seguradora]` | Same for insurers and capitalization companies |
| `[setores]` | Keywords that classify each company's sector |
| `[regex]` | Regex patterns to detect special accounts (D&A, capex, dividends) |

Minimal example:

```toml
[pipeline]
doc_types = ["DRE", "BPA", "BPP", "DFC_MI", "DFC_MD"]
years_start = 2010
years_end = 2026
max_workers = 5
force_refresh = false

[market]
enabled = true
cache_max_age_days = 7
batch_size = 100
```

A broken config aborts the pipeline before any I/O. Validation errors say exactly which field failed.

## Project structure

```
Projeto_CVM/
├── synetra/
│   ├── __init__.py            # public version + re-exported API
│   ├── config.py              # Pydantic models + load_config()
│   ├── downloader.py          # async HTTP, Last-Modified cache
│   ├── loader.py              # CVM CSV → Parquet (DFP + FRE)
│   ├── transformer.py         # calculation orchestrator
│   ├── observability.py       # PipelineMetrics + timed_step()
│   ├── utils.py               # vectorized helpers (text, display)
│   ├── py.typed               # PEP 561 marker
│   ├── domain/                # domain layer (pure functions)
│   │   ├── __init__.py
│   │   └── indicators.py      # Tiers 1-5, Altman Z'', expression helpers
│   ├── market/                # quote integration
│   │   ├── yahoo_client.py    # OHLCV download + cache
│   │   └── price_aggregator.py# snapshot + historical valuation
│   └── data_quality/          # post-pipeline audit
│       ├── checks.py          # individual rules (stale, delisted, ...)
│       ├── models.py          # severity enums and flag types
│       └── report.py          # aggregation + CSV output
│
├── tests/                     # 19 modules, ~80 tests (pytest + hypothesis)
├── docs/                      # WIKI.md, index.md, stylesheets/
├── .github/workflows/         # ci.yml, docs.yml
├── .synetra_cache/            # Parquet cache (generated on first run)
├── scratch/                   # experimental scripts (ignored by lint)
│
├── main.py                    # entry point (asyncio.run)
├── config.toml                # pipeline config
├── mapa_tickers.csv           # Ticker ↔ CNPJ ↔ corporate name
├── mkdocs.yml                 # documentation build
├── pyproject.toml             # deps + ruff/mypy/pytest/coverage
└── uv.lock                    # reproducible lockfile
```

## Indicators

All indicators live in `synetra/domain/indicators.py` as pure functions. They run in five tiers with chained dependencies.

| Tier | Indicators |
|---|---|
| **1 — Profitability and bases** | ROE, ROA, EPS, BVPS, Asset Turnover, LT Leverage, Accruals, Accrual Ratio, GP/A, Gross/EBIT/Net margins |
| **2 — Cash flow** | FCF, Payout, EBITDA |
| **3 — Capital structure** | EBITDA Margin, Total Debt, Current Ratio |
| **4 — Leverage** | Net Debt, Debt/Equity |
| **5 — Final ratios** | ND/EBITDA, ROIC (Damodaran), Altman Z''-Score EMS |

On top of the tiers, `transformer.py` also computes:

- **Piotroski F-Score** — earnings quality, 9 criteria, industrial sector only.
- **Beneish M-Score** — earnings manipulation detection, 8 terms, industrial only.
- **Growth** — YoY and 3y/5y CAGR for revenue and earnings.
- **Quant factors** — earnings stability, ROE volatility, delta ROE, delta margin, cash conversion.
- **Operational efficiency** — FCO Margin, FCF Margin, CASH_ROA, DSO, Working Capital, ROCE, NOPAT, Reinvestment Rate, Sustainable Growth, Cash Ratio.

When quotes are available (Yahoo enabled):

- **Annual historical valuation** — P/E, P/BV, Market Cap using the year's last trading day close.
- **Current snapshot** — P/E, P/BV and Market Cap using the latest available close.

### Sector-aware cleanup

Banks and insurers have their own accounting model. Industrial metrics that don't apply to them are explicitly `null`, not zero. This prevents rankings from mixing companies on metrics that don't mean the same thing.

| Sector | Nullified metrics |
|---|---|
| Banks | EBITDA, operating margins, CAPEX, ROIC, liquidity, all debt ratios, trade receivables, cash conversion, DSO, ROCE, NOPAT |
| Insurers | Gross Margin, ROIC, Current Ratio, debt ratios |

## Testing and quality

- **80 tests** across 19 modules: unit, integration, property-based (hypothesis), mocked I/O.
- **mypy strict** on production code. Scratch and tests are excluded.
- **ruff** with bugbear, simplify, comprehensions, pyupgrade and isort enabled.
- **GitHub Actions CI** runs lint, type check and tests on every push.
- **Coverage** via `pytest-cov` configured in `pyproject.toml`.

Domain tests (`test_indicators.py`, `test_transformer_formulas.py`) cover every formula in isolation, including zero-denominator edge cases and per-sector segregation.

## Performance

Measured on a reference machine (Intel Core i3, SATA SSD, Windows 11):

| Step | Approx. time |
|---|---|
| Download 16 years of DFP + FRE (first run, typical connection) | ~30 s |
| Read full Parquet cache | < 2 s |
| Compute indicators (Tiers 1-5 + F-Score + Beneish + quant) | ~4 s |
| Yahoo Finance download (~350 tickers, 15 years) | ~10 s |
| Full pipeline with warm cache | < 10 s |
| Full pipeline first run | ~46 s |

Decisions that matter for that budget:

- **Lazy Parquet scan** with predicate pushdown — only the target CNPJ subset hits memory.
- **Categorical dtypes** on low-cardinality columns (CNPJ, sector, category, account name) cut footprint by ~70%.
- **Full vectorization** of calculations. No `apply`, no Python loop over rows.
- **Consolidated shifts** in a single `with_columns` let Polars optimize the execution plan.
- **Parallel downloads** with `asyncio.gather` + `Semaphore`. CPU-bound work (CSV parsing) runs on `run_in_executor`.

## Outputs

| File | Content |
|---|---|
| `serie_historica_financeira.csv` | Wide historical series, `;` separator, UTF-8, 2 decimals for monetary values and 4 for ratios. |
| `snapshot_atual.csv` | One row per ticker with the latest close, recomputed multiples and timestamp. |
| `data_quality_report.csv` | Per-ticker quality flags: no Yahoo history, stale quote, likely delisting, recent IPO, temporal gaps. |
| `synetra.log` | Structured log rotated at 10 MB. |

## Further reading

- [Detailed technical wiki](docs/WIKI_EN.md) — formulas, modeling decisions, analysis examples. [Versão em português](docs/WIKI.md).
- [README in Portuguese](README.md).
- Domain code (`synetra/domain/indicators.py`) and the transformer (`synetra/transformer.py`) carry extensive docstrings with each indicator's derivation.
