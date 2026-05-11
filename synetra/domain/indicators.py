"""Camada de Domínio: Regras de Negócio e Cálculos Financeiros.

Pipeline em tiers (dependência em cadeia):

    Tier 1 → Rentabilidade, margens, bases independentes.
    Tier 2 → Fluxo de caixa e eficiência (depende de CAPEX, Depreciação).
    Tier 3 → Estrutura de capital (depende de EBITDA).
    Tier 4 → Alavancagem avançada (depende de Dívida Total).
    Tier 5 → Ratios finais (depende de Dívida Líquida e EBITDA).

Segregação setorial (assepsia):

    Indústrias, Financeiras e Seguradoras têm lógicas contábeis distintas.
    Indicadores que não fazem sentido para um setor retornam ``None``.

API Pública:
    - :func:`calculate_all_indicators` — aplica todos os tiers em sequência.
    - :func:`safe_div` — divisão segura (denominador zero → ``None``).
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

import polars as pl

# --- Constantes de Domínio ---

#: Alíquota nominal combinada IR (25%) + CSLL (9%) no Brasil.
#: Base legal: Lei 9.249/95 e Lei 9.316/96. Usada no cálculo de NOPAT.
BRAZIL_TAX_RATE: float = 0.34

#: Fator pós-impostos para converter EBIT em NOPAT.
BRAZIL_AFTER_TAX: float = 1 - BRAZIL_TAX_RATE

# Coeficientes do Altman Z''-Score para Emerging Markets (Altman, 2005).
# Fórmula: Z'' = 6.56·A + 3.26·B + 6.72·C + 1.05·D
#   A = Capital de Giro / Ativo Total
#   B = Lucros Retidos (proxy: PL) / Ativo Total
#   C = EBIT / Ativo Total
#   D = Valor Contábil PL / Passivo Total
ALTMAN_COEF_WC_TA: float = 6.56
ALTMAN_COEF_RE_TA: float = 3.26
ALTMAN_COEF_EBIT_TA: float = 6.72
ALTMAN_COEF_BV_TL: float = 1.05


class Categoria(StrEnum):
    """Classificação setorial das empresas com regras contábeis distintas.

    - ``INDUSTRIAL``: maioria das empresas não-financeiras.
    - ``FINANCEIRO``: bancos (sem CAPEX, sem dívida operacional convencional).
    - ``SEGURADORA``: modelo contábil próprio (passivo = reservas técnicas).
    """

    INDUSTRIAL = "INDUSTRIAL"
    FINANCEIRO = "FINANCEIRO"
    SEGURADORA = "SEGURADORA"


# --- Helpers de Expressão ---

def safe_div(num: str | pl.Expr, den: str | pl.Expr) -> pl.Expr:
    """Divisão segura que retorna ``None`` quando o denominador é zero.

    Args:
        num: Nome da coluna ou expressão do numerador.
        den: Nome da coluna ou expressão do denominador.

    Returns:
        Expressão Polars que avalia ``num / den``, ou ``None`` quando ``den == 0``.
    """
    numerator = pl.col(num) if isinstance(num, str) else num
    denominator = pl.col(den) if isinstance(den, str) else den
    return numerator / pl.when(denominator == 0).then(None).otherwise(denominator)


def only_for(category: Categoria, expression: pl.Expr) -> pl.Expr:
    """Aplica ``expression`` apenas para empresas da categoria indicada.

    Args:
        category: Categoria setorial que deve receber o cálculo.
        expression: Expressão Polars a aplicar quando a categoria bate.

    Returns:
        Expressão que retorna ``expression`` se ``CATEGORIA == category``,
        caso contrário ``None``.
    """
    return (
        pl.when(pl.col("CATEGORIA") == category.value)
        .then(expression)
        .otherwise(None)
    )


def by_category(
    *,
    industrial: pl.Expr | None = None,
    seguradora: pl.Expr | None = None,
    financeiro: pl.Expr | None = None,
    default: pl.Expr | None = None,
) -> pl.Expr:
    """Aplica uma expressão diferente por categoria setorial.

    Útil para indicadores como DIVIDA_TOTAL cujo cálculo muda conforme o setor.

    Args:
        industrial: Expressão para ``CATEGORIA == 'INDUSTRIAL'``.
        seguradora: Expressão para ``CATEGORIA == 'SEGURADORA'``.
        financeiro: Expressão para ``CATEGORIA == 'FINANCEIRO'``.
        default: Expressão aplicada quando nenhuma categoria bate.

    Returns:
        Expressão condicional ``when/then/otherwise`` encadeada.
    """
    fallback = default if default is not None else pl.lit(None)
    branches: list[tuple[Categoria, pl.Expr]] = []
    if industrial is not None:
        branches.append((Categoria.INDUSTRIAL, industrial))
    if seguradora is not None:
        branches.append((Categoria.SEGURADORA, seguradora))
    if financeiro is not None:
        branches.append((Categoria.FINANCEIRO, financeiro))

    if not branches:
        return fallback

    # Polars encadeia via `When/Then` interno, mas o stub público expõe apenas
    # `pl.Expr`. Usamos `Any` para navegar pelo builder sem brigar com o mypy.
    first_cat, first_expr = branches[0]
    builder: Any = pl.when(pl.col("CATEGORIA") == first_cat.value).then(first_expr)
    for cat, then_expr in branches[1:]:
        builder = builder.when(pl.col("CATEGORIA") == cat.value).then(then_expr)
    return builder.otherwise(fallback)


# --- Tier 1: Rentabilidade e Bases ---

def _profitability_ratios() -> list[pl.Expr]:
    """ROE, ROA e métricas por ação (LPA, VPA)."""
    return [
        safe_div("LUCRO_FINAL", "PATRIMONIO_LIQUIDO").alias("ROE"),
        safe_div("LUCRO_FINAL", "ATIVO_TOTAL").alias("ROA"),
        safe_div("LUCRO_FINAL", "QTDE_ACOES").alias("LPA"),
        safe_div("PATRIMONIO_LIQUIDO", "QTDE_ACOES").alias("VPA"),
    ]


def _efficiency_ratios() -> list[pl.Expr]:
    """Giro do ativo, alavancagem de longo prazo e qualidade de lucros."""
    return [
        safe_div("RECEITA_LIQUIDA", "ATIVO_TOTAL").alias("GIRO_ATIVO"),
        safe_div("DIVIDA_LP", "ATIVO_TOTAL").alias("ALAVANCAGEM_LP"),
        (pl.col("LUCRO_FINAL") - pl.col("FCO")).alias("ACCRUALS"),
        only_for(
            Categoria.INDUSTRIAL,
            safe_div(pl.col("LUCRO_FINAL") - pl.col("FCO"), "ATIVO_TOTAL"),
        ).alias("ACCRUAL_RATIO"),
        only_for(
            Categoria.INDUSTRIAL,
            safe_div("RESULTADO_BRUTO", "ATIVO_TOTAL"),
        ).alias("GP_A"),
    ]


def _margin_ratios() -> list[pl.Expr]:
    """Margens operacionais: EBIT, Líquida e Bruta."""
    return [
        safe_div("EBIT", "RECEITA_LIQUIDA").alias("MARGEM_EBIT"),
        safe_div("LUCRO_FINAL", "RECEITA_LIQUIDA").alias("MARGEM_LIQUIDA"),
        safe_div("RESULTADO_BRUTO", "RECEITA_LIQUIDA").alias("MARGEM_BRUTA"),
    ]


def _sector_isolated_values() -> list[pl.Expr]:
    """Valores absolutos que precisam de isolamento setorial."""
    capex_for_non_financials = (
        pl.when(pl.col("CATEGORIA") == Categoria.FINANCEIRO.value)
        .then(None)
        .otherwise(pl.col("CAPEX_VAL"))
        .alias("CAPEX")
    )
    return [
        capex_for_non_financials,
        pl.col("DEPREC_AMORT").alias("DEPREC_AMORT"),
        pl.col("DIVIDENDOS_PAGOS").abs().alias("PROVENTOS"),
    ]


def get_tier1_expressions() -> list[pl.Expr]:
    """Tier 1: Rentabilidade, margens, eficiência e bases setoriais.

    Agrupa ROE, ROA, margens, giro, alavancagem, accruals e valores
    setor-específicos (CAPEX, depreciação, proventos).
    """
    return [
        *_profitability_ratios(),
        *_efficiency_ratios(),
        *_margin_ratios(),
        *_sector_isolated_values(),
    ]


# --- Tier 2: Fluxo de Caixa e Eficiência ---

def _free_cash_flow() -> pl.Expr:
    """Fluxo de Caixa Livre.

    - Indústrias/Seguradoras: ``FCO + CAPEX`` (CAPEX é negativo na DFC).
    - Financeiras: ``FCO + FCI`` (bancos não têm CAPEX tradicional).
    """
    return by_category(
        financeiro=pl.col("FCO") + pl.col("FCI"),
        default=pl.col("FCO") + pl.col("CAPEX"),
    ).alias("FCL")


def _ebitda() -> pl.Expr:
    """EBITDA aproximado = EBIT + Depreciação/Amortização."""
    depreciation = pl.col("DEPREC_AMORT").fill_null(0).abs()
    return (pl.col("EBIT") + depreciation).alias("EBITDA")


def get_tier2_expressions() -> list[pl.Expr]:
    """Tier 2: FCL, Payout e EBITDA."""
    return [
        _free_cash_flow(),
        safe_div("PROVENTOS", "LUCRO_FINAL").alias("PAYOUT"),
        _ebitda(),
    ]


# --- Tier 3: Estrutura de Capital e Dívida Base ---

def _total_debt() -> pl.Expr:
    """Dívida bruta = Dívida CP + Dívida LP.

    - Indústria: soma natural.
    - Seguradora: zero (passivo é reserva técnica, não dívida).
    - Financeiro: ``None`` (conceito não se aplica).
    """
    return by_category(
        industrial=pl.col("DIVIDA_CP") + pl.col("DIVIDA_LP"),
        seguradora=pl.lit(0.0),
    ).alias("DIVIDA_TOTAL")


def _current_ratio() -> pl.Expr:
    """Liquidez Corrente = Ativo Circulante / Passivo Circulante (só indústria)."""
    return only_for(
        Categoria.INDUSTRIAL,
        safe_div("ATIVO_CIRCULANTE", "PASSIVO_CIRCULANTE"),
    ).alias("LIQUIDEZ_CORRENTE")


def get_tier3_expressions() -> list[pl.Expr]:
    """Tier 3: Margem EBITDA, Dívida Total e Liquidez Corrente."""
    return [
        safe_div("EBITDA", "RECEITA_LIQUIDA").alias("MARGEM_EBITDA"),
        _total_debt(),
        _current_ratio(),
    ]


# --- Tier 4: Alavancagem Avançada ---

def _net_debt() -> pl.Expr:
    """Dívida Líquida = Dívida Total - Caixa e Equivalentes."""
    return by_category(
        industrial=pl.col("DIVIDA_TOTAL") - pl.col("CAIXA_EQUIVALENTES"),
        seguradora=pl.lit(0.0) - pl.col("CAIXA_EQUIVALENTES"),
    ).alias("DIVIDA_LIQUIDA")


def _debt_to_equity() -> pl.Expr:
    """Alavancagem Dívida/PL = Dívida Total / Patrimônio Líquido."""
    return by_category(
        industrial=safe_div("DIVIDA_TOTAL", "PATRIMONIO_LIQUIDO"),
        seguradora=pl.lit(0.0),
    ).alias("DIVIDA_PL")


def get_tier4_expressions() -> list[pl.Expr]:
    """Tier 4: Dívida Líquida e Alavancagem Dívida/PL."""
    return [_net_debt(), _debt_to_equity()]


# --- Tier 5: Ratios Finais ---

def _debt_coverage() -> pl.Expr:
    """Cobertura da Dívida = Dívida Líquida / EBITDA."""
    return (
        pl.when(pl.col("CATEGORIA").is_in([Categoria.INDUSTRIAL.value, Categoria.SEGURADORA.value]))
        .then(safe_div("DIVIDA_LIQUIDA", "EBITDA"))
        .otherwise(None)
        .alias("DL_EBITDA")
    )


def _roic() -> pl.Expr:
    """Return on Invested Capital (fórmula Damodaran).

    .. math::
        ROIC = \\frac{EBIT \\times (1 - tax)}{PL + D\\text{ívida Bruta}}

    Mede o retorno sobre capital total dos financiadores (acionistas + credores).
    Padrão CFA Institute e McKinsey. Aplicável apenas a empresas industriais.
    """
    nopat = pl.col("EBIT") * BRAZIL_AFTER_TAX
    invested_capital = pl.col("PATRIMONIO_LIQUIDO") + pl.col("DIVIDA_TOTAL")
    safe_invested_capital = (
        pl.when(invested_capital == 0).then(None).otherwise(invested_capital)
    )
    return only_for(Categoria.INDUSTRIAL, nopat / safe_invested_capital).alias("ROIC")


def _altman_z_score() -> pl.Expr:
    """Altman Z''-Score para Emerging Markets (Altman, 2005).

    .. math::
        Z'' = 6.56 \\cdot A + 3.26 \\cdot B + 6.72 \\cdot C + 1.05 \\cdot D

    Onde:
        - A = (Ativo Circulante − Passivo Circulante) / Ativo Total
        - B = Patrimônio Líquido / Ativo Total
        - C = EBIT / Ativo Total
        - D = Patrimônio Líquido / Passivo Total

    Zonas de interpretação:
        - Z'' > 2.60 → Zona Segura (baixo risco de falência)
        - 1.10 < Z'' ≤ 2.60 → Zona Cinza
        - Z'' ≤ 1.10 → Zona de Risco (alta probabilidade de distress)
    """
    working_capital = pl.col("ATIVO_CIRCULANTE") - pl.col("PASSIVO_CIRCULANTE")
    total_liabilities = pl.col("ATIVO_TOTAL") - pl.col("PATRIMONIO_LIQUIDO")
    safe_total_liabilities = (
        pl.when(total_liabilities == 0).then(None).otherwise(total_liabilities)
    )

    term_a = ALTMAN_COEF_WC_TA * safe_div(working_capital, "ATIVO_TOTAL")
    term_b = ALTMAN_COEF_RE_TA * safe_div("PATRIMONIO_LIQUIDO", "ATIVO_TOTAL")
    term_c = ALTMAN_COEF_EBIT_TA * safe_div("EBIT", "ATIVO_TOTAL")
    term_d = ALTMAN_COEF_BV_TL * safe_div("PATRIMONIO_LIQUIDO", safe_total_liabilities)

    return only_for(Categoria.INDUSTRIAL, term_a + term_b + term_c + term_d).alias("ALTMAN_Z")


def get_tier5_expressions() -> list[pl.Expr]:
    """Tier 5: DL/EBITDA, ROIC e Altman Z-Score."""
    return [_debt_coverage(), _roic(), _altman_z_score()]


# --- API Pública ---

def calculate_all_indicators(df: pl.DataFrame) -> pl.DataFrame:
    """Aplica todos os tiers de indicadores financeiros em sequência.

    A ordem de execução respeita as dependências entre tiers:
    Tier 1 → 2 → 3 → 4 → 5.

    Args:
        df: DataFrame com dados contábeis brutos (CVM).

    Returns:
        DataFrame enriquecido com todos os indicadores calculados.
    """
    return (
        df.with_columns(get_tier1_expressions())
        .with_columns(get_tier2_expressions())
        .with_columns(get_tier3_expressions())
        .with_columns(get_tier4_expressions())
        .with_columns(get_tier5_expressions())
    )
