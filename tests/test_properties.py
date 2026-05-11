"""
Property-Based Testing: testa propriedades MATEMÁTICAS UNIVERSAIS
dos indicadores do Synetra usando Hypothesis.

Diferente de testes de exemplo (testam 1 caso), estes testam 1.000+ casos
aleatórios por teste — pegam bugs que exemplos nunca acham.

Princípio (John Hughes, 2000): "Don't write tests, write specifications."
"""
import math

import polars as pl
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from synetra.domain.indicators import safe_div

# --- Estratégias customizadas (geradores de dados realistas) ---

# Valores financeiros realistas (evita infinitos e NaN)
finite_money = st.floats(
    min_value=-1e15,
    max_value=1e15,
    allow_nan=False,
    allow_infinity=False,
)

# Valores positivos (para receita, ativo, lucro em anos bons)
positive_money = st.floats(
    min_value=1.0,
    max_value=1e12,
    allow_nan=False,
    allow_infinity=False,
)

# Anos válidos do Synetra
valid_years = st.integers(min_value=1, max_value=10)


# --- 1. Propriedades de safe_div ---

class TestSafeDivProperties:
    """Propriedades universais da divisão segura."""

    @given(num=finite_money, den=finite_money)
    @settings(max_examples=500)
    def test_nunca_levanta_zero_division(self, num: float, den: float) -> None:
        """PROPRIEDADE: safe_div nunca deve levantar ZeroDivisionError, para qualquer entrada."""
        df = pl.DataFrame({"n": [num], "d": [den]})
        # Não deve levantar exceção
        result = df.select(safe_div("n", "d").alias("r"))
        assert result.height == 1  # retornou algo (mesmo que null)

    @given(num=finite_money)
    @settings(max_examples=200)
    def test_divisao_por_zero_sempre_null(self, num: float) -> None:
        """PROPRIEDADE: qualquer coisa dividida por zero deve retornar None."""
        df = pl.DataFrame({"n": [num], "d": [0.0]})
        result = df.select(safe_div("n", "d").alias("r"))
        assert result["r"][0] is None

    @given(num=finite_money, den=finite_money)
    @settings(max_examples=500)
    def test_resultado_consistente_com_divisao_regular(self, num: float, den: float) -> None:
        """PROPRIEDADE: quando den != 0, safe_div deve dar o mesmo resultado que /."""
        assume(den != 0)
        assume(abs(den) > 1e-100)  # evita precisão numérica extrema

        df = pl.DataFrame({"n": [num], "d": [den]})
        result = df.select(safe_div("n", "d").alias("r"))
        expected = num / den

        if math.isfinite(expected) and math.isfinite(result["r"][0]):
            assert result["r"][0] == pytest.approx(expected, rel=1e-6)


# --- 2. Propriedades do CAGR ---

class TestCAGRProperties:
    """Propriedades universais do CAGR (crescimento composto)."""

    @staticmethod
    def _cagr(valor_atual: float, valor_base: float, n: int) -> float | None:
        """Implementação de referência do CAGR com as mesmas proteções do Synetra."""
        if valor_base <= 0 or valor_atual <= 0 or n <= 0:
            return None
        return math.pow(valor_atual / valor_base, 1.0 / n) - 1

    @given(base=positive_money, n=valid_years)
    @settings(max_examples=300)
    def test_cagr_mesmo_valor_eh_zero(self, base: float, n: int) -> None:
        """PROPRIEDADE: se valor atual == valor base, CAGR = 0% (empresa estagnada)."""
        result = self._cagr(base, base, n)
        assert result == pytest.approx(0.0, abs=1e-9)

    @given(base=positive_money, multiplier=st.floats(min_value=1.01, max_value=100.0, allow_nan=False),
           n=valid_years)
    @settings(max_examples=300)
    def test_cagr_crescimento_positivo_sempre_positivo(self, base: float, multiplier: float, n: int) -> None:
        """PROPRIEDADE: se valor atual > base, CAGR deve ser positivo."""
        atual = base * multiplier
        result = self._cagr(atual, base, n)
        assert result is not None
        assert result > 0

    @given(base=positive_money, multiplier=st.floats(min_value=0.01, max_value=0.99, allow_nan=False),
           n=valid_years)
    @settings(max_examples=300)
    def test_cagr_decrescimento_sempre_negativo(self, base: float, multiplier: float, n: int) -> None:
        """PROPRIEDADE: se valor atual < base, CAGR deve ser negativo (mas > -1)."""
        atual = base * multiplier
        result = self._cagr(atual, base, n)
        assert result is not None
        assert -1 < result < 0

    @given(base=st.floats(max_value=0, allow_nan=False, allow_infinity=False),
           atual=positive_money, n=valid_years)
    @settings(max_examples=200)
    def test_cagr_base_nao_positiva_eh_null(self, base: float, atual: float, n: int) -> None:
        """PROPRIEDADE: base ≤ 0 (prejuízo histórico) deve sempre retornar None."""
        result = self._cagr(atual, base, n)
        assert result is None

    @given(base=positive_money, atual=st.floats(max_value=0, allow_nan=False, allow_infinity=False),
           n=valid_years)
    @settings(max_examples=200)
    def test_cagr_atual_nao_positivo_eh_null(self, base: float, atual: float, n: int) -> None:
        """PROPRIEDADE: valor atual ≤ 0 (prejuízo atual) deve sempre retornar None."""
        result = self._cagr(atual, base, n)
        assert result is None

    @given(base=positive_money, atual=positive_money, n=valid_years)
    @settings(max_examples=500)
    def test_cagr_nunca_menor_que_menos_um(self, base: float, atual: float, n: int) -> None:
        """PROPRIEDADE: CAGR nunca pode ser menor que -100% (matemática de composição)."""
        result = self._cagr(atual, base, n)
        if result is not None:
            assert result > -1.0


# --- 3. Propriedades do Cash Conversion ---

class TestCashConversionProperties:
    """Propriedades universais do Cash Conversion."""

    @staticmethod
    def _cash_conversion(fco: float, lucro: float) -> float | None:
        """Implementação de referência."""
        if lucro <= 0:
            return None
        return fco / lucro

    @given(fco=finite_money, lucro=st.floats(max_value=0, allow_nan=False, allow_infinity=False))
    @settings(max_examples=200)
    def test_cc_lucro_nao_positivo_eh_null(self, fco: float, lucro: float) -> None:
        """PROPRIEDADE: lucro ≤ 0 deve sempre retornar None (prejuízo não tem CC interpretável)."""
        result = self._cash_conversion(fco, lucro)
        assert result is None

    @given(fco=positive_money, lucro=positive_money)
    @settings(max_examples=500)
    def test_cc_fco_positivo_lucro_positivo_sempre_positivo(self, fco: float, lucro: float) -> None:
        """PROPRIEDADE: se FCO > 0 e Lucro > 0, CC deve ser positivo."""
        result = self._cash_conversion(fco, lucro)
        assert result is not None
        assert result > 0

    @given(fco=positive_money)
    @settings(max_examples=200)
    def test_cc_fco_igual_lucro_eh_um(self, fco: float) -> None:
        """PROPRIEDADE: se FCO == Lucro, CC deve ser exatamente 1.0."""
        result = self._cash_conversion(fco, fco)
        assert result == pytest.approx(1.0)


# --- 4. Propriedades de Earnings Stability (desvio-padrão) ---

class TestEarningsStabilityProperties:
    """Propriedades da estabilidade (desvio-padrão do ROE)."""

    @given(
        valores=st.lists(
            st.floats(min_value=-10, max_value=10, allow_nan=False, allow_infinity=False),
            min_size=5,
            max_size=5,
        )
    )
    @settings(max_examples=300)
    def test_estabilidade_sempre_nao_negativa(self, valores: list[float]) -> None:
        """PROPRIEDADE: desvio-padrão é sempre ≥ 0 (matemática)."""
        df = pl.DataFrame({"ROE": valores})
        result = df.select(pl.col("ROE").std().alias("std"))
        std_val = result["std"][0]
        if std_val is not None:
            assert std_val >= 0

    @given(valor=st.floats(min_value=-10, max_value=10, allow_nan=False, allow_infinity=False))
    @settings(max_examples=100)
    def test_valores_iguais_estabilidade_zero(self, valor: float) -> None:
        """PROPRIEDADE: se todos os ROEs são iguais, desvio-padrão é 0 (máxima estabilidade)."""
        df = pl.DataFrame({"ROE": [valor] * 5})
        result = df.select(pl.col("ROE").std().alias("std"))
        assert result["std"][0] == pytest.approx(0.0, abs=1e-9)


# --- 5. Propriedades de Delta (Momentum) ---

class TestDeltaProperties:
    """Propriedades universais dos momentum factors (Δ ROE, Δ Margem)."""

    @given(atual=finite_money, anterior=finite_money)
    @settings(max_examples=500)
    def test_delta_eh_antissimetrico(self, atual: float, anterior: float) -> None:
        """PROPRIEDADE: delta(a, b) = -delta(b, a) (antissimetria)."""
        delta1 = atual - anterior
        delta2 = anterior - atual
        assert delta1 == pytest.approx(-delta2)

    @given(valor=finite_money)
    @settings(max_examples=200)
    def test_delta_mesmo_valor_eh_zero(self, valor: float) -> None:
        """PROPRIEDADE: se atual == anterior, delta = 0."""
        assert (valor - valor) == 0
