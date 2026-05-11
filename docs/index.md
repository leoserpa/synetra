# Synetra Documentation

Pipeline ETL para análise fundamentalista de dados da CVM. Polars no motor, Domain-Driven Design na estrutura.

---

## Sobre

Synetra lê os arquivos brutos da CVM (DFP e FRE de 2010 em diante), aplica regras contábeis por setor (industrial, financeiro, seguradora) e devolve uma série histórica larga com indicadores fundamentalistas prontos para análise quantitativa.

A lógica financeira é pura e vive isolada em `synetra/domain/`. Download, cache e I/O ficam nas bordas do sistema. Isso deixa cada fórmula fácil de testar e auditar uma por uma.

Opcionalmente, anexa cotações da B3 via Yahoo Finance para calcular múltiplos históricos (P/L, P/VP, Market Cap) e um snapshot do pregão mais recente.

## Destaques

=== "Indicadores"

    - 5 tiers de indicadores com dependência em cadeia (rentabilidade → fluxo → dívida → alavancagem → ratios)
    - Piotroski F-Score (9 critérios)
    - Altman Z''-Score EMS (Altman, 2005)
    - Beneish M-Score (8 termos)
    - Crescimento YoY e CAGR (3a e 5a)
    - Fatores quantitativos (Quality · Momentum · Risk)
    - 10 indicadores de eficiência operacional
    - Valuation histórico anual + snapshot atual

=== "Performance"

    - Pipeline completo com cache quente em menos de 10 segundos
    - Primeiro run (download de 16 anos) em torno de 46 segundos
    - Lazy Parquet scan com predicate pushdown
    - Categorical dtypes em colunas de baixa cardinalidade (~70% menos RAM)
    - Downloads paralelos com `asyncio` + CPU-bound em thread pool

=== "Qualidade"

    - Suíte com 461 testes (unit, integration, property-based via Hypothesis, mocked I/O)
    - `mypy` em modo strict no código de produção
    - `ruff` com bugbear, simplify, pyupgrade e isort
    - CI rodando lint, type check e testes a cada push
    - Scanner de segurança (`bandit`) e auditoria de dependências (`pip-audit`)

## Arquitetura

```mermaid
graph TB
    subgraph "Fontes externas"
        CVM["CVM Dados Abertos (ZIP)"]
        YF["Yahoo Finance (opcional)"]
    end

    subgraph "Infra (bordas)"
        DL["downloader.py<br>(async HTTPX + cache)"]
        LD["loader.py<br>(ZIP → Parquet)"]
        YC["market/yahoo_client.py"]
        CACHE[(".synetra_cache/<br>Parquet)"]
    end

    subgraph "Domínio (puro)"
        DOM["domain/indicators.py<br>(Tiers 1-5)"]
        TR["transformer.py<br>(orquestrador)"]
    end

    subgraph "Saída"
        CSV1["serie_historica_financeira.csv"]
        CSV2["snapshot_atual.csv"]
        DQ["data_quality/<br>data_quality_report.csv"]
        OBS["observability.py<br>(métricas)"]
    end

    CVM --> DL --> CACHE --> LD --> TR
    YF --> YC --> TR
    DOM -->|expressões puras| TR
    TR --> CSV1
    TR --> CSV2
    TR --> DQ
    TR --> OBS
```

## Quick Start

```bash
# 1. Instalar uv (gerenciador de dependências)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Sincronizar o ambiente
uv sync

# 3. Ajustar alvos em config.toml (anos, tickers, setores)
#    O mapa Ticker → CNPJ fica em mapa_tickers.csv

# 4. Rodar o pipeline
uv run python main.py
```

Saídas geradas na raiz:

- `serie_historica_financeira.csv` — série histórica larga (TICKER × ANO × indicadores)
- `snapshot_atual.csv` — uma linha por ticker com preço do último pregão
- `data_quality_report.csv` — flags de qualidade por ticker
- `synetra.log` — log estruturado com rotação em 10 MB

## Quality Gates

```bash
# Lint
uv run ruff check synetra/ tests/ main.py

# Type check estrito
uv run mypy synetra/

# Testes com cobertura
uv run pytest --cov=synetra --cov-report=term-missing

# Scanner de segurança no código
uv run bandit -r synetra/

# Auditoria de CVEs nas dependências
uv run pip-audit
```

## Navegação

- [Wiki Técnica](WIKI.md) — fórmulas, mapeamento de contas CVM, regras de assepsia setorial, lógica de resolução de lucro, detecção de contas especiais via regex, Beneish termo a termo, valuation, data quality. [English version](WIKI_EN.md).
- [README](../README.md) — visão geral e setup
- [Mapeamento de contas por setor](WIKI.md#mapeamento-de-contas-por-setor) — qual código CVM vira qual nome interno, por setor
- [Indicadores por tier](WIKI.md#indicadores-por-tier-domain-layer) — fórmulas de Tiers 1 a 5
- [Assepsia setorial consolidada](WIKI.md#assepsia-setorial--resumo-consolidado) — o que é calculado em cada setor

---

!!! tip "Sobre este site"
    Gerado com MkDocs Material. O WIKI é a referência canônica — toda fórmula citada lá aponta para o arquivo e a função onde está implementada.

Engine: Polars + Rust · Licença: ver [LICENSE](https://github.com/synetra/synetra/blob/main/LICENSE)
