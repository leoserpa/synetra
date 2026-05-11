"""
Testes do agregador de cotações (diário → anual).

Skill: python-testing-patterns → Unit + Property Testing

Escopo:
    - `aggregate_to_yearly`: regra de agregação (último, média, volume)
    - `attach_prices_to_history`: merge por (TICKER, ANO) com LEFT JOIN

Garantias testadas:
    1. PRECO_FIM_ANO = último fechamento do ano (ordenado por DATA)
    2. PRECO_MEDIO_ANO = média aritmética simples dos fechamentos
    3. VOLUME_MEDIO = média do volume diário (arredondado)
    4. Empresa sem cotação → 3 colunas ficam NULL (não quebra)
    5. Contrato do schema: as 3 colunas SEMPRE aparecem no output
    6. Idempotência: rodar 2x dá o mesmo resultado
    7. Propriedades matemáticas via Hypothesis (min ≤ média ≤ max)
"""
from __future__ import annotations

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from synetra.market.price_aggregator import aggregate_to_yearly, attach_prices_to_history

# --- Helpers ---

def _precos_diarios(rows: list[dict]) -> pl.DataFrame:
    """Cria DataFrame no schema esperado pelo agregador."""
    # Preenche colunas que não vêm mas são opcionais do schema
    for r in rows:
        r.setdefault("ABERTURA", r["FECHAMENTO"])
        r.setdefault("MAXIMA", r["FECHAMENTO"])
        r.setdefault("MINIMA", r["FECHAMENTO"])
        r.setdefault("FECHAMENTO_AJUSTADO", r["FECHAMENTO"])
    return pl.DataFrame(rows)


# --- aggregate_to_yearly — regras de agregação ---

class TestAggregateToYearly:
    """Testa a agregação diário → anual."""

    def test_dataframe_vazio_retorna_vazio(self) -> None:
        vazio = pl.DataFrame()
        result = aggregate_to_yearly(vazio)
        assert result.is_empty()

    def test_schema_de_saida(self) -> None:
        """Saída deve ter exatamente: TICKER, ANO, PRECO_FIM_ANO, PRECO_MEDIO_ANO, VOLUME_MEDIO."""
        df = _precos_diarios([
            {"TICKER": "PETR4", "DATA": "2024-01-02", "FECHAMENTO": 38.0, "VOLUME": 1_000_000.0},
            {"TICKER": "PETR4", "DATA": "2024-12-30", "FECHAMENTO": 40.0, "VOLUME": 2_000_000.0},
        ])
        r = aggregate_to_yearly(df)
        assert set(r.columns) == {"TICKER", "ANO", "PRECO_FIM_ANO", "PRECO_MEDIO_ANO", "VOLUME_MEDIO"}

    def test_preco_fim_ano_eh_ultimo_pregao(self) -> None:
        """PRECO_FIM_ANO deve ser o último fechamento cronológico do ano."""
        df = _precos_diarios([
            {"TICKER": "PETR4", "DATA": "2024-01-02", "FECHAMENTO": 30.0, "VOLUME": 1e6},
            {"TICKER": "PETR4", "DATA": "2024-06-15", "FECHAMENTO": 35.0, "VOLUME": 1e6},
            {"TICKER": "PETR4", "DATA": "2024-12-30", "FECHAMENTO": 42.0, "VOLUME": 1e6},
        ])
        r = aggregate_to_yearly(df)
        assert r.filter(pl.col("ANO") == 2024)["PRECO_FIM_ANO"][0] == 42.0

    def test_preco_fim_ano_eh_robusto_a_ordem_de_entrada(self) -> None:
        """Mesmo se linhas chegarem desordenadas, PRECO_FIM_ANO é o mais recente."""
        df = _precos_diarios([
            {"TICKER": "PETR4", "DATA": "2024-12-30", "FECHAMENTO": 42.0, "VOLUME": 1e6},
            {"TICKER": "PETR4", "DATA": "2024-01-02", "FECHAMENTO": 30.0, "VOLUME": 1e6},
            {"TICKER": "PETR4", "DATA": "2024-06-15", "FECHAMENTO": 35.0, "VOLUME": 1e6},
        ])
        r = aggregate_to_yearly(df)
        assert r.filter(pl.col("ANO") == 2024)["PRECO_FIM_ANO"][0] == 42.0

    def test_preco_medio_ano_eh_media_aritmetica(self) -> None:
        """PRECO_MEDIO_ANO = média simples dos fechamentos do ano."""
        df = _precos_diarios([
            {"TICKER": "VALE3", "DATA": "2024-01-02", "FECHAMENTO": 60.0, "VOLUME": 1e6},
            {"TICKER": "VALE3", "DATA": "2024-06-15", "FECHAMENTO": 70.0, "VOLUME": 1e6},
            {"TICKER": "VALE3", "DATA": "2024-12-30", "FECHAMENTO": 80.0, "VOLUME": 1e6},
        ])
        r = aggregate_to_yearly(df)
        # (60 + 70 + 80) / 3 = 70.0
        assert r.filter(pl.col("ANO") == 2024)["PRECO_MEDIO_ANO"][0] == 70.0

    def test_volume_medio_arredondado(self) -> None:
        """VOLUME_MEDIO é a média diária do volume (arredondada)."""
        df = _precos_diarios([
            {"TICKER": "ITUB4", "DATA": "2024-01-02", "FECHAMENTO": 30.0, "VOLUME": 1_000_000.0},
            {"TICKER": "ITUB4", "DATA": "2024-06-15", "FECHAMENTO": 32.0, "VOLUME": 2_000_000.0},
        ])
        r = aggregate_to_yearly(df)
        assert r.filter(pl.col("ANO") == 2024)["VOLUME_MEDIO"][0] == 1_500_000.0

    def test_separa_por_ticker_e_ano(self) -> None:
        """2 tickers × 2 anos = 4 linhas."""
        df = _precos_diarios([
            {"TICKER": "PETR4", "DATA": "2023-12-29", "FECHAMENTO": 32.0, "VOLUME": 1e6},
            {"TICKER": "PETR4", "DATA": "2024-12-30", "FECHAMENTO": 40.0, "VOLUME": 1e6},
            {"TICKER": "VALE3", "DATA": "2023-12-29", "FECHAMENTO": 70.0, "VOLUME": 1e6},
            {"TICKER": "VALE3", "DATA": "2024-12-30", "FECHAMENTO": 80.0, "VOLUME": 1e6},
        ])
        r = aggregate_to_yearly(df)
        assert r.height == 4

    def test_um_unico_dia_produz_valores_iguais(self) -> None:
        """Se ticker tem só 1 pregão no ano, fim_ano == medio_ano == aquele valor."""
        df = _precos_diarios([
            {"TICKER": "IPO1", "DATA": "2024-12-30", "FECHAMENTO": 25.0, "VOLUME": 100_000.0},
        ])
        r = aggregate_to_yearly(df)
        assert r["PRECO_FIM_ANO"][0] == 25.0
        assert r["PRECO_MEDIO_ANO"][0] == 25.0

    def test_precos_arredondados_para_duas_casas(self) -> None:
        """Padrão de mercado: preços com 2 casas decimais."""
        df = _precos_diarios([
            {"TICKER": "PETR4", "DATA": "2024-06-15", "FECHAMENTO": 38.123456, "VOLUME": 1e6},
            {"TICKER": "PETR4", "DATA": "2024-06-16", "FECHAMENTO": 38.876543, "VOLUME": 1e6},
        ])
        r = aggregate_to_yearly(df)
        fim = r["PRECO_FIM_ANO"][0]
        med = r["PRECO_MEDIO_ANO"][0]
        # Checa que tem no máximo 2 casas decimais
        assert abs(fim - round(fim, 2)) < 1e-9
        assert abs(med - round(med, 2)) < 1e-9


# --- attach_prices_to_history — merge por (TICKER, ANO) ---

class TestAttachPricesToHistory:
    """Testa o merge contábil + preços."""

    def test_precos_vazios_anexam_colunas_nulas(self) -> None:
        """Se cotações estão vazias, as 3 colunas são adicionadas como NULL."""
        history = pl.DataFrame({
            "TICKER": ["PETR4"], "ANO": [2024], "RECEITA": [1000.0],
        })
        resultado = attach_prices_to_history(history, pl.DataFrame())

        assert "PRECO_FIM_ANO" in resultado.columns
        assert "PRECO_MEDIO_ANO" in resultado.columns
        assert "VOLUME_MEDIO" in resultado.columns
        assert resultado["PRECO_FIM_ANO"][0] is None

    def test_merge_basico_ok(self) -> None:
        """Merge por (TICKER, ANO) anexa as 3 colunas de preço."""
        history = pl.DataFrame({
            "TICKER": ["PETR4", "VALE3"],
            "ANO": [2024, 2024],
            "RECEITA": [1000.0, 2000.0],
        })
        precos = _precos_diarios([
            {"TICKER": "PETR4", "DATA": "2024-12-30", "FECHAMENTO": 40.0, "VOLUME": 1e6},
            {"TICKER": "VALE3", "DATA": "2024-12-30", "FECHAMENTO": 80.0, "VOLUME": 2e6},
        ])

        r = attach_prices_to_history(history, precos)
        r_petr = r.filter(pl.col("TICKER") == "PETR4")
        r_vale = r.filter(pl.col("TICKER") == "VALE3")

        assert r_petr["PRECO_FIM_ANO"][0] == 40.0
        assert r_vale["PRECO_FIM_ANO"][0] == 80.0

    def test_ticker_sem_cotacao_fica_null(self) -> None:
        """Ticker no history mas SEM cotação no Yahoo → PRECO_FIM_ANO é NULL."""
        history = pl.DataFrame({
            "TICKER": ["PETR4", "ARND3"],
            "ANO": [2024, 2024],
            "RECEITA": [1000.0, 50.0],
        })
        precos = _precos_diarios([
            {"TICKER": "PETR4", "DATA": "2024-12-30", "FECHAMENTO": 40.0, "VOLUME": 1e6},
        ])

        r = attach_prices_to_history(history, precos)
        assert r.filter(pl.col("TICKER") == "PETR4")["PRECO_FIM_ANO"][0] == 40.0
        assert r.filter(pl.col("TICKER") == "ARND3")["PRECO_FIM_ANO"][0] is None

    def test_ipo_recente_anos_anteriores_ficam_null(self) -> None:
        """IPO em 2022 → anos 2020, 2021 ficam com PRECO_FIM_ANO = NULL."""
        history = pl.DataFrame({
            "TICKER": ["FIQE3", "FIQE3", "FIQE3"],
            "ANO": [2020, 2021, 2022],
            "RECEITA": [100.0, 120.0, 150.0],
        })
        precos = _precos_diarios([
            {"TICKER": "FIQE3", "DATA": "2022-12-30", "FECHAMENTO": 15.0, "VOLUME": 1e5},
        ])

        r = attach_prices_to_history(history, precos).sort("ANO")
        assert r["PRECO_FIM_ANO"][0] is None     # 2020
        assert r["PRECO_FIM_ANO"][1] is None     # 2021
        assert r["PRECO_FIM_ANO"][2] == 15.0     # 2022

    def test_contrato_do_schema_preservado(self) -> None:
        """Colunas originais do history continuam presentes após o merge."""
        history = pl.DataFrame({
            "TICKER": ["PETR4"], "ANO": [2024],
            "RECEITA": [1000.0], "LUCRO_LIQUIDO": [100.0], "CNPJ": ["00.000/0001-00"],
        })
        precos = _precos_diarios([
            {"TICKER": "PETR4", "DATA": "2024-12-30", "FECHAMENTO": 40.0, "VOLUME": 1e6},
        ])
        r = attach_prices_to_history(history, precos)

        for col in ("TICKER", "ANO", "RECEITA", "LUCRO_LIQUIDO", "CNPJ"):
            assert col in r.columns

    def test_idempotencia_do_pipeline(self) -> None:
        """Rodar o merge 2x não muda o resultado."""
        history = pl.DataFrame({
            "TICKER": ["PETR4"], "ANO": [2024], "RECEITA": [1000.0],
        })
        precos = _precos_diarios([
            {"TICKER": "PETR4", "DATA": "2024-12-30", "FECHAMENTO": 40.0, "VOLUME": 1e6},
        ])
        r1 = attach_prices_to_history(history, precos)

        # Preparar input idêntico e rodar de novo
        r2 = attach_prices_to_history(
            history, precos
        )
        assert r1.equals(r2)

    def test_ano_tipo_diferente_compatibiliza(self) -> None:
        """Se history.ANO for Int64 e preco.ANO for Int32, merge funciona."""
        history = pl.DataFrame({
            "TICKER": ["PETR4"], "ANO": pl.Series([2024], dtype=pl.Int64),
            "RECEITA": [1000.0],
        })
        precos = _precos_diarios([
            {"TICKER": "PETR4", "DATA": "2024-12-30", "FECHAMENTO": 40.0, "VOLUME": 1e6},
        ])
        r = attach_prices_to_history(history, precos)
        assert r["PRECO_FIM_ANO"][0] == 40.0


# --- Property-Based Testing — propriedades matemáticas universais ---

# Preços realistas da B3 (penny stock a blue chip)
precos_realistas = st.floats(
    min_value=0.10, max_value=500.0, allow_nan=False, allow_infinity=False,
)
volumes_realistas = st.floats(
    min_value=0.0, max_value=1e10, allow_nan=False, allow_infinity=False,
)


class TestPropertyBased:
    """Propriedades que devem valer PARA TODO input válido."""

    @given(
        precos=st.lists(precos_realistas, min_size=1, max_size=250),
    )
    @settings(max_examples=100, deadline=None)
    def test_media_esta_entre_min_e_max(self, precos: list[float]) -> None:
        """Propriedade: min(precos) ≤ PRECO_MEDIO_ANO ≤ max(precos).

        Invariante matemática de toda média aritmética.
        """
        rows = [
            {
                "TICKER": "TEST", "DATA": f"2024-{(i % 12) + 1:02d}-15",
                "FECHAMENTO": p, "VOLUME": 1e6,
            }
            for i, p in enumerate(precos)
        ]
        df = _precos_diarios(rows)
        r = aggregate_to_yearly(df)

        medio = r["PRECO_MEDIO_ANO"][0]
        # Tolerância para o arredondamento de 2 casas
        assert min(precos) - 0.005 <= medio <= max(precos) + 0.005

    @given(
        precos=st.lists(precos_realistas, min_size=1, max_size=50),
    )
    @settings(max_examples=50, deadline=None)
    def test_preco_fim_ano_eh_exatamente_o_ultimo_cronologico(
        self, precos: list[float],
    ) -> None:
        """Propriedade: PRECO_FIM_ANO deve ser o valor do último DATA do ano."""
        rows = []
        # datas monotonicamente crescentes dentro de 2024
        for i, p in enumerate(precos):
            month = (i // 28) + 1
            day = (i % 28) + 1
            if month > 12:
                break
            rows.append({
                "TICKER": "TEST",
                "DATA": f"2024-{month:02d}-{day:02d}",
                "FECHAMENTO": p, "VOLUME": 1e6,
            })

        if not rows:
            return

        df = _precos_diarios(rows)
        r = aggregate_to_yearly(df)
        ultimo_fechamento = rows[-1]["FECHAMENTO"]
        assert isinstance(ultimo_fechamento, float)
        esperado = round(ultimo_fechamento, 2)
        assert r["PRECO_FIM_ANO"][0] == pytest.approx(esperado, abs=0.01)

    @given(
        n_tickers=st.integers(min_value=1, max_value=10),
        n_dias=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=30, deadline=None)
    def test_agregacao_produz_uma_linha_por_ticker_ano(
        self, n_tickers: int, n_dias: int,
    ) -> None:
        """Propriedade: agregação deve produzir exatamente 1 linha por (TICKER, ANO)."""
        rows = []
        for t in range(n_tickers):
            for d in range(n_dias):
                month = (d // 28) + 1
                day = (d % 28) + 1
                if month > 12:
                    continue
                rows.append({
                    "TICKER": f"T{t}",
                    "DATA": f"2024-{month:02d}-{day:02d}",
                    "FECHAMENTO": 10.0 + d,
                    "VOLUME": 1e6,
                })
        if not rows:
            return

        df = _precos_diarios(rows)
        r = aggregate_to_yearly(df)

        # Cada (TICKER, ANO=2024) aparece 1x
        combos = r.group_by(["TICKER", "ANO"]).len()
        assert combos["len"].max() == 1

    @given(
        n_rows=st.integers(min_value=1, max_value=30),
    )
    @settings(max_examples=20, deadline=None)
    def test_attach_preserva_numero_de_linhas(self, n_rows: int) -> None:
        """Propriedade: attach_prices_to_history nunca altera o n_rows do history."""
        history = pl.DataFrame({
            "TICKER": [f"T{i}" for i in range(n_rows)],
            "ANO": [2024] * n_rows,
            "RECEITA": [100.0 * (i + 1) for i in range(n_rows)],
        })
        precos = _precos_diarios([
            {"TICKER": "T0", "DATA": "2024-12-30", "FECHAMENTO": 10.0, "VOLUME": 1e6},
        ])
        r = attach_prices_to_history(history, precos)
        assert r.height == n_rows
