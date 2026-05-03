# 🌌 Synetra (v1.0.0)
[Leia em Português] | [Read in English](README_EN.md)
> **Financial Intelligence Pipeline** | ETL de alta performance para dados da CVM com Polars.

![Versão](https://img.shields.io/badge/version-1.0.0-blueviolet)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![Engine](https://img.shields.io/badge/Engine-Polars_&_Rust-yellow)
![Tests](https://img.shields.io/badge/tests-38_passed-success)
![Data](https://img.shields.io/badge/Data-CVM--Open--Data-orange)

---

### 🌐 [PT-BR] Português

O **Synetra** é um motor de processamento de dados financeiros desenhado para extrair, limpar e transformar o volume massivo de dados brutos da CVM (Comissão de Valores Mobiliários) em uma base de indicadores fundamentalistas pronta para análise institucional.

A versão **1.0.0** representa um salto de engenharia, transformando o Synetra de um script analítico em um produto robusto, com tipagem estrita (Pydantic), suíte de testes (TDD) e uma refatoração de elite do motor Rust/Polars.

#### ⚡ Performance (Benchmark v1.0.0)
O coração do Synetra foi refatorado utilizando padrões de *Single-Evaluation* e *Global Joins* do ecossistema Polars:
- **Tempo de Cálculo de 16 Anos de Dados (5.200+ registros):** ~3.2 segundos.
- **Vetorização Extrema:** Remoção total do Pandas. O motor de transformação processa todos os cálculos matemáticos nativamente na camada C++/Rust.

#### 🛠️ Novidades da v1.0.0 (Minor Update)
- **Refatoração de Elite:** Substituição de iterações Python por Comprehensions paralelas e Joins unificados no `transformer.py`.
- **Validação Estrita (Pydantic):** O arquivo `parameters.toml` agora possui uma barreira de segurança que aborta execuções com configurações inválidas.
- **Financial Auditor Integrado:** O pipeline agora audita a si mesmo no final da execução, detectando gaps temporais e ROEs astronômicos gerados por distorções de Patrimônio Líquido negativo.
- **Suíte de Testes Unitários:** 38 testes implementados via `pytest` cobrindo 100% da lógica matemática (ROE, F-Score, Divisões Seguras).

#### 🏗️ Arquitetura do Sistema

```mermaid
graph TD
    A[CVM Open Data FTP] -->|Download Paralelo| B(Downloader)
    B -->|Grava Cache| C[(.synetra_cache)]
    C -->|Lazy Load| D(Loader)
    D -->|Vetorização| E(Transformer)
    E -->|Mapeamento TOML| E
    E --> F[serie_historica_financeira.csv]
    F --> G{Financial Auditor}
    G -->|Gera Logs de Qualidade| H[synetra.log]
```

#### 📂 Estrutura do Projeto
- `synetra/`: Pacote principal com os módulos de inteligência.
- `main.py`: O orquestrador de execução com auditoria integrada.
- `parameters.toml`: Configuração centralizada (regras de negócio e mapeamento de contas).
- `tests/`: Suíte de testes unitários para o motor financeiro.

#### 🚀 Como Executar
1. Instale as dependências: `pip install -r requirements.txt`
2. Revise/configure o mapeamento no `parameters.toml`.
3. Execute o pipeline:
   ```bash
   python main.py
   ```
4. Verifique os avisos de Data Quality no arquivo `synetra.log`.

