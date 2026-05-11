"""Testes da prioridade do Lucro Atribuído à Controladora no LUCRO_FINAL.

Regra do Synetra (alinhada ao padrão de mercado - StatusInvest, Bloomberg):
    Prioridade no LUCRO_FINAL (do mais para o menos preferido):
        1. LUCRO_CONTROLADORA (conta 3.11.01) — lucro atribuído aos sócios
           da empresa controladora (exclui participação de minoritários)
        2. LUCRO_CONTROLADORA_BCO (conta 3.09.01) — fallback para bancos
        3. LUCRO_LIQUIDO (conta 3.11) — lucro consolidado total
        4. LUCRO_LIQUIDO_BCO (conta 3.09) — fallback final

Isso é importante para casos como:
    - Petrobras 2017: Consolidado = +R$ 0.38 bi, Controladora = -R$ 0.45 bi
      (empresa teve prejuízo atribuído aos acionistas, mas minoritários "salvaram" o consolidado)
    - Vale: Consolidado costuma ser maior que controladora (minoritários de Samarco, etc.)
"""
from __future__ import annotations

import polars as pl
import pytest

from synetra.config import SynetraConfig, load_config
from synetra.transformer import FinancialTransformer

# --- Fixtures ---

def _make_history_row(
    cnpj: str, ano: int, categoria: str,
    cd_conta: str, ds_conta: str, vl_conta: float,
) -> dict[str, object]:
    """Helper para criar uma linha do df_history bruto."""
    return {
        "CNPJ_CIA": cnpj,
        "ANO": ano,
        "CATEGORIA": categoria,
        "SETOR_ATIV": "Petroleo",
        "CD_CONTA": cd_conta,
        "DS_CONTA": ds_conta,
        "VL_CONTA": vl_conta,
        "DOC_TYPE": "DRE",
    }


@pytest.fixture
def config() -> SynetraConfig:
    """Carrega config real do projeto."""
    return load_config("config.toml")


@pytest.fixture
def transformer(config: SynetraConfig) -> FinancialTransformer:
    return FinancialTransformer(config)


# --- Testes ---

class TestPrioridadeLucroControladora:
    """Valida que LUCRO_CONTROLADORA tem prioridade sobre LUCRO_LIQUIDO."""

    def test_usa_controladora_quando_disponivel(
        self, transformer: FinancialTransformer
    ) -> None:
        """Empresa industrial com 3.11 e 3.11.01 → LUCRO_FINAL usa 3.11.01."""
        history = pl.DataFrame([
            # Lucro consolidado total: +R$ 1 bi
            _make_history_row(
                "11.111.111/0001-00", 2023, "INDUSTRIAL",
                "3.11", "Lucro/Prejuizo Consolidado", 1_000_000_000.0,
            ),
            # Lucro controladora: -R$ 500 mi (o que importa)
            _make_history_row(
                "11.111.111/0001-00", 2023, "INDUSTRIAL",
                "3.11.01", "Atribuido a Controladora", -500_000_000.0,
            ),
            _make_history_row(
                "11.111.111/0001-00", 2023, "INDUSTRIAL",
                "3.01", "Receita", 10_000_000_000.0,
            ),
        ])

        matches = pl.DataFrame({
            "CNPJ_CIA": ["11.111.111/0001-00"],
            "TICKER": ["TEST3"],
            "RAZAO_CVM": ["TEST S.A."],
        })
        setor = pl.DataFrame({
            "CNPJ_CIA": ["11.111.111/0001-00"],
            "SETOR_ATIV": ["Petroleo"],
        })

        result = transformer.calculate_indicators(history, matches, setor)
        linha = result.filter(pl.col("TICKER") == "TEST3").row(0, named=True)

        # LUCRO_FINAL deve ser -500 mi (controladora), não +1 bi (consolidado)
        assert linha["LUCRO_LIQUIDO"] == -500_000_000.0

    def test_fallback_para_consolidado_quando_controladora_ausente(
        self, transformer: FinancialTransformer
    ) -> None:
        """Empresa sem 3.11.01 (ex: WEG) → LUCRO_FINAL usa 3.11."""
        history = pl.DataFrame([
            _make_history_row(
                "22.222.222/0001-00", 2023, "INDUSTRIAL",
                "3.11", "Lucro Liquido", 2_000_000_000.0,
            ),
            _make_history_row(
                "22.222.222/0001-00", 2023, "INDUSTRIAL",
                "3.01", "Receita", 10_000_000_000.0,
            ),
        ])

        matches = pl.DataFrame({
            "CNPJ_CIA": ["22.222.222/0001-00"],
            "TICKER": ["WEG3"],
            "RAZAO_CVM": ["WEG S.A."],
        })
        setor = pl.DataFrame({
            "CNPJ_CIA": ["22.222.222/0001-00"],
            "SETOR_ATIV": ["Maquinas"],
        })

        result = transformer.calculate_indicators(history, matches, setor)
        linha = result.filter(pl.col("TICKER") == "WEG3").row(0, named=True)

        # Sem 3.11.01 → LUCRO_LIQUIDO vem de 3.11 (consolidado = 2 bi)
        assert linha["LUCRO_LIQUIDO"] == 2_000_000_000.0

    def test_banco_usa_controladora_bco_quando_disponivel(
        self, transformer: FinancialTransformer
    ) -> None:
        """Banco com 3.09 e 3.09.01 → usa 3.09.01 (padrão Itaú)."""
        history = pl.DataFrame([
            # Lucro consolidado bancário: +R$ 42 bi
            _make_history_row(
                "33.333.333/0001-00", 2023, "FINANCEIRO",
                "3.09", "Lucro Consolidado", 42_000_000_000.0,
            ),
            # Lucro controladora: +R$ 41 bi (minoritários ficaram com R$ 1 bi)
            _make_history_row(
                "33.333.333/0001-00", 2023, "FINANCEIRO",
                "3.09.01", "Atribuido a Controladora", 41_000_000_000.0,
            ),
            _make_history_row(
                "33.333.333/0001-00", 2023, "FINANCEIRO",
                "3.01", "Receita", 100_000_000_000.0,
            ),
        ])

        matches = pl.DataFrame({
            "CNPJ_CIA": ["33.333.333/0001-00"],
            "TICKER": ["BANK4"],
            "RAZAO_CVM": ["BANCO TESTE"],
        })
        setor = pl.DataFrame({
            "CNPJ_CIA": ["33.333.333/0001-00"],
            "SETOR_ATIV": ["BANCO"],
        })

        result = transformer.calculate_indicators(history, matches, setor)
        linha = result.filter(pl.col("TICKER") == "BANK4").row(0, named=True)

        # LUCRO_LIQUIDO (para banco vem de 3.09) deve ser 41 bi (controladora)
        assert linha["LUCRO_LIQUIDO"] == 41_000_000_000.0

    def test_preserva_comportamento_quando_controladora_eh_zero(
        self, transformer: FinancialTransformer
    ) -> None:
        """Se 3.11.01 existir mas for zero, cai para 3.11 (consolidado)."""
        history = pl.DataFrame([
            _make_history_row(
                "44.444.444/0001-00", 2023, "INDUSTRIAL",
                "3.11", "Lucro Consolidado", 1_500_000_000.0,
            ),
            # Controladora zerado (caso raro)
            _make_history_row(
                "44.444.444/0001-00", 2023, "INDUSTRIAL",
                "3.11.01", "Atribuido a Controladora", 0.0,
            ),
            _make_history_row(
                "44.444.444/0001-00", 2023, "INDUSTRIAL",
                "3.01", "Receita", 5_000_000_000.0,
            ),
        ])

        matches = pl.DataFrame({
            "CNPJ_CIA": ["44.444.444/0001-00"],
            "TICKER": ["TEST4"],
            "RAZAO_CVM": ["TEST 4 S.A."],
        })
        setor = pl.DataFrame({
            "CNPJ_CIA": ["44.444.444/0001-00"],
            "SETOR_ATIV": ["Petroleo"],
        })

        result = transformer.calculate_indicators(history, matches, setor)
        linha = result.filter(pl.col("TICKER") == "TEST4").row(0, named=True)

        # Controladora zero → fallback para consolidado (1.5 bi)
        assert linha["LUCRO_LIQUIDO"] == 1_500_000_000.0
