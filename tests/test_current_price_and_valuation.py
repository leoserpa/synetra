"""Testes do snapshot atual derivado do histórico diário.

Desde a simplificação do pipeline, o "snapshot" não depende mais de uma
chamada separada ao Yahoo para cotação intraday. Em vez disso:

    - `build_snapshot_atual(df_history)` usa o último fechamento disponível
      (coluna `PRECO_FIM_ANO`) como proxy do preço atual.

Cobre:
    1. Contrato do snapshot (colunas, 1 linha por ticker)
    2. Cálculos dos múltiplos atuais (MARKET_CAP_ATUAL, P_L_ATUAL, etc.)
    3. Assepsia setorial (EV → NULL para bancos e seguradoras)
    4. Proteções contra denominadores inválidos
    5. Property-based tests para invariantes matemáticos
"""
from __future__ import annotations

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from synetra.market.price_aggregator import (
    SNAPSHOT_COLS,
    build_snapshot_atual,
)

# --- Fixtures / helpers ---


def _history_industrial_row(**overrides: object) -> dict[str, object]:
    """Linha base (setor INDUSTRIAL) para construir DataFrame de história."""
    base: dict[str, object] = {
        "TICKER": "PETR4",
        "CNPJ_CIA": "33.000.167/0001-01",
        "RAZAO_CVM": "PETROLEO BRASILEIRO S.A.",
        "ANO": 2024,
        "CATEGORIA": "INDUSTRIAL",
        "QTDE_ACOES": 10_000.0,
        "QTDE_ON": 10_000.0,
        "QTDE_PN": 0.0,
        "LUCRO_LIQUIDO": 50_000.0,
        "PATRIMONIO_LIQUIDO": 500_000.0,
        "RECEITA_LIQUIDA": 400_000.0,
        "EBITDA": 150_000.0,
        "DIVIDA_LIQUIDA": 50_000.0,
        "PRECO_FIM_ANO": 30.0,
    }
    base.update(overrides)
    return base


# --- 1. Snapshot atual — estrutura ---


class TestSnapshotEstrutura:
    """Contrato do snapshot atual."""

    def test_snapshot_columns_match_contract(self) -> None:
        """Snapshot contém as colunas essenciais de SNAPSHOT_COLS."""
        history = pl.DataFrame([_history_industrial_row()])
        snapshot = build_snapshot_atual(history)

        # PRECO_ATUAL e DATA_COTACAO sempre devem existir no output
        assert "PRECO_ATUAL" in snapshot.columns
        assert "DATA_COTACAO" in snapshot.columns
        assert "MARKET_CAP_ATUAL" in snapshot.columns

        # Nenhuma coluna extra além das declaradas em SNAPSHOT_COLS
        for col in snapshot.columns:
            assert col in SNAPSHOT_COLS, f"Coluna inesperada no snapshot: {col}"

    def test_empty_history_returns_empty(self) -> None:
        empty = pl.DataFrame()
        assert build_snapshot_atual(empty).is_empty()

    def test_history_without_preco_fim_ano_returns_empty(self) -> None:
        """Sem PRECO_FIM_ANO não há base de preço — snapshot vazio."""
        row = _history_industrial_row()
        del row["PRECO_FIM_ANO"]
        history = pl.DataFrame([row])
        assert build_snapshot_atual(history).is_empty()

    def test_snapshot_one_row_per_ticker(self) -> None:
        """Snapshot retorna 1 linha por ticker (ano mais recente)."""
        rows = [
            _history_industrial_row(ANO=2022, PRECO_FIM_ANO=20.0),
            _history_industrial_row(ANO=2023, PRECO_FIM_ANO=25.0),
            _history_industrial_row(ANO=2024, PRECO_FIM_ANO=30.0),
        ]
        history = pl.DataFrame(rows)
        snapshot = build_snapshot_atual(history)
        assert snapshot.height == 1
        assert snapshot["ANO_REFERENCIA"][0] == 2024
        # Preço atual vira o último fechamento disponível (2024)
        assert snapshot["PRECO_ATUAL"][0] == pytest.approx(30.0)

    def test_data_cotacao_is_today(self) -> None:
        """DATA_COTACAO usa a data de hoje (formato YYYY-MM-DD)."""
        from datetime import date

        history = pl.DataFrame([_history_industrial_row()])
        snapshot = build_snapshot_atual(history)
        assert snapshot["DATA_COTACAO"][0] == date.today().isoformat()


# --- 2. Cálculos do snapshot — INDUSTRIAL ---


class TestSnapshotCalculosIndustrial:
    """Cálculos dos múltiplos atuais para empresa INDUSTRIAL."""

    def test_market_cap_atual(self) -> None:
        history = pl.DataFrame(
            [_history_industrial_row(PRECO_FIM_ANO=40.0, QTDE_ON=10_000.0, QTDE_PN=0.0)]
        )
        snapshot = build_snapshot_atual(history)
        # 40 × 10.000 = 400.000 (consolidado ON+PN)
        assert snapshot["MARKET_CAP_ATUAL"][0] == pytest.approx(400_000.0)

    def test_pl_atual(self) -> None:
        history = pl.DataFrame(
            [
                _history_industrial_row(
                    PRECO_FIM_ANO=40.0,
                    LUCRO_LIQUIDO=100_000.0,
                    QTDE_ACOES=10_000.0,
                )
            ]
        )
        snapshot = build_snapshot_atual(history)
        # LPA = 100k/10k = 10; P/L = 40/10 = 4.0
        assert snapshot["P_L_ATUAL"][0] == pytest.approx(4.0)

    def test_earnings_yield_eh_inverso_pl(self) -> None:
        history = pl.DataFrame(
            [
                _history_industrial_row(
                    PRECO_FIM_ANO=40.0,
                    LUCRO_LIQUIDO=100_000.0,
                    QTDE_ACOES=10_000.0,
                )
            ]
        )
        snapshot = build_snapshot_atual(history)
        # EY = LPA/Preço = 10/40 = 0.25
        assert snapshot["EARNINGS_YIELD"][0] == pytest.approx(0.25)

    def test_pvp_atual(self) -> None:
        history = pl.DataFrame(
            [
                _history_industrial_row(
                    PRECO_FIM_ANO=40.0,
                    PATRIMONIO_LIQUIDO=500_000.0,
                    QTDE_ACOES=10_000.0,
                )
            ]
        )
        snapshot = build_snapshot_atual(history)
        # VPA = 500k/10k = 50; P/VP = 40/50 = 0.8
        assert snapshot["P_VP_ATUAL"][0] == pytest.approx(0.8)

    def test_ev_ebitda_industrial(self) -> None:
        history = pl.DataFrame(
            [
                _history_industrial_row(
                    PRECO_FIM_ANO=40.0,
                    EBITDA=150_000.0,
                    DIVIDA_LIQUIDA=50_000.0,
                    QTDE_ACOES=10_000.0,
                )
            ]
        )
        snapshot = build_snapshot_atual(history)
        # MC = 400k; EV = MC+DL = 450k; EV/EBITDA = 450k/150k = 3.0
        assert snapshot["EV_EBITDA"][0] == pytest.approx(3.0)


# --- 3. Assepsia setorial ---


class TestSnapshotAssepsiaSetorial:
    """Assepsia: bancos e seguradoras não têm EV."""

    def test_banco_nao_tem_ev(self) -> None:
        history = pl.DataFrame(
            [_history_industrial_row(TICKER="ITUB4", CATEGORIA="FINANCEIRO")]
        )
        snapshot = build_snapshot_atual(history)
        assert snapshot["EV_EBITDA"][0] is None
        assert snapshot["EV_RECEITA"][0] is None
        # Mas P/L e P/VP existem mesmo em bancos
        assert snapshot["P_L_ATUAL"][0] is not None
        assert snapshot["P_VP_ATUAL"][0] is not None

    def test_seguradora_nao_tem_ev(self) -> None:
        history = pl.DataFrame(
            [_history_industrial_row(TICKER="PSSA3", CATEGORIA="SEGURADORA")]
        )
        snapshot = build_snapshot_atual(history)
        assert snapshot["EV_EBITDA"][0] is None
        assert snapshot["EV_RECEITA"][0] is None

    def test_industrial_mantem_ev(self) -> None:
        history = pl.DataFrame(
            [_history_industrial_row(TICKER="PETR4", CATEGORIA="INDUSTRIAL")]
        )
        snapshot = build_snapshot_atual(history)
        assert snapshot["EV_EBITDA"][0] is not None
        assert snapshot["EV_RECEITA"][0] is not None


# --- 4. Proteções contra denominadores inválidos ---


class TestSnapshotProteccoes:
    """Proteções: denominadores zero/negativos → NULL."""

    def test_lucro_zero_produz_pl_null(self) -> None:
        history = pl.DataFrame([_history_industrial_row(LUCRO_LIQUIDO=0.0)])
        snapshot = build_snapshot_atual(history)
        assert snapshot["P_L_ATUAL"][0] is None
        assert snapshot["EARNINGS_YIELD"][0] is None

    def test_patrimonio_negativo_produz_pvp_null(self) -> None:
        history = pl.DataFrame(
            [_history_industrial_row(PATRIMONIO_LIQUIDO=-100_000.0)]
        )
        snapshot = build_snapshot_atual(history)
        assert snapshot["P_VP_ATUAL"][0] is None

    def test_preco_fim_ano_null_produz_multiplos_null(self) -> None:
        """Sem PRECO_FIM_ANO, múltiplos não podem ser calculados."""
        history = pl.DataFrame([_history_industrial_row(PRECO_FIM_ANO=None)])
        snapshot = build_snapshot_atual(history)
        assert snapshot["P_L_ATUAL"][0] is None
        assert snapshot["MARKET_CAP_ATUAL"][0] is None


# --- 5. Property-Based Testing ---

precos_realistas = st.floats(
    min_value=1.0, max_value=500.0, allow_nan=False, allow_infinity=False,
)
qtdes_acoes = st.floats(
    min_value=1e5, max_value=1e10, allow_nan=False, allow_infinity=False,
)
patrimonios_positivos = st.floats(
    min_value=1e6, max_value=1e12, allow_nan=False, allow_infinity=False,
)


class TestPropertyBased:
    """Invariantes matemáticos que valem PARA TODO input válido."""

    @given(preco=precos_realistas, qtde=qtdes_acoes)
    @settings(max_examples=80, deadline=None)
    def test_mc_atual_sempre_positivo(
        self, preco: float, qtde: float,
    ) -> None:
        history = pl.DataFrame(
            [
                _history_industrial_row(
                    PRECO_FIM_ANO=preco,
                    QTDE_ACOES=qtde,
                    QTDE_ON=qtde,
                    QTDE_PN=0.0,
                )
            ]
        )
        snapshot = build_snapshot_atual(history)
        mc = snapshot["MARKET_CAP_ATUAL"][0]
        assert mc is not None
        assert mc > 0

    @given(
        preco=precos_realistas,
        patrimonio=patrimonios_positivos,
        qtde=qtdes_acoes,
    )
    @settings(max_examples=80, deadline=None)
    def test_pvp_positivo_quando_inputs_positivos(
        self, preco: float, patrimonio: float, qtde: float,
    ) -> None:
        history = pl.DataFrame(
            [
                _history_industrial_row(
                    PRECO_FIM_ANO=preco,
                    PATRIMONIO_LIQUIDO=patrimonio,
                    QTDE_ACOES=qtde,
                    QTDE_ON=qtde,
                    QTDE_PN=0.0,
                )
            ]
        )
        snapshot = build_snapshot_atual(history)
        p_vp = snapshot["P_VP_ATUAL"][0]
        assert p_vp is not None
        assert p_vp >= 0
