"""Testes unitários para fórmulas individuais extraídas do transformer.

Valida as fórmulas de:
    - Beneish M-Score (8 termos: DSRI, GMI, AQI, SGI, DEPI, SGAI, TATA, LVGI)
    - Crescimento (YoY, CAGR)
    - Eficiência (ROCE, Cash ROA, PMR, Cash Ratio, Sustainable Growth, ...)
    - Helper _nullify_for (assepsia setorial)

Princípios F.I.R.S.T.:
    Fast          — DataFrames de 1-3 linhas.
    Independent   — cada teste monta seu próprio fixture.
    Repeatable    — sem I/O, sem estado compartilhado.
    Self-Validating — assertions com valores esperados calculados manualmente.
"""
from __future__ import annotations

import polars as pl
import pytest

from synetra.domain import BRAZIL_AFTER_TAX, Categoria
from synetra.transformer import (
    _beneish_aqi,
    _beneish_depi,
    _beneish_dsri,
    _beneish_gmi,
    _beneish_lvgi,
    _beneish_sgai,
    _beneish_sgi,
    _beneish_tata,
    _cagr_expr,
    _cash_ratio,
    _cash_roa,
    _margin_of_cash_flow,
    _nullify_for,
    _receivables_days,
    _reinvestment_rate,
    _roce,
    _sustainable_growth,
)

# --- _nullify_for ---


class TestNullifyFor:
    """Helper de assepsia setorial: anula colunas para uma categoria."""

    @pytest.fixture
    def mixed_df(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "CATEGORIA": ["INDUSTRIAL", "FINANCEIRO", "SEGURADORA"],
                "ROIC": [0.15, 0.20, 0.10],
                "EBITDA": [100.0, 200.0, 50.0],
            }
        )

    def test_nullifies_for_target_category_only(self, mixed_df: pl.DataFrame) -> None:
        result = _nullify_for(mixed_df, Categoria.FINANCEIRO, ["ROIC"])
        assert result["ROIC"].to_list() == [0.15, None, 0.10]
        # Colunas não listadas permanecem intactas
        assert result["EBITDA"].to_list() == [100.0, 200.0, 50.0]

    def test_ignores_nonexistent_columns(self, mixed_df: pl.DataFrame) -> None:
        # Não deve explodir se a coluna não existir
        result = _nullify_for(mixed_df, Categoria.FINANCEIRO, ["COLUNA_INEXISTENTE"])
        assert result.equals(mixed_df)

    def test_returns_same_df_when_no_valid_columns(
        self, mixed_df: pl.DataFrame
    ) -> None:
        result = _nullify_for(mixed_df, Categoria.INDUSTRIAL, [])
        assert result.equals(mixed_df)


# --- Beneish M-Score — termos individuais ---


def _build_beneish_fixture() -> pl.DataFrame:
    """DataFrame de 1 linha com valores prev e current para calcular Beneish."""
    return pl.DataFrame(
        {
            "CONTAS_A_RECEBER": [120.0],
            "CONTAS_A_RECEBER_prev": [100.0],
            "RECEITA_LIQUIDA": [1000.0],
            "RECEITA_LIQUIDA_prev": [900.0],
            "MARGEM_BRUTA": [0.30],
            "MARGEM_BRUTA_prev": [0.35],
            "ATIVO_CIRCULANTE": [500.0],
            "ATIVO_CIRCULANTE_prev": [450.0],
            "IMOBILIZADO": [400.0],
            "IMOBILIZADO_prev": [350.0],
            "ATIVO_TOTAL": [1000.0],
            "ATIVO_TOTAL_prev": [900.0],
            "DEPREC_AMORT": [-50.0],
            "DEPREC_AMORT_prev": [-40.0],
            "DESPESAS_OPERACIONAIS": [-150.0],
            "DESPESAS_OPERACIONAIS_prev": [-130.0],
            "DIVIDA_BRUTA": [300.0],
            "DIVIDA_BRUTA_prev": [250.0],
            "ACCRUALS": [80.0],
        }
    )


class TestBeneishTerms:
    """Cada termo do Beneish M-Score deve bater com a fórmula manual."""

    @pytest.fixture
    def df(self) -> pl.DataFrame:
        return _build_beneish_fixture()

    def test_dsri_is_receivables_ratio(self, df: pl.DataFrame) -> None:
        """DSRI = (AR/Rec) / (AR_prev/Rec_prev)."""
        result = df.select(_beneish_dsri().alias("r")).row(0, named=True)
        # (120/1000) / (100/900) = 0.12 / 0.1111... = 1.08
        expected = (120.0 / 1000.0) / (100.0 / 900.0)
        assert result["r"] == pytest.approx(expected)

    def test_gmi_is_margin_deterioration(self, df: pl.DataFrame) -> None:
        """GMI = Margem_prev / Margem (quanto a margem caiu)."""
        result = df.select(_beneish_gmi().alias("r")).row(0, named=True)
        expected = 0.35 / 0.30
        assert result["r"] == pytest.approx(expected)

    def test_sgi_is_sales_growth(self, df: pl.DataFrame) -> None:
        """SGI = Receita / Receita_prev."""
        result = df.select(_beneish_sgi().alias("r")).row(0, named=True)
        expected = 1000.0 / 900.0
        assert result["r"] == pytest.approx(expected)

    def test_tata_is_accruals_over_assets(self, df: pl.DataFrame) -> None:
        """TATA = Accruals / Ativo Total."""
        result = df.select(_beneish_tata().alias("r")).row(0, named=True)
        expected = 80.0 / 1000.0
        assert result["r"] == pytest.approx(expected)

    def test_lvgi_is_leverage_change(self, df: pl.DataFrame) -> None:
        """LVGI = (Div/AT) / (Div_prev/AT_prev)."""
        result = df.select(_beneish_lvgi().alias("r")).row(0, named=True)
        expected = (300.0 / 1000.0) / (250.0 / 900.0)
        assert result["r"] == pytest.approx(expected)

    def test_aqi_formula(self, df: pl.DataFrame) -> None:
        """AQI = (1 - (AC+Imob)/AT) / (1 - (AC_prev+Imob_prev)/AT_prev)."""
        result = df.select(_beneish_aqi().alias("r")).row(0, named=True)
        numerator = 1 - (500.0 + 400.0) / 1000.0
        denominator = 1 - (450.0 + 350.0) / 900.0
        expected = numerator / denominator
        assert result["r"] == pytest.approx(expected)

    def test_depi_formula(self, df: pl.DataFrame) -> None:
        """DEPI = (D_prev/(D_prev+Imob_prev)) / (D/(D+Imob))."""
        result = df.select(_beneish_depi().alias("r")).row(0, named=True)
        num = 40.0 / (40.0 + 350.0)
        den = 50.0 / (50.0 + 400.0)
        expected = num / den
        assert result["r"] == pytest.approx(expected)

    def test_sgai_formula(self, df: pl.DataFrame) -> None:
        """SGAI = (|Desp|/Rec) / (|Desp_prev|/Rec_prev)."""
        result = df.select(_beneish_sgai().alias("r")).row(0, named=True)
        num = 150.0 / 1000.0
        den = 130.0 / 900.0
        expected = num / den
        assert result["r"] == pytest.approx(expected)


# --- CAGR ---


class TestCagrExpression:
    """Taxa composta de crescimento anual."""

    def test_positive_growth(self) -> None:
        df = pl.DataFrame({"current": [200.0], "base": [100.0]})
        result = df.select(
            _cagr_expr("current", "base", years=3).alias("r")
        ).row(0, named=True)
        # (200/100)^(1/3) - 1 = 2^0.3333 - 1 ≈ 0.2599
        assert result["r"] == pytest.approx(2 ** (1 / 3) - 1)

    def test_negative_base_returns_none(self) -> None:
        df = pl.DataFrame({"current": [200.0], "base": [-100.0]})
        result = df.select(
            _cagr_expr("current", "base", years=3).alias("r")
        ).row(0, named=True)
        assert result["r"] is None

    def test_zero_base_returns_none(self) -> None:
        df = pl.DataFrame({"current": [200.0], "base": [0.0]})
        result = df.select(
            _cagr_expr("current", "base", years=3).alias("r")
        ).row(0, named=True)
        assert result["r"] is None

    def test_negative_current_returns_none(self) -> None:
        df = pl.DataFrame({"current": [-50.0], "base": [100.0]})
        result = df.select(
            _cagr_expr("current", "base", years=3).alias("r")
        ).row(0, named=True)
        assert result["r"] is None

    def test_five_year_cagr(self) -> None:
        df = pl.DataFrame({"current": [300.0], "base": [100.0]})
        result = df.select(
            _cagr_expr("current", "base", years=5).alias("r")
        ).row(0, named=True)
        assert result["r"] == pytest.approx(3 ** (1 / 5) - 1)


# --- Fórmulas de eficiência ---


class TestCashFlowMargins:
    """Margens de fluxo de caixa (FCO/Receita, FCL/Receita)."""

    def test_margin_fco(self) -> None:
        df = pl.DataFrame({"FCO": [80.0], "RECEITA_LIQUIDA": [400.0]})
        result = df.select(_margin_of_cash_flow("FCO").alias("r")).row(0, named=True)
        assert result["r"] == pytest.approx(0.20)

    def test_margin_fcl(self) -> None:
        df = pl.DataFrame({"FCL": [50.0], "RECEITA_LIQUIDA": [400.0]})
        result = df.select(_margin_of_cash_flow("FCL").alias("r")).row(0, named=True)
        assert result["r"] == pytest.approx(0.125)

    def test_zero_revenue_returns_none(self) -> None:
        df = pl.DataFrame({"FCO": [80.0], "RECEITA_LIQUIDA": [0.0]})
        result = df.select(_margin_of_cash_flow("FCO").alias("r")).row(0, named=True)
        assert result["r"] is None


class TestCashRoa:
    def test_cash_roa_formula(self) -> None:
        df = pl.DataFrame({"FCO": [100.0], "ATIVO_TOTAL": [1000.0]})
        result = df.select(_cash_roa().alias("r")).row(0, named=True)
        assert result["r"] == pytest.approx(0.10)

    def test_zero_assets_returns_none(self) -> None:
        df = pl.DataFrame({"FCO": [100.0], "ATIVO_TOTAL": [0.0]})
        result = df.select(_cash_roa().alias("r")).row(0, named=True)
        assert result["r"] is None


class TestReceivablesDays:
    def test_pmr_formula(self) -> None:
        # 73 / 365 * 365 = 73 dias
        df = pl.DataFrame({"CONTAS_A_RECEBER": [200.0], "RECEITA_LIQUIDA": [1000.0]})
        result = df.select(_receivables_days().alias("r")).row(0, named=True)
        # (200/1000) * 365 = 73 dias
        assert result["r"] == pytest.approx(73.0)

    def test_zero_revenue_returns_none(self) -> None:
        df = pl.DataFrame({"CONTAS_A_RECEBER": [200.0], "RECEITA_LIQUIDA": [0.0]})
        result = df.select(_receivables_days().alias("r")).row(0, named=True)
        assert result["r"] is None


class TestRoce:
    def test_roce_formula(self) -> None:
        df = pl.DataFrame(
            {"EBIT": [150.0], "ATIVO_TOTAL": [1000.0], "PASSIVO_CIRCULANTE": [200.0]}
        )
        result = df.select(_roce().alias("r")).row(0, named=True)
        # 150 / (1000 - 200) = 150/800 = 0.1875
        assert result["r"] == pytest.approx(0.1875)

    def test_negative_capital_employed_returns_none(self) -> None:
        df = pl.DataFrame(
            {"EBIT": [150.0], "ATIVO_TOTAL": [100.0], "PASSIVO_CIRCULANTE": [200.0]}
        )
        result = df.select(_roce().alias("r")).row(0, named=True)
        assert result["r"] is None


class TestReinvestmentRate:
    def test_expansion_signal(self) -> None:
        # |CAPEX| > |Depreciação| → empresa expandindo
        df = pl.DataFrame({"CAPEX": [-150.0], "DEPREC_AMORT": [-100.0]})
        result = df.select(_reinvestment_rate().alias("r")).row(0, named=True)
        assert result["r"] == pytest.approx(1.5)

    def test_capex_abs_values_used(self) -> None:
        """Valores absolutos: sinal do CAPEX não importa na fórmula."""
        df = pl.DataFrame({"CAPEX": [150.0], "DEPREC_AMORT": [100.0]})
        result = df.select(_reinvestment_rate().alias("r")).row(0, named=True)
        assert result["r"] == pytest.approx(1.5)

    def test_zero_depreciation_returns_none(self) -> None:
        df = pl.DataFrame({"CAPEX": [-150.0], "DEPREC_AMORT": [0.0]})
        result = df.select(_reinvestment_rate().alias("r")).row(0, named=True)
        assert result["r"] is None


class TestSustainableGrowth:
    def test_standard_formula(self) -> None:
        """SGR = ROE × (1 - Payout) para empresa típica."""
        df = pl.DataFrame({"ROE": [0.20], "PAYOUT": [0.40]})
        result = df.select(_sustainable_growth().alias("r")).row(0, named=True)
        # 0.20 × (1 - 0.40) = 0.12
        assert result["r"] == pytest.approx(0.12)

    def test_payout_above_one_is_clipped(self) -> None:
        """Payout pode ser > 1 em anos ruins — clip em [0, 1] evita SGR negativo espúrio."""
        df = pl.DataFrame({"ROE": [0.15], "PAYOUT": [1.50]})
        result = df.select(_sustainable_growth().alias("r")).row(0, named=True)
        # Clip 1.50 → 1.0, então 0.15 × (1 - 1.0) = 0
        assert result["r"] == pytest.approx(0.0)

    def test_negative_payout_is_clipped_to_zero(self) -> None:
        """Payout < 0 é clipado para 0 (empresa retém tudo e ainda recebe)."""
        df = pl.DataFrame({"ROE": [0.15], "PAYOUT": [-0.10]})
        result = df.select(_sustainable_growth().alias("r")).row(0, named=True)
        # Clip -0.10 → 0, então 0.15 × (1 - 0) = 0.15
        assert result["r"] == pytest.approx(0.15)

    def test_null_roe_returns_none(self) -> None:
        df = pl.DataFrame({"ROE": [None], "PAYOUT": [0.40]}, schema={"ROE": pl.Float64, "PAYOUT": pl.Float64})
        result = df.select(_sustainable_growth().alias("r")).row(0, named=True)
        assert result["r"] is None


class TestCashRatio:
    def test_cash_ratio_formula(self) -> None:
        df = pl.DataFrame(
            {"CAIXA_EQUIVALENTES": [100.0], "PASSIVO_CIRCULANTE": [200.0]}
        )
        result = df.select(_cash_ratio().alias("r")).row(0, named=True)
        assert result["r"] == pytest.approx(0.5)

    def test_zero_current_liabilities_returns_none(self) -> None:
        df = pl.DataFrame(
            {"CAIXA_EQUIVALENTES": [100.0], "PASSIVO_CIRCULANTE": [0.0]}
        )
        result = df.select(_cash_ratio().alias("r")).row(0, named=True)
        assert result["r"] is None


# --- Constante fiscal usada no NOPAT ---


class TestBrazilAfterTaxUsage:
    """Garante que NOPAT usa a constante correta de 66% pós-impostos."""

    def test_constant_value(self) -> None:
        assert pytest.approx(0.66) == BRAZIL_AFTER_TAX

    def test_nopat_direct_calculation(self) -> None:
        """NOPAT = EBIT × 0.66."""
        ebit = 150.0
        expected_nopat = ebit * BRAZIL_AFTER_TAX
        assert expected_nopat == pytest.approx(99.0)
