"""Testes unitários das funções internas do price_aggregator.

Cobre os helpers extraídos na refatoração:
    - _positive_price_and_qty (predicado de guarda)
    - _price_ratio / _yield_ratio (P/L e Earnings Yield)
    - _compute_valuation (pipeline genérico)
    - _apply_sectorial_assepsia (EV para bancos)
    - _round_valuation_columns
    - _latest_fiscal_year_per_ticker

Princípios F.I.R.S.T.: DataFrames pequenos, independentes, sem I/O.
"""
from __future__ import annotations

import polars as pl
import pytest

from synetra.domain import Categoria
from synetra.market.price_aggregator import (
    _CURRENT_CONTEXT,
    _HISTORICAL_CONTEXT,
    ValuationContext,
    _apply_sectorial_assepsia,
    _compute_market_cap,
    _compute_valuation,
    _latest_fiscal_year_per_ticker,
    _positive_price_and_qty,
    _price_ratio,
    _round_valuation_columns,
    _yield_ratio,
)

# --- ValuationContext ---


class TestValuationContext:
    """Garante que os dois contextos pré-configurados estão corretos."""

    def test_historical_context_uses_preco_fim_ano(self) -> None:
        assert _HISTORICAL_CONTEXT.price_col == "PRECO_FIM_ANO"
        assert _HISTORICAL_CONTEXT.market_cap_col == "MARKET_CAP"
        assert _HISTORICAL_CONTEXT.pl_col == "P_L"
        assert _HISTORICAL_CONTEXT.pvp_col == "P_VP"

    def test_current_context_uses_preco_atual_with_suffix(self) -> None:
        assert _CURRENT_CONTEXT.price_col == "PRECO_ATUAL"
        assert _CURRENT_CONTEXT.market_cap_col == "MARKET_CAP_ATUAL"
        assert _CURRENT_CONTEXT.pl_col == "P_L_ATUAL"
        assert _CURRENT_CONTEXT.pvp_col == "P_VP_ATUAL"

    def test_context_is_immutable(self) -> None:
        ctx = ValuationContext(
            price_col="X", market_cap_col="MC", pl_col="PL", pvp_col="PVP"
        )
        with pytest.raises(AttributeError):
            ctx.price_col = "Y"  # type: ignore[misc]


# --- _positive_price_and_qty ---


class TestPositivePriceAndQty:
    """Predicado de guarda: preço > 0 AND todas as quantidades > 0."""

    def test_all_positive_returns_true(self) -> None:
        df = pl.DataFrame({"price": [10.0], "q1": [100.0], "q2": [50.0]})
        result = df.select(
            _positive_price_and_qty(pl.col("price"), pl.col("q1"), pl.col("q2")).alias("r")
        ).row(0, named=True)
        assert result["r"] is True

    def test_zero_price_returns_false(self) -> None:
        df = pl.DataFrame({"price": [0.0], "q1": [100.0]})
        result = df.select(
            _positive_price_and_qty(pl.col("price"), pl.col("q1")).alias("r")
        ).row(0, named=True)
        assert result["r"] is False

    def test_negative_qty_returns_false(self) -> None:
        df = pl.DataFrame({"price": [10.0], "q1": [-5.0]})
        result = df.select(
            _positive_price_and_qty(pl.col("price"), pl.col("q1")).alias("r")
        ).row(0, named=True)
        assert result["r"] is False


# --- _price_ratio / _yield_ratio ---


class TestPriceRatio:
    """P/L = preço / lucro por ação."""

    def test_basic_ratio(self) -> None:
        df = pl.DataFrame({"price": [100.0], "lpa": [5.0], "qty": [1000.0]})
        result = df.select(
            _price_ratio(pl.col("price"), pl.col("lpa"), pl.col("qty")).alias("r")
        ).row(0, named=True)
        assert result["r"] == pytest.approx(20.0)

    def test_zero_price_returns_none(self) -> None:
        df = pl.DataFrame({"price": [0.0], "lpa": [5.0], "qty": [1000.0]})
        result = df.select(
            _price_ratio(pl.col("price"), pl.col("lpa"), pl.col("qty")).alias("r")
        ).row(0, named=True)
        assert result["r"] is None

    def test_negative_lpa_returns_none(self) -> None:
        """P/L negativo não é informativo — empresa com prejuízo."""
        df = pl.DataFrame({"price": [100.0], "lpa": [-2.0], "qty": [1000.0]})
        result = df.select(
            _price_ratio(pl.col("price"), pl.col("lpa"), pl.col("qty")).alias("r")
        ).row(0, named=True)
        assert result["r"] is None


class TestYieldRatio:
    """Earnings Yield = lpa / preço (inverso de P/L)."""

    def test_is_reciprocal_of_price_ratio(self) -> None:
        price, lpa, qty = 100.0, 5.0, 1000.0
        df = pl.DataFrame({"price": [price], "lpa": [lpa], "qty": [qty]})
        yield_r = df.select(
            _yield_ratio(pl.col("price"), pl.col("lpa"), pl.col("qty")).alias("r")
        ).row(0, named=True)["r"]
        assert yield_r == pytest.approx(lpa / price)  # 0.05

    def test_zero_price_returns_none(self) -> None:
        df = pl.DataFrame({"price": [0.0], "lpa": [5.0], "qty": [1000.0]})
        result = df.select(
            _yield_ratio(pl.col("price"), pl.col("lpa"), pl.col("qty")).alias("r")
        ).row(0, named=True)
        assert result["r"] is None


# --- _apply_sectorial_assepsia ---


class TestSectorialAssepsia:
    """EV_EBITDA e EV_RECEITA não se aplicam a bancos/seguradoras."""

    @pytest.fixture
    def mixed_df(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "CATEGORIA": ["INDUSTRIAL", "FINANCEIRO", "SEGURADORA"],
                "EV_EBITDA": [8.5, 12.0, 10.0],
                "EV_RECEITA": [2.1, 3.0, 2.5],
                "P_L": [15.0, 10.0, 12.0],  # Não deve ser anulado
            }
        )

    def test_nullifies_ev_for_financial(self, mixed_df: pl.DataFrame) -> None:
        result = _apply_sectorial_assepsia(mixed_df)
        assert result["EV_EBITDA"].to_list() == [8.5, None, None]
        assert result["EV_RECEITA"].to_list() == [2.1, None, None]

    def test_preserves_pl_for_all_categories(self, mixed_df: pl.DataFrame) -> None:
        """P/L funciona pra qualquer setor — não deve ser anulado."""
        result = _apply_sectorial_assepsia(mixed_df)
        assert result["P_L"].to_list() == [15.0, 10.0, 12.0]

    def test_no_categoria_column_returns_unchanged(self) -> None:
        df = pl.DataFrame({"EV_EBITDA": [8.5]})
        result = _apply_sectorial_assepsia(df)
        assert result.equals(df)

    def test_uses_categoria_enum_values(self) -> None:
        """Garante que a função usa os valores exatos do enum Categoria."""
        df = pl.DataFrame(
            {
                "CATEGORIA": [Categoria.FINANCEIRO.value, Categoria.INDUSTRIAL.value],
                "EV_EBITDA": [10.0, 8.0],
                "EV_RECEITA": [2.0, 1.5],
            }
        )
        result = _apply_sectorial_assepsia(df)
        assert result["EV_EBITDA"].to_list() == [None, 8.0]


# --- _round_valuation_columns ---


class TestRoundValuationColumns:
    """Arredondamentos: 2d preço/ratios, 4d yield, 0d MC."""

    def test_rounds_each_type_correctly(self) -> None:
        df = pl.DataFrame(
            {
                "PRECO_FIM_ANO": [10.12345],
                "MARKET_CAP": [1_000_000_123.456],
                "P_L": [15.12345],
                "P_VP": [1.23456],
                "EARNINGS_YIELD": [0.054321],
            }
        )
        result = _round_valuation_columns(
            df,
            price_col="PRECO_FIM_ANO",
            market_cap_col="MARKET_CAP",
            pl_col="P_L",
            pvp_col="P_VP",
        )
        assert result["PRECO_FIM_ANO"][0] == pytest.approx(10.12)
        assert result["MARKET_CAP"][0] == pytest.approx(1_000_000_123.0)
        assert result["P_L"][0] == pytest.approx(15.12)
        assert result["P_VP"][0] == pytest.approx(1.23)
        assert result["EARNINGS_YIELD"][0] == pytest.approx(0.0543)

    def test_ignores_missing_columns(self) -> None:
        """Não deve falhar se colunas não existirem."""
        df = pl.DataFrame({"P_L": [15.123]})
        result = _round_valuation_columns(
            df,
            price_col="PRECO_INEXISTENTE",
            market_cap_col="MC_INEXISTENTE",
            pl_col="P_L",
            pvp_col="PVP_INEXISTENTE",
        )
        assert result["P_L"][0] == pytest.approx(15.12)


# --- _latest_fiscal_year_per_ticker ---


class TestLatestFiscalYearPerTicker:
    """Pega o último ano de cada ticker mantendo ANO e criando ANO_REFERENCIA."""

    def test_selects_most_recent_year(self) -> None:
        df = pl.DataFrame(
            {
                "TICKER": ["PETR4", "PETR4", "PETR4", "VALE3", "VALE3"],
                "ANO": [2022, 2024, 2023, 2023, 2024],
                "LUCRO_LIQUIDO": [100, 300, 200, 400, 500],
            }
        )
        result = _latest_fiscal_year_per_ticker(df).sort("TICKER")
        assert result["ANO_REFERENCIA"].to_list() == [2024, 2024]
        assert result["LUCRO_LIQUIDO"].to_list() == [300, 500]

    def test_ano_column_is_preserved(self) -> None:
        """ANO precisa continuar presente (usado no MC consolidado)."""
        df = pl.DataFrame({"TICKER": ["PETR4"], "ANO": [2024], "LUCRO": [100.0]})
        result = _latest_fiscal_year_per_ticker(df)
        assert "ANO" in result.columns
        assert "ANO_REFERENCIA" in result.columns
        assert result["ANO"][0] == result["ANO_REFERENCIA"][0]


# --- _compute_market_cap (fallback path) ---


class TestComputeMarketCapFallback:
    """Quando falta QTDE_ON/QTDE_PN usa fallback simples preço × qtde total."""

    def test_fallback_when_no_on_pn_columns(self) -> None:
        df = pl.DataFrame(
            {"TICKER": ["X"], "PRECO_FIM_ANO": [10.0], "QTDE_ACOES": [100.0]}
        )
        result = _compute_market_cap(df, _HISTORICAL_CONTEXT)
        assert result["MARKET_CAP"][0] == pytest.approx(1000.0)

    def test_null_when_missing_price_column(self) -> None:
        df = pl.DataFrame({"TICKER": ["X"], "QTDE_ACOES": [100.0]})
        result = _compute_market_cap(df, _HISTORICAL_CONTEXT)
        assert result["MARKET_CAP"][0] is None

    def test_null_when_price_is_zero(self) -> None:
        df = pl.DataFrame(
            {"TICKER": ["X"], "PRECO_FIM_ANO": [0.0], "QTDE_ACOES": [100.0]}
        )
        result = _compute_market_cap(df, _HISTORICAL_CONTEXT)
        assert result["MARKET_CAP"][0] is None


# --- _compute_valuation (pipeline completo) ---


class TestComputeValuationPipeline:
    """Teste de integração do pipeline genérico de valuation."""

    @pytest.fixture
    def industrial_df(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "TICKER": ["ABC3"],
                "CATEGORIA": [Categoria.INDUSTRIAL.value],
                "PRECO_FIM_ANO": [50.0],
                "QTDE_ACOES": [1_000_000.0],
                "LUCRO_LIQUIDO": [5_000_000.0],
                "PATRIMONIO_LIQUIDO": [20_000_000.0],
                "RECEITA_LIQUIDA": [100_000_000.0],
                "EBITDA": [15_000_000.0],
                "DIVIDA_LIQUIDA": [10_000_000.0],
            }
        )

    def test_all_multiples_calculated(self, industrial_df: pl.DataFrame) -> None:
        result = _compute_valuation(industrial_df, _HISTORICAL_CONTEXT)
        # MC = 50 × 1M = 50M
        assert result["MARKET_CAP"][0] == pytest.approx(50_000_000.0)
        # LPA = 5M / 1M = 5; P/L = 50/5 = 10
        assert result["P_L"][0] == pytest.approx(10.0)
        # VPA = 20M / 1M = 20; P/VP = 50/20 = 2.5
        assert result["P_VP"][0] == pytest.approx(2.5)
        # P/Receita = 50M / 100M = 0.5
        assert result["P_RECEITA"][0] == pytest.approx(0.5)
        # EV = MC + DL = 60M; EV/EBITDA = 60M / 15M = 4
        assert result["EV_EBITDA"][0] == pytest.approx(4.0)
        # Earnings Yield = 5/50 = 0.10 = 10%
        assert result["EARNINGS_YIELD"][0] == pytest.approx(0.10)

    def test_financial_has_no_ev_multiples(
        self, industrial_df: pl.DataFrame
    ) -> None:
        bank_df = industrial_df.with_columns(
            pl.lit(Categoria.FINANCEIRO.value).alias("CATEGORIA")
        )
        result = _compute_valuation(bank_df, _HISTORICAL_CONTEXT)
        assert result["EV_EBITDA"][0] is None
        assert result["EV_RECEITA"][0] is None
        # Mas P/L continua funcionando
        assert result["P_L"][0] == pytest.approx(10.0)

    def test_current_context_produces_atual_columns(
        self, industrial_df: pl.DataFrame
    ) -> None:
        """Mesmo pipeline com contexto de snapshot produz colunas _ATUAL."""
        df = industrial_df.with_columns(pl.lit(60.0).alias("PRECO_ATUAL"))
        result = _compute_valuation(df, _CURRENT_CONTEXT)
        assert "MARKET_CAP_ATUAL" in result.columns
        assert "P_L_ATUAL" in result.columns
        assert "P_VP_ATUAL" in result.columns
        # P/L_ATUAL = 60 / 5 = 12
        assert result["P_L_ATUAL"][0] == pytest.approx(12.0)
