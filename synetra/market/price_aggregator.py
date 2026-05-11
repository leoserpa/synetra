"""Agregador de cotações diárias em valores anuais.

Responsabilidades:

    1. Transformar série diária em valores anuais (preço fim de ano + médio).
    2. Anexar colunas de preço à série histórica do Synetra via (TICKER, ANO).
    3. Calcular múltiplos ANUAIS na série histórica (P/L, P/VP, MC por ano).
    4. Gerar snapshot ATUAL (1 linha por ticker, múltiplos com preço de hoje).

Arquitetura de dados:

    - ``serie_historica_financeira.csv`` → 1 linha por (ticker, ano)
      Colunas: ``MARKET_CAP``, ``P_L``, ``P_VP`` (valores do próprio ano).

    - ``snapshot_atual.csv`` → 1 linha por ticker (último ano fiscal)
      Colunas: ``MARKET_CAP_ATUAL``, ``P_L_ATUAL``, ``P_VP_ATUAL``,
      ``P_RECEITA``, ``EV_EBITDA``, ``EV_RECEITA``, ``EARNINGS_YIELD``
      (todos com o último preço de fechamento disponível).

A separação em dois artefatos evita que o valuation histórico fique
contaminado pelo preço de hoje, e evita que o screening atual precise
carregar a série inteira.
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl
from loguru import logger

from synetra.domain import Categoria

# --- Constantes de domínio ---

#: Sufixos padronizados usados nas colunas B3 para identificar classe de ação.
_TICKER_SUFFIX_ON = "3"
"""Final de ticker indicativo de ações ordinárias (ex: PETR3)."""

_TICKER_SUFFIX_UNIT = "11"
"""Final de ticker indicativo de Units (ex: SANB11)."""

#: Casas decimais padrão para cada tipo de coluna
_ROUND_PRICE_MULTIPLES = 2  # P/L, P/VP, P/Receita, EV múltiplos, preços
_ROUND_YIELD = 4            # Earnings Yield (fração decimal)
_ROUND_MARKET_CAP = 0       # Market Cap (valor absoluto em R$)

# --- Agregação diária → anual ---


def aggregate_to_yearly(df_precos_diarios: pl.DataFrame) -> pl.DataFrame:
    """Agrega cotações diárias em valores anuais.

    Args:
        df_precos_diarios: DataFrame com colunas ``TICKER``, ``DATA`` (string),
            ``FECHAMENTO``, ``VOLUME``.

    Returns:
        DataFrame com colunas ``TICKER``, ``ANO``, ``PRECO_FIM_ANO``,
        ``PRECO_MEDIO_ANO``, ``VOLUME_MEDIO``.
    """
    if df_precos_diarios.is_empty():
        return pl.DataFrame()

    df = df_precos_diarios.with_columns(
        pl.col("DATA").str.to_date().alias("DATA_D")
    ).with_columns(pl.col("DATA_D").dt.year().alias("ANO"))

    # PRECO_FIM_ANO: último fechamento do ano (31/dez)
    # PRECO_MEDIO_ANO: média aritmética
    # VOLUME_MEDIO: volume médio diário
    agregado = (
        df.sort(["TICKER", "DATA_D"])
        .group_by(["TICKER", "ANO"])
        .agg(
            [
                pl.col("FECHAMENTO").last().alias("PRECO_FIM_ANO"),
                pl.col("FECHAMENTO").mean().alias("PRECO_MEDIO_ANO"),
                pl.col("VOLUME").mean().alias("VOLUME_MEDIO"),
            ]
        )
        .sort(["TICKER", "ANO"])
    )

    agregado = agregado.with_columns(
        [
            pl.col("PRECO_FIM_ANO").round(_ROUND_PRICE_MULTIPLES),
            pl.col("PRECO_MEDIO_ANO").round(_ROUND_PRICE_MULTIPLES),
            pl.col("VOLUME_MEDIO").round(0),
        ]
    )

    logger.info("Agregação anual: {} tickers × anos", agregado.height)
    return agregado


def attach_prices_to_history(
    df_history: pl.DataFrame,
    df_precos_diarios: pl.DataFrame,
) -> pl.DataFrame:
    """Anexa colunas de preço anual ao DataFrame da série histórica.

    Args:
        df_history: Série histórica do Synetra com ``TICKER`` e ``ANO``.
        df_precos_diarios: Cotações diárias do Yahoo.

    Returns:
        Série histórica com ``PRECO_FIM_ANO``, ``PRECO_MEDIO_ANO``, ``VOLUME_MEDIO``.
    """
    if df_precos_diarios.is_empty():
        logger.warning("Preços vazios — retornando série histórica sem colunas de preço.")
        return df_history.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("PRECO_FIM_ANO"),
                pl.lit(None, dtype=pl.Float64).alias("PRECO_MEDIO_ANO"),
                pl.lit(None, dtype=pl.Float64).alias("VOLUME_MEDIO"),
            ]
        )

    df_anual = aggregate_to_yearly(df_precos_diarios).with_columns(
        pl.col("ANO").cast(pl.Int32)
    )
    df_history = df_history.with_columns(pl.col("ANO").cast(pl.Int32))

    resultado = df_history.join(df_anual, on=["TICKER", "ANO"], how="left")

    matches = resultado.filter(pl.col("PRECO_FIM_ANO").is_not_null()).height
    total = resultado.height
    pct = (matches / total * 100) if total > 0 else 0
    logger.info(
        "Merge contábil + preços: {} de {} linhas ({:.1f}%) têm cotação",
        matches, total, pct,
    )
    return resultado


# --- Classificação de Tickers (ON/PN/UNIT) ---


def _classify_ticker_class(ticker_col: str = "TICKER") -> pl.Expr:
    """Classifica o ticker em ``ON``, ``PN`` ou ``UNIT`` conforme regra B3.

    Regra B3:
        - Final ``3`` → ``ON`` (Ordinária).
        - Final ``4``, ``5``, ``6``, ``7``, ``8`` → ``PN`` (Preferencial).
        - Final ``11`` → ``UNIT`` (pacote de ações).

    Args:
        ticker_col: Nome da coluna com o ticker.

    Returns:
        Expressão Polars retornando ``'ON'``, ``'PN'`` ou ``'UNIT'``.
    """
    return (
        pl.when(pl.col(ticker_col).str.slice(-1) == _TICKER_SUFFIX_ON)
        .then(pl.lit("ON"))
        .when(pl.col(ticker_col).str.slice(-2) == _TICKER_SUFFIX_UNIT)
        .then(pl.lit("UNIT"))
        .otherwise(pl.lit("PN"))
    )


# --- Market Cap Consolidado (ON + PN por empresa) ---


def _compute_consolidated_market_cap(
    df: pl.DataFrame,
    price_col: str,
    output_col: str,
) -> pl.DataFrame:
    """Calcula Market Cap consolidado por empresa (``CNPJ_CIA`` + ``ANO``).

    Fórmula:

    .. math:: MC_{empresa} = \\sum_{classe} QTDE_{classe} \\times PRECO_{classe}

    Onde ``QTDE_classe`` vem do FRE (``QTDE_ON`` ou ``QTDE_PN``) e
    ``PRECO_classe`` é o preço do ticker daquela classe.

    Comportamento:
        - Empresas com ON e PN (ex: PETR3+PETR4): ``MC = ON·preço3 + PN·preço4``.
        - Empresas só com ON (ex: WEGE3): ``MC = QTDE_ON · preço3``.
        - Units: ``MC = QTDE_ACOES · preço_unit``.
        - O MESMO MC é broadcast para todos os tickers da mesma empresa.

    Args:
        df: DataFrame com ``TICKER``, ``CNPJ_CIA``, ``ANO``, ``QTDE_ACOES``,
            ``QTDE_ON``, ``QTDE_PN`` e a coluna de preço indicada.
        price_col: Nome da coluna de preço (ex: ``"PRECO_FIM_ANO"``).
        output_col: Nome da coluna de saída (ex: ``"MARKET_CAP"``).

    Returns:
        DataFrame com ``output_col`` adicionada (consolidado por empresa).
    """
    df = df.with_columns(_classify_ticker_class().alias("_TICKER_CLASSE"))

    prices_wide = _build_prices_per_class(df, price_col)

    if prices_wide.is_empty():
        return df.drop("_TICKER_CLASSE").with_columns(
            pl.lit(None, dtype=pl.Float64).alias(output_col)
        )

    df = df.join(prices_wide, on=["CNPJ_CIA", "ANO"], how="left")

    mc_consolidated = _consolidated_market_cap_expr()
    mc_fallback = pl.col("QTDE_ACOES").fill_null(0) * pl.col(price_col).fill_null(0)

    df = df.with_columns(
        pl.when(mc_consolidated > 0)
        .then(mc_consolidated)
        .when(mc_fallback > 0)
        .then(mc_fallback)
        .otherwise(None)
        .alias(output_col)
    )

    aux_cols = ["_TICKER_CLASSE", "_PRECO_ON", "_PRECO_PN", "_PRECO_UNIT"]
    return df.drop([c for c in aux_cols if c in df.columns])


def _build_prices_per_class(df: pl.DataFrame, price_col: str) -> pl.DataFrame:
    """Pivota preços por classe (ON/PN/UNIT) dentro de cada empresa-ano.

    Resultado esperado tem colunas: ``CNPJ_CIA``, ``ANO``, ``_PRECO_ON``,
    ``_PRECO_PN``, ``_PRECO_UNIT`` (com nulls onde a classe não existe).
    """
    prices_per_class = (
        df.filter(pl.col(price_col).is_not_null())
        .group_by(["CNPJ_CIA", "ANO", "_TICKER_CLASSE"])
        .agg(pl.col(price_col).max().alias("_PRECO_CLASSE"))
    )

    if prices_per_class.is_empty():
        return prices_per_class

    prices_wide = prices_per_class.pivot(
        index=["CNPJ_CIA", "ANO"],
        on="_TICKER_CLASSE",
        values="_PRECO_CLASSE",
    )

    # Garantir que todas as classes existam como coluna
    for classe in ("ON", "PN", "UNIT"):
        if classe not in prices_wide.columns:
            prices_wide = prices_wide.with_columns(
                pl.lit(None, dtype=pl.Float64).alias(classe)
            )

    return prices_wide.rename(
        {"ON": "_PRECO_ON", "PN": "_PRECO_PN", "UNIT": "_PRECO_UNIT"}
    )


def _consolidated_market_cap_expr() -> pl.Expr:
    """Expressão ``QTDE_ON · PRECO_ON + QTDE_PN · PRECO_PN``."""
    mc_on = pl.col("QTDE_ON").fill_null(0) * pl.col("_PRECO_ON").fill_null(0)
    mc_pn = pl.col("QTDE_PN").fill_null(0) * pl.col("_PRECO_PN").fill_null(0)
    return mc_on + mc_pn


# --- Valuation (usado por histórico e snapshot) ---


@dataclass(frozen=True)
class ValuationContext:
    """Configura qual preço usar e o sufixo das colunas de valuation.

    Attributes:
        price_col: Coluna de preço (ex: ``"PRECO_FIM_ANO"`` ou ``"PRECO_ATUAL"``).
        market_cap_col: Nome da coluna de MC produzida.
        pl_col: Nome da coluna P/L produzida.
        pvp_col: Nome da coluna P/VP produzida.
    """

    price_col: str
    market_cap_col: str
    pl_col: str
    pvp_col: str


#: Contexto para valuation histórico (preço fim do ano + múltiplos sem sufixo).
_HISTORICAL_CONTEXT = ValuationContext(
    price_col="PRECO_FIM_ANO",
    market_cap_col="MARKET_CAP",
    pl_col="P_L",
    pvp_col="P_VP",
)

#: Contexto para snapshot atual (preço de hoje + sufixo ``_ATUAL``).
_CURRENT_CONTEXT = ValuationContext(
    price_col="PRECO_ATUAL",
    market_cap_col="MARKET_CAP_ATUAL",
    pl_col="P_L_ATUAL",
    pvp_col="P_VP_ATUAL",
)

#: Indicadores que NÃO se aplicam a bancos/seguradoras.
_ANULAR_FINANCEIRO_SEGURADORA: list[str] = ["EV_EBITDA", "EV_RECEITA"]

#: Colunas de valuation anuais produzidas na série histórica.
VALUATION_COLS: list[str] = [
    "MARKET_CAP",
    "P_L",
    "P_VP",
    "P_RECEITA",
    "EARNINGS_YIELD",
    "EV_EBITDA",
    "EV_RECEITA",
]


def _positive_price_and_qty(price: pl.Expr, *qty: pl.Expr) -> pl.Expr:
    """Predicado ``price > 0 AND todas qty > 0``."""
    condition = price > 0
    for q in qty:
        condition = condition & (q > 0)
    return condition


def _price_ratio(
    price: pl.Expr, per_share_metric: pl.Expr, qty_shares: pl.Expr
) -> pl.Expr:
    """Razão preço / métrica-por-ação, com guardas contra divisão inválida."""
    return (
        pl.when(_positive_price_and_qty(price, qty_shares, per_share_metric))
        .then(price / per_share_metric)
        .otherwise(None)
    )


def _yield_ratio(
    price: pl.Expr, per_share_metric: pl.Expr, qty_shares: pl.Expr
) -> pl.Expr:
    """Rendimento = métrica-por-ação / preço (inverso de P/L)."""
    return (
        pl.when(_positive_price_and_qty(price, qty_shares, per_share_metric))
        .then(per_share_metric / price)
        .otherwise(None)
    )


def _compute_market_cap(df: pl.DataFrame, ctx: ValuationContext) -> pl.DataFrame:
    """Calcula Market Cap consolidado (ON+PN) ou fallback simples."""
    has_on_pn = "QTDE_ON" in df.columns and "QTDE_PN" in df.columns

    if ctx.price_col not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias(ctx.market_cap_col))

    if has_on_pn and "CNPJ_CIA" in df.columns:
        return _compute_consolidated_market_cap(
            df, price_col=ctx.price_col, output_col=ctx.market_cap_col
        )

    # Fallback simples: preço × qtde_acoes total
    price = pl.col(ctx.price_col)
    qty = pl.col("QTDE_ACOES")
    return df.with_columns(
        pl.when((price > 0) & (qty > 0))
        .then(price * qty)
        .otherwise(None)
        .alias(ctx.market_cap_col)
    )


def _compute_per_share_multiples(
    df: pl.DataFrame, ctx: ValuationContext
) -> pl.DataFrame:
    """Calcula P/L, P/VP e Earnings Yield usando o preço do contexto.

    Earnings Yield só é produzido no contexto histórico. No snapshot atual,
    ele é calculado junto com os múltiplos ``_ATUAL`` para manter o nome
    padronizado (não ``EARNINGS_YIELD_ATUAL``).
    """
    exprs: list[pl.Expr] = []
    price = pl.col(ctx.price_col)
    qty = pl.col("QTDE_ACOES")

    has_lpa_inputs = all(
        c in df.columns for c in (ctx.price_col, "LUCRO_LIQUIDO", "QTDE_ACOES")
    )
    if has_lpa_inputs:
        lpa = pl.col("LUCRO_LIQUIDO") / qty
        exprs.append(_price_ratio(price, lpa, qty).alias(ctx.pl_col))
        exprs.append(_yield_ratio(price, lpa, qty).alias("EARNINGS_YIELD"))
    else:
        exprs.append(pl.lit(None, dtype=pl.Float64).alias(ctx.pl_col))
        exprs.append(pl.lit(None, dtype=pl.Float64).alias("EARNINGS_YIELD"))

    has_vpa_inputs = all(
        c in df.columns for c in (ctx.price_col, "PATRIMONIO_LIQUIDO", "QTDE_ACOES")
    )
    if has_vpa_inputs:
        vpa = pl.col("PATRIMONIO_LIQUIDO") / qty
        exprs.append(_price_ratio(price, vpa, qty).alias(ctx.pvp_col))
    else:
        exprs.append(pl.lit(None, dtype=pl.Float64).alias(ctx.pvp_col))

    return df.with_columns(exprs)


def _compute_enterprise_value_multiples(
    df: pl.DataFrame, ctx: ValuationContext
) -> pl.DataFrame:
    """P/Receita, EV/EBITDA e EV/Receita (todos derivados do Market Cap)."""
    mc = pl.col(ctx.market_cap_col)
    ev = mc + pl.col("DIVIDA_LIQUIDA").fill_null(0)

    exprs: list[pl.Expr] = [
        _safe_ratio(
            numerator=mc,
            denominator=pl.col("RECEITA_LIQUIDA"),
            condition=(pl.col("RECEITA_LIQUIDA") > 0) & (mc > 0),
            required_cols=("RECEITA_LIQUIDA", ctx.market_cap_col),
            df_columns=df.columns,
        ).alias("P_RECEITA"),
        _safe_ratio(
            numerator=ev,
            denominator=pl.col("EBITDA"),
            condition=(pl.col("EBITDA") > 0) & (mc > 0),
            required_cols=("EBITDA", "DIVIDA_LIQUIDA", ctx.market_cap_col),
            df_columns=df.columns,
        ).alias("EV_EBITDA"),
        _safe_ratio(
            numerator=ev,
            denominator=pl.col("RECEITA_LIQUIDA"),
            condition=(pl.col("RECEITA_LIQUIDA") > 0) & (mc > 0),
            required_cols=("RECEITA_LIQUIDA", "DIVIDA_LIQUIDA", ctx.market_cap_col),
            df_columns=df.columns,
        ).alias("EV_RECEITA"),
    ]
    return df.with_columns(exprs)


def _safe_ratio(
    numerator: pl.Expr,
    denominator: pl.Expr,
    condition: pl.Expr,
    required_cols: tuple[str, ...],
    df_columns: list[str],
) -> pl.Expr:
    """Retorna ``num/den`` sob condição, ou ``None`` se faltar coluna exigida."""
    if any(c not in df_columns for c in required_cols):
        return pl.lit(None, dtype=pl.Float64)
    return pl.when(condition).then(numerator / denominator).otherwise(None)


def _apply_sectorial_assepsia(df: pl.DataFrame) -> pl.DataFrame:
    """Anula ``EV_EBITDA`` e ``EV_RECEITA`` para bancos e seguradoras.

    EV não faz sentido pra bancos porque dívida é matéria-prima do negócio,
    não financiamento operacional.
    """
    if "CATEGORIA" not in df.columns:
        return df

    cols_to_nullify = [c for c in _ANULAR_FINANCEIRO_SEGURADORA if c in df.columns]
    if not cols_to_nullify:
        return df

    is_financial_or_insurance = pl.col("CATEGORIA").is_in(
        [Categoria.FINANCEIRO.value, Categoria.SEGURADORA.value]
    )
    return df.with_columns(
        [
            pl.when(is_financial_or_insurance).then(None).otherwise(pl.col(c)).alias(c)
            for c in cols_to_nullify
        ]
    )


def _round_valuation_columns(
    df: pl.DataFrame, price_col: str, market_cap_col: str, pl_col: str, pvp_col: str
) -> pl.DataFrame:
    """Arredonda múltiplos ao padrão: preço/ratios 2d, yield 4d, MC 0d."""
    round_2d = [price_col, pl_col, pvp_col, "P_RECEITA", "EV_EBITDA", "EV_RECEITA"]
    round_4d = ["EARNINGS_YIELD"]
    round_0d = [market_cap_col]

    return df.with_columns(
        [pl.col(c).round(_ROUND_PRICE_MULTIPLES) for c in round_2d if c in df.columns]
        + [pl.col(c).round(_ROUND_YIELD) for c in round_4d if c in df.columns]
        + [pl.col(c).round(_ROUND_MARKET_CAP) for c in round_0d if c in df.columns]
    )


def _compute_valuation(df: pl.DataFrame, ctx: ValuationContext) -> pl.DataFrame:
    """Pipeline de valuation genérico: MC → múltiplos por ação → EV → assepsia → round."""
    df = _compute_market_cap(df, ctx)
    df = _compute_per_share_multiples(df, ctx)
    df = _compute_enterprise_value_multiples(df, ctx)
    df = _apply_sectorial_assepsia(df)
    df = _round_valuation_columns(
        df,
        price_col=ctx.price_col,
        market_cap_col=ctx.market_cap_col,
        pl_col=ctx.pl_col,
        pvp_col=ctx.pvp_col,
    )
    return df


# --- API pública: Valuation Histórico ---


def attach_historical_valuation(df_history: pl.DataFrame) -> pl.DataFrame:
    """Calcula múltiplos de valuation HISTÓRICOS (preço fim do ano).

    Para cada linha ``(ticker, ano)``, calcula:

        - ``MARKET_CAP`` = ``PRECO_FIM_ANO × QTDE_ACOES`` (consolidado ON+PN).
        - ``P_L`` = ``PRECO_FIM_ANO / LPA``.
        - ``P_VP`` = ``PRECO_FIM_ANO / VPA``.
        - ``P_RECEITA`` = ``MARKET_CAP / RECEITA_LIQUIDA``.
        - ``EARNINGS_YIELD`` = ``LUCRO_LIQUIDO / MARKET_CAP`` (inverso do P/L).
        - ``EV_EBITDA`` = ``(MC + DL) / EBITDA`` (só industrial).
        - ``EV_RECEITA`` = ``(MC + DL) / RECEITA`` (só industrial).

    Todos os múltiplos usam o preço do próprio ano — valor de 2015 reflete
    o que a empresa valia em 2015, não o preço de hoje contaminando dados.

    Assepsia setorial: ``EV_EBITDA`` e ``EV_RECEITA`` = ``None`` para
    ``FINANCEIRO`` e ``SEGURADORA``.

    Args:
        df_history: Série histórica com ``TICKER``, ``ANO``, ``PRECO_FIM_ANO``,
            ``LUCRO_LIQUIDO``, ``PATRIMONIO_LIQUIDO``, ``RECEITA_LIQUIDA``,
            ``EBITDA``, ``DIVIDA_LIQUIDA``, ``QTDE_ACOES``, ``QTDE_ON``,
            ``QTDE_PN``, ``CNPJ_CIA``, ``CATEGORIA``.

    Returns:
        DataFrame com as colunas de ``VALUATION_COLS`` adicionadas.
    """
    df = _compute_valuation(df_history, _HISTORICAL_CONTEXT)

    if "MARKET_CAP" in df.columns:
        com_mc = df.filter(pl.col("MARKET_CAP").is_not_null()).height
        pct = (com_mc / df.height * 100) if df.height > 0 else 0
        logger.info(
            "Valuation histórico anual: {} de {} linhas ({:.1f}%) têm Market Cap",
            com_mc, df.height, pct,
        )
    return df


# --- API pública: Snapshot Atual ---

#: Colunas do snapshot atual (ordem oficial de saída).
SNAPSHOT_COLS: list[str] = [
    "TICKER", "CNPJ_CIA", "RAZAO_CVM", "CATEGORIA", "ANO_REFERENCIA",
    "DATA_COTACAO", "PRECO_ATUAL",
    "MARKET_CAP_ATUAL",
    "P_L_ATUAL", "P_VP_ATUAL", "P_RECEITA", "EARNINGS_YIELD",
    "EV_EBITDA", "EV_RECEITA",
    "LPA", "VPA",
    "ROE", "ROA", "ROIC", "MARGEM_LIQUIDA", "MARGEM_EBITDA",
    "DIVIDA_LIQUIDA", "PATRIMONIO_LIQUIDO", "RECEITA_LIQUIDA",
    "LUCRO_LIQUIDO", "EBITDA",
]


def build_snapshot_atual(
    df_history: pl.DataFrame,
    df_precos_diarios: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Constrói o snapshot atual — 1 linha por ticker com o último fechamento real.

    O preço usado é o **último fechamento diário disponível** de cada ticker no
    histórico de cotações do Yahoo (geralmente o fechamento do último pregão).
    Quando ``df_precos_diarios`` não é fornecido, usa ``PRECO_FIM_ANO`` do
    último ano fiscal como fallback (modo "degradado", útil para testes).

    Padrão de mercado: "snapshot" é tabela separada com 1 linha por ticker, não
    um valor repetido em todo o histórico.

    Args:
        df_history: Série histórica completa (já com múltiplos históricos).
            Deve conter ``PRECO_FIM_ANO`` — usado como fallback.
        df_precos_diarios: Cotações diárias por ticker (``TICKER``, ``DATA``,
            ``FECHAMENTO``). Quando fornecido, o ``PRECO_ATUAL`` é o último
            fechamento disponível (pregão mais recente). Quando ``None``,
            usa o ``PRECO_FIM_ANO`` do último ano fiscal (fallback legado).

    Returns:
        DataFrame com 1 linha por ticker e múltiplos ATUAIS. Vazio se
        ``df_history`` estiver vazio ou sem ``PRECO_FIM_ANO``.

    Assepsia setorial: Bancos e Seguradoras → ``EV_EBITDA`` e
    ``EV_RECEITA`` = ``None``.

    Nota de honestidade:
        O "preço atual" é o último fechamento diário do Yahoo, não uma
        cotação intraday real-time. Para análise fundamentalista isso
        é o dado correto e suficiente.
    """
    if df_history.is_empty() or "PRECO_FIM_ANO" not in df_history.columns:
        logger.warning(
            "df_history sem PRECO_FIM_ANO — snapshot atual retornará vazio."
        )
        return pl.DataFrame()

    df_latest = _latest_fiscal_year_per_ticker(df_history)
    df = _attach_current_price(df_latest, df_precos_diarios)
    df = _compute_valuation(df, _CURRENT_CONTEXT)

    cols_final = [c for c in SNAPSHOT_COLS if c in df.columns]
    df = df.select(cols_final)

    com_preco = df.filter(pl.col("PRECO_ATUAL").is_not_null()).height
    pct = (com_preco / df.height * 100) if df.height > 0 else 0
    logger.info(
        "Snapshot atual: {} tickers, {:.1f}% com preço atual", df.height, pct
    )
    return df


def _today_iso() -> str:
    """Retorna a data de hoje no formato YYYY-MM-DD (sem horário)."""
    from datetime import date

    return date.today().isoformat()


def _attach_current_price(
    df_latest: pl.DataFrame,
    df_precos_diarios: pl.DataFrame | None,
) -> pl.DataFrame:
    """Anexa ``PRECO_ATUAL`` e ``DATA_COTACAO`` baseado no último pregão disponível.

    Se ``df_precos_diarios`` for fornecido, pega o fechamento do último pregão
    de cada ticker (que é o preço "atual" mais recente disponível no Yahoo).
    Caso contrário, usa ``PRECO_FIM_ANO`` como fallback e marca ``DATA_COTACAO``
    com a data de hoje (modo degradado — útil para testes sem cotações).

    Args:
        df_latest: DataFrame com 1 linha por ticker (resultado de
            ``_latest_fiscal_year_per_ticker``).
        df_precos_diarios: Cotações diárias (``TICKER``, ``DATA``, ``FECHAMENTO``).
            ``None`` aciona o modo fallback.

    Returns:
        ``df_latest`` com colunas ``PRECO_ATUAL`` (Float64) e ``DATA_COTACAO``.
    """
    if df_precos_diarios is None or df_precos_diarios.is_empty():
        return df_latest.with_columns(
            pl.col("PRECO_FIM_ANO").cast(pl.Float64).alias("PRECO_ATUAL"),
            pl.lit(_today_iso()).alias("DATA_COTACAO"),
        )

    ultimo_pregao = _latest_close_per_ticker(df_precos_diarios)
    df = df_latest.join(ultimo_pregao, on="TICKER", how="left")

    # Fallback para tickers sem cotação: usa PRECO_FIM_ANO e hoje.
    today = _today_iso()
    return df.with_columns(
        pl.col("PRECO_ATUAL").fill_null(pl.col("PRECO_FIM_ANO").cast(pl.Float64)),
        pl.col("DATA_COTACAO").fill_null(pl.lit(today)),
    )


def _latest_close_per_ticker(df_precos_diarios: pl.DataFrame) -> pl.DataFrame:
    """Extrai o último fechamento disponível de cada ticker.

    Args:
        df_precos_diarios: DataFrame com colunas ``TICKER``, ``DATA`` (string
            ISO ``YYYY-MM-DD``) e ``FECHAMENTO``.

    Returns:
        DataFrame com ``TICKER``, ``PRECO_ATUAL`` (último fechamento) e
        ``DATA_COTACAO`` (data do último fechamento).
    """
    return (
        df_precos_diarios.sort("DATA", descending=True)
        .group_by("TICKER")
        .agg(
            pl.col("FECHAMENTO").first().cast(pl.Float64).alias("PRECO_ATUAL"),
            pl.col("DATA").first().alias("DATA_COTACAO"),
        )
    )


def _latest_fiscal_year_per_ticker(df_history: pl.DataFrame) -> pl.DataFrame:
    """Retorna a última linha (por ANO descendente) de cada ticker.

    A coluna ``ANO`` é preservada (necessária para o cálculo consolidado de MC)
    e também exposta como ``ANO_REFERENCIA`` para o output do snapshot.
    """
    df_latest = (
        df_history.sort(["TICKER", "ANO"], descending=[False, True])
        .unique(subset=["TICKER"], keep="first")
        .rename({"ANO": "ANO_REFERENCIA"})
    )
    return df_latest.with_columns(pl.col("ANO_REFERENCIA").alias("ANO"))
