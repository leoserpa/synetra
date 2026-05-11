"""Testes unitários para synetra.domain.indicators.

Cobre as pequenas unidades funcionais que compõem os tiers (safe_div, only_for,
by_category) e valida os indicadores compostos (ROIC, Altman Z, FCL) em cima
de DataFrames mínimos — evitando depender do pipeline completo.

Princípios (F.I.R.S.T.):
    Fast          — todos usam DataFrames pequenos (< 5 linhas).
    Independent   — cada teste monta seu próprio DataFrame.
    Repeatable    — sem I/O, sem dependências externas.
    Self-Validating — assertions explícitas com valores esperados.
    Timely        — escritos logo após a refatoração.
"""
from __future__ import annotations

import math

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from synetra.domain import (
    BRAZIL_AFTER_TAX,
    BRAZIL_TAX_RATE,
    Categoria,
    by_category,
    calculate_all_indicators,
    only_for,
    safe_div,
)
from synetra.domain.indicators import (
    ALTMAN_COEF_BV_TL,
    ALTMAN_COEF_EBIT_TA,
    ALTMAN_COEF_RE_TA,
    ALTMAN_COEF_WC_TA,
)

# --- Constantes financeiras ---


class TestBrazilianTaxConstants:
    """Garante valores exatos das constantes fiscais brasileiras."""

    def test_tax_rate_is_34_percent(self) -> None:
        assert BRAZIL_TAX_RATE == 0.34

    def test_after_tax_factor_is_66_percent(self) -> None:
        assert pytest.approx(0.66) == BRAZIL_AFTER_TAX

    def test_tax_rate_and_after_tax_sum_to_one(self) -> None:
        assert pytest.approx(1.0) == BRAZIL_TAX_RATE + BRAZIL_AFTER_TAX


class TestAltmanCoefficients:
    """Coeficientes do Altman Z''-Score (Altman, 2005 - Emerging Markets)."""

    def test_working_capital_coefficient(self) -> None:
        assert ALTMAN_COEF_WC_TA == 6.56

    def test_retained_earnings_coefficient(self) -> None:
        assert ALTMAN_COEF_RE_TA == 3.26

    def test_ebit_coefficient(self) -> None:
        assert ALTMAN_COEF_EBIT_TA == 6.72

    def test_book_value_coefficient(self) -> None:
        assert ALTMAN_COEF_BV_TL == 1.05


# --- safe_div ---


class TestSafeDiv:
    """Divisão segura que retorna None quando denominador é zero."""

    def test_basic_division(self) -> None:
        df = pl.DataFrame({"num": [10.0], "den": [2.0]})
        result = df.with_columns(safe_div("num", "den").alias("r")).row(0, named=True)
        assert result["r"] == 5.0

    def test_zero_denominator_returns_none(self) -> None:
        df = pl.DataFrame({"num": [10.0], "den": [0.0]})
        result = df.with_columns(safe_div("num", "den").alias("r")).row(0, named=True)
        assert result["r"] is None

    def test_negative_values(self) -> None:
        df = pl.DataFrame({"num": [-10.0], "den": [2.0]})
        result = df.with_columns(safe_div("num", "den").alias("r")).row(0, named=True)
        assert result["r"] == -5.0

    def test_accepts_expression_arguments(self) -> None:
        df = pl.DataFrame({"a": [10.0], "b": [5.0], "c": [2.0]})
        result = df.with_columns(
            safe_div(pl.col("a") - pl.col("b"), "c").alias("r")
        ).row(0, named=True)
        assert result["r"] == 2.5

    @given(
        numerator=st.floats(min_value=-1e9, max_value=1e9, allow_nan=False),
        denominator=st.floats(min_value=-1e9, max_value=1e9, allow_nan=False).filter(
            lambda x: abs(x) > 1e-9
        ),
    )
    def test_never_raises_on_nonzero_denominator(
        self, numerator: float, denominator: float
    ) -> None:
        df = pl.DataFrame({"n": [numerator], "d": [denominator]})
        result = df.with_columns(safe_div("n", "d").alias("r")).row(0, named=True)
        assert result["r"] is not None
        assert math.isclose(result["r"], numerator / denominator, rel_tol=1e-9)


# --- only_for ---


class TestOnlyFor:
    """Aplica expressão apenas quando CATEGORIA bate."""

    @pytest.fixture
    def mixed_categories_df(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "CATEGORIA": ["INDUSTRIAL", "FINANCEIRO", "SEGURADORA"],
                "valor": [100.0, 200.0, 300.0],
            }
        )

    def test_preserves_value_for_matching_category(
        self, mixed_categories_df: pl.DataFrame
    ) -> None:
        result = mixed_categories_df.with_columns(
            only_for(Categoria.INDUSTRIAL, pl.col("valor")).alias("r")
        )
        assert result["r"].to_list() == [100.0, None, None]

    def test_returns_none_for_other_categories(
        self, mixed_categories_df: pl.DataFrame
    ) -> None:
        result = mixed_categories_df.with_columns(
            only_for(Categoria.FINANCEIRO, pl.col("valor")).alias("r")
        )
        assert result["r"].to_list() == [None, 200.0, None]


# --- by_category ---


class TestByCategory:
    """Aplica expressão diferente para cada categoria."""

    @pytest.fixture
    def sample_df(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "CATEGORIA": ["INDUSTRIAL", "FINANCEIRO", "SEGURADORA", "OUTROS"],
                "x": [10.0, 20.0, 30.0, 40.0],
            }
        )

    def test_different_formula_per_category(self, sample_df: pl.DataFrame) -> None:
        result = sample_df.with_columns(
            by_category(
                industrial=pl.col("x") * 2,
                financeiro=pl.col("x") * 3,
                seguradora=pl.col("x") * 4,
            ).alias("r")
        )
        assert result["r"].to_list() == [20.0, 60.0, 120.0, None]

    def test_default_fallback_is_applied(self, sample_df: pl.DataFrame) -> None:
        result = sample_df.with_columns(
            by_category(industrial=pl.col("x"), default=pl.lit(-1.0)).alias("r")
        )
        assert result["r"].to_list() == [10.0, -1.0, -1.0, -1.0]

    def test_all_none_when_no_branches(self, sample_df: pl.DataFrame) -> None:
        result = sample_df.with_columns(by_category().alias("r"))
        assert all(v is None for v in result["r"].to_list())


# --- Pipeline completo — indicadores compostos ---


def _build_industrial_fixture() -> pl.DataFrame:
    """DataFrame de 1 linha com dados consistentes para uma indústria saudável.

    Valores escolhidos para produzir ROIC ≈ 9,9% e Altman Z ≈ 7,163.
    """
    return pl.DataFrame(
        {
            "CATEGORIA": ["INDUSTRIAL"],
            "LUCRO_FINAL": [100.0],
            "PATRIMONIO_LIQUIDO": [600.0],
            "ATIVO_TOTAL": [1000.0],
            "QTDE_ACOES": [100.0],
            "RECEITA_LIQUIDA": [800.0],
            "DIVIDA_LP": [200.0],
            "DIVIDA_CP": [100.0],
            "FCO": [110.0],
            "FCI": [-30.0],
            "RESULTADO_BRUTO": [300.0],
            "EBIT": [150.0],
            "CAPEX_VAL": [-40.0],
            "DEPREC_AMORT": [-20.0],
            "DIVIDENDOS_PAGOS": [-30.0],
            "ATIVO_CIRCULANTE": [500.0],
            "PASSIVO_CIRCULANTE": [100.0],
            "CAIXA_EQUIVALENTES": [80.0],
        }
    )


class TestTier1Profitability:
    """ROE, ROA, LPA, VPA para empresa industrial."""

    @pytest.fixture
    def df(self) -> pl.DataFrame:
        return calculate_all_indicators(_build_industrial_fixture())

    def test_roe_is_lucro_over_equity(self, df: pl.DataFrame) -> None:
        assert df["ROE"][0] == pytest.approx(100.0 / 600.0)

    def test_roa_is_lucro_over_assets(self, df: pl.DataFrame) -> None:
        assert df["ROA"][0] == pytest.approx(100.0 / 1000.0)

    def test_lpa_is_lucro_per_share(self, df: pl.DataFrame) -> None:
        assert df["LPA"][0] == pytest.approx(1.0)

    def test_vpa_is_equity_per_share(self, df: pl.DataFrame) -> None:
        assert df["VPA"][0] == pytest.approx(6.0)

    def test_margens_operacionais(self, df: pl.DataFrame) -> None:
        assert df["MARGEM_BRUTA"][0] == pytest.approx(300.0 / 800.0)
        assert df["MARGEM_EBIT"][0] == pytest.approx(150.0 / 800.0)
        assert df["MARGEM_LIQUIDA"][0] == pytest.approx(100.0 / 800.0)


class TestTier2CashFlow:
    """FCL, Payout, EBITDA."""

    def test_fcl_industrial_is_fco_plus_capex(self) -> None:
        df = calculate_all_indicators(_build_industrial_fixture())
        # FCO=110, CAPEX=-40 (depois do tier 1), soma = 70
        assert df["FCL"][0] == pytest.approx(110.0 + (-40.0))

    def test_fcl_financial_uses_fci(self) -> None:
        base = _build_industrial_fixture().with_columns(
            pl.lit("FINANCEIRO").alias("CATEGORIA")
        )
        df = calculate_all_indicators(base)
        # FCO=110, FCI=-30 → 80
        assert df["FCL"][0] == pytest.approx(80.0)

    def test_payout_is_proventos_over_lucro(self) -> None:
        df = calculate_all_indicators(_build_industrial_fixture())
        # PROVENTOS = abs(-30) = 30; payout = 30 / 100 = 0.30
        assert df["PAYOUT"][0] == pytest.approx(0.30)

    def test_ebitda_is_ebit_plus_abs_depreciation(self) -> None:
        df = calculate_all_indicators(_build_industrial_fixture())
        # EBIT=150, |D&A|=20 → 170
        assert df["EBITDA"][0] == pytest.approx(170.0)


class TestTier5RoicAndAltman:
    """ROIC e Altman Z-Score — indicadores de maior complexidade."""

    def test_roic_formula(self) -> None:
        """ROIC = EBIT × 0.66 / (PL + Dívida Total)."""
        df = calculate_all_indicators(_build_industrial_fixture())
        # EBIT=150, PL=600, Dívida Total=300 → NOPAT=99, capital=900 → ROIC=0.11
        expected = 150.0 * BRAZIL_AFTER_TAX / 900.0
        assert df["ROIC"][0] == pytest.approx(expected)

    def test_roic_none_for_financial_sector(self) -> None:
        base = _build_industrial_fixture().with_columns(
            pl.lit("FINANCEIRO").alias("CATEGORIA")
        )
        df = calculate_all_indicators(base)
        assert df["ROIC"][0] is None

    def test_altman_z_score_formula(self) -> None:
        """Validação manual da fórmula Z'' = 6.56·A + 3.26·B + 6.72·C + 1.05·D."""
        df = calculate_all_indicators(_build_industrial_fixture())
        # A = (AC - PC)/AT = (500-100)/1000 = 0.4
        # B = PL/AT = 600/1000 = 0.6
        # C = EBIT/AT = 150/1000 = 0.15
        # D = PL/(AT-PL) = 600/400 = 1.5
        expected = (
            ALTMAN_COEF_WC_TA * 0.4
            + ALTMAN_COEF_RE_TA * 0.6
            + ALTMAN_COEF_EBIT_TA * 0.15
            + ALTMAN_COEF_BV_TL * 1.5
        )
        assert df["ALTMAN_Z"][0] == pytest.approx(expected)

    def test_altman_z_none_for_insurance(self) -> None:
        base = _build_industrial_fixture().with_columns(
            pl.lit("SEGURADORA").alias("CATEGORIA")
        )
        df = calculate_all_indicators(base)
        assert df["ALTMAN_Z"][0] is None


class TestSectorialCleanliness:
    """Assepsia setorial: CAPEX/Dívida/Altman não se aplicam igualmente."""

    def test_capex_is_null_for_financial(self) -> None:
        base = _build_industrial_fixture().with_columns(
            pl.lit("FINANCEIRO").alias("CATEGORIA")
        )
        df = calculate_all_indicators(base)
        assert df["CAPEX"][0] is None

    def test_divida_total_zero_for_insurance(self) -> None:
        base = _build_industrial_fixture().with_columns(
            pl.lit("SEGURADORA").alias("CATEGORIA")
        )
        df = calculate_all_indicators(base)
        assert df["DIVIDA_TOTAL"][0] == 0.0

    def test_divida_total_null_for_financial(self) -> None:
        base = _build_industrial_fixture().with_columns(
            pl.lit("FINANCEIRO").alias("CATEGORIA")
        )
        df = calculate_all_indicators(base)
        assert df["DIVIDA_TOTAL"][0] is None

    def test_liquidez_corrente_only_for_industrial(self) -> None:
        industrial = calculate_all_indicators(_build_industrial_fixture())
        insurance = calculate_all_indicators(
            _build_industrial_fixture().with_columns(
                pl.lit("SEGURADORA").alias("CATEGORIA")
            )
        )
        assert industrial["LIQUIDEZ_CORRENTE"][0] == pytest.approx(5.0)
        assert insurance["LIQUIDEZ_CORRENTE"][0] is None


# --- Enum Categoria ---


class TestCategoriaEnum:
    """Enum substitui strings mágicas e facilita refatoração."""

    def test_enum_values_match_legacy_strings(self) -> None:
        assert Categoria.INDUSTRIAL.value == "INDUSTRIAL"
        assert Categoria.FINANCEIRO.value == "FINANCEIRO"
        assert Categoria.SEGURADORA.value == "SEGURADORA"

    def test_enum_is_strenum_subclass(self) -> None:
        # StrEnum permite comparação direta com strings (compatibilidade).
        assert Categoria.INDUSTRIAL == "INDUSTRIAL"
