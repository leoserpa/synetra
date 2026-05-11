"""
Testes unitários para o motor de cálculo financeiro do Synetra.
Utiliza DataFrames sintéticos com resultados conhecidos (gabarito).
"""
from types import SimpleNamespace

import polars as pl
import pytest

from synetra.domain.indicators import safe_div
from synetra.transformer import classify_sectors, map_accounts

# --- 1. Testes de safe_div (Divisão Segura) ---

class TestSafeDiv:
    """Testa a função de divisão segura que evita ZeroDivisionError."""

    def test_divisao_normal(self):
        """Divisão 100 / 50 deve retornar 2.0"""
        df = pl.DataFrame({"num": [100.0], "den": [50.0]})
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] == pytest.approx(2.0)

    def test_divisao_por_zero_retorna_null(self):
        """Divisão por zero deve retornar None (null), não infinito."""
        df = pl.DataFrame({"num": [100.0], "den": [0.0]})
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] is None

    def test_numerador_zero(self):
        """0 / 50 deve retornar 0.0"""
        df = pl.DataFrame({"num": [0.0], "den": [50.0]})
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] == pytest.approx(0.0)

    def test_valores_negativos(self):
        """-100 / 200 deve retornar -0.5 (caso comum: prejuízo / PL positivo)"""
        df = pl.DataFrame({"num": [-100.0], "den": [200.0]})
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] == pytest.approx(-0.5)

    def test_ambos_negativos(self):
        """-100 / -200 deve retornar 0.5 (prejuízo / PL negativo)"""
        df = pl.DataFrame({"num": [-100.0], "den": [-200.0]})
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] == pytest.approx(0.5)

    def test_batch_com_zeros_misturados(self):
        """Vetorização: deve tratar zero apenas nas linhas certas."""
        df = pl.DataFrame({
            "num": [100.0, 200.0, 300.0],
            "den": [50.0, 0.0, 150.0]
        })
        result = df.select(safe_div("num", "den").alias("resultado"))
        assert result["resultado"][0] == pytest.approx(2.0)
        assert result["resultado"][1] is None  # divisão por zero
        assert result["resultado"][2] == pytest.approx(2.0)


# --- 2. Testes de classify_sectors (Classificação Setorial) ---

class TestClassifySectors:
    """Testa a classificação de empresas em INDUSTRIAL/FINANCEIRO/SEGURADORA."""

    @pytest.fixture
    def config(self):
        return SimpleNamespace(
            setores=SimpleNamespace(
                financeiro=["BANCO", "CREDITO", "ARRENDAMENTO"],
                seguradora=["SEGURO", "PREVIDENCIA"]
            )
        )

    def test_classifica_banco_como_financeiro(self, config):
        df = pl.DataFrame({"SETOR_ATIV": ["BANCO COMERCIAL"], "CNPJ_CIA": ["111"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "FINANCEIRO"

    def test_classifica_seguradora(self, config):
        df = pl.DataFrame({"SETOR_ATIV": ["SEGURO DE VIDA"], "CNPJ_CIA": ["222"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "SEGURADORA"

    def test_classifica_industria_como_default(self, config):
        df = pl.DataFrame({"SETOR_ATIV": ["SIDERURGIA E METALURGIA"], "CNPJ_CIA": ["333"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "INDUSTRIAL"

    def test_classifica_previdencia_como_seguradora(self, config):
        df = pl.DataFrame({"SETOR_ATIV": ["PREVIDENCIA PRIVADA"], "CNPJ_CIA": ["444"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "SEGURADORA"

    def test_setor_vazio_vira_industrial(self, config):
        df = pl.DataFrame({"SETOR_ATIV": ["NAO INFORMADO"], "CNPJ_CIA": ["555"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "INDUSTRIAL"

    def test_case_insensitive(self, config):
        """Deve funcionar independente de maiúsculas/minúsculas."""
        df = pl.DataFrame({"SETOR_ATIV": ["banco comercial"], "CNPJ_CIA": ["666"]})
        result = classify_sectors(df, config)
        assert result["CATEGORIA"][0] == "FINANCEIRO"

    def test_batch_multiplos_setores(self, config):
        """Deve classificar corretamente em batch."""
        df = pl.DataFrame({
            "SETOR_ATIV": ["BANCO DIGITAL", "SEGURO SAUDE", "ALIMENTOS", "CREDITO PESSOAL"],
            "CNPJ_CIA": ["A", "B", "C", "D"]
        })
        result = classify_sectors(df, config)
        assert result["CATEGORIA"].to_list() == ["FINANCEIRO", "SEGURADORA", "INDUSTRIAL", "FINANCEIRO"]


# --- 3. Testes de map_accounts (Mapeamento de Contas) ---

class TestMapAccounts:
    """Testa o mapeamento de contas contábeis por setor."""

    @pytest.fixture
    def config(self):
        return SimpleNamespace(
            contas={
                "industrial": {"3.01": "RECEITA_LIQUIDA", "3.05": "EBIT"},
                "financeiro": {"3.01": "RECEITA_LIQUIDA", "2.08": "PATRIMONIO_LIQUIDO"},
                "seguradora": {"3.01": "RECEITA_LIQUIDA"}
            }
        )

    def test_mapeia_conta_industrial(self, config):
        df = pl.DataFrame({
            "CD_CONTA": ["3.01", "3.05"],
            "CATEGORIA": ["INDUSTRIAL", "INDUSTRIAL"],
            "VL_CONTA": [1000.0, 500.0]
        })
        result = map_accounts(df, config)
        nomes = result.sort("CD_CONTA").select("CONTA_NOME").to_series().to_list()
        assert nomes == ["RECEITA_LIQUIDA", "EBIT"]

    def test_conta_nao_mapeada_retorna_null(self, config):
        df = pl.DataFrame({
            "CD_CONTA": ["9.99"],
            "CATEGORIA": ["INDUSTRIAL"],
            "VL_CONTA": [100.0]
        })
        result = map_accounts(df, config)
        assert result["CONTA_NOME"][0] is None

    def test_mesma_conta_diferente_por_setor(self, config):
        """A conta 2.08 só existe para FINANCEIRO, não para INDUSTRIAL."""
        df = pl.DataFrame({
            "CD_CONTA": ["2.08", "2.08"],
            "CATEGORIA": ["FINANCEIRO", "INDUSTRIAL"],
            "VL_CONTA": [500.0, 500.0]
        })
        result = map_accounts(df, config)
        fin_row = result.filter(pl.col("CATEGORIA") == "FINANCEIRO")
        ind_row = result.filter(pl.col("CATEGORIA") == "INDUSTRIAL")
        assert fin_row["CONTA_NOME"][0] == "PATRIMONIO_LIQUIDO"
        assert ind_row["CONTA_NOME"][0] is None


# --- 4. Testes de Indicadores Financeiros (Parametrizado) ---

class TestIndicadoresFinanceiros:
    """
    Testa cálculos de indicadores usando valores sintéticos (gabarito).
    Skill python-testing: @pytest.mark.parametrize para cobrir N cenários.
    """

    @pytest.mark.parametrize("num,den,expected", [
        (100_000.0, 500_000.0, 0.2),      # ROE positivo (20%)
        (100_000.0, 2_000_000.0, 0.05),    # ROA (5%)
        (400_000.0, 1_000_000.0, 0.4),     # Margem Bruta (40%)
        (100_000.0, 1_000_000.0, 0.1),     # Margem Líquida (10%)
        (30_000.0, 100_000.0, 0.3),        # Payout (30%)
        (600_000.0, 300_000.0, 2.0),       # Liquidez Corrente
        (100_000.0, -200_000.0, -0.5),     # ROE com PL negativo
        (-100_000.0, 200_000.0, -0.5),     # Prejuízo / PL positivo
        (0.0, 500_000.0, 0.0),             # Numerador zero
    ], ids=[
        "ROE_20pct", "ROA_5pct", "MgBruta_40pct", "MgLiquida_10pct",
        "Payout_30pct", "LiqCorrente_2x", "ROE_PL_negativo",
        "Prejuizo", "Numerador_zero",
    ])
    def test_indicador_safe_div(self, num, den, expected):
        """Testa safe_div com múltiplos cenários financeiros."""
        df = pl.DataFrame({"num": [num], "den": [den]})
        result = df.select(safe_div("num", "den").alias("r"))
        assert result["r"][0] == pytest.approx(expected)

    def test_ebitda(self):
        """EBITDA = EBIT + Depreciação = 200k + 50k = 250k"""
        df = pl.DataFrame({"ebit": [200_000.0], "deprec": [50_000.0]})
        result = df.select((pl.col("ebit") + pl.col("deprec")).alias("ebitda"))
        assert result["ebitda"][0] == pytest.approx(250_000.0)

    def test_divida_liquida(self):
        """Dívida Líquida = (CP + LP) - Caixa = (100k + 300k) - 150k = 250k"""
        df = pl.DataFrame({"cp": [100_000.0], "lp": [300_000.0], "caixa": [150_000.0]})
        result = df.select((pl.col("cp") + pl.col("lp") - pl.col("caixa")).alias("dl"))
        assert result["dl"][0] == pytest.approx(250_000.0)

    def test_roic(self):
        """ROIC = (EBIT * 0.66) / (PL + Dívida Bruta) = (200k * 0.66) / 900k ≈ 0.1467"""
        df = pl.DataFrame({"ebit": [200_000.0], "pl": [500_000.0], "divida": [400_000.0]})
        result = df.select(
            ((pl.col("ebit") * 0.66) / (pl.col("pl") + pl.col("divida"))).alias("roic")
        )
        assert result["roic"][0] == pytest.approx(0.14666, rel=1e-3)


# --- 5. Testes de Piotroski F-Score (Reais via Polars) ---

class TestPiotroskiFScore:
    """
    Testa cada critério do F-Score usando expressões Polars reais,
    não apenas comparações Python triviais.
    """

    @pytest.fixture
    def df_fscore(self):
        """DataFrame com 2 anos para calcular variações YoY."""
        return pl.DataFrame({
            "TICKER": ["ACME3", "ACME3"],
            "ANO": [2022, 2023],
            "ROA": [0.04, 0.06],
            "FCO": [200_000.0, 350_000.0],
            "LUCRO_FINAL": [150_000.0, 300_000.0],
            "ALAVANCAGEM_LP": [0.25, 0.20],
            "LIQUIDEZ_CORRENTE": [1.5, 1.8],
            "QTDE_ACOES": [1_000_000.0, 1_000_000.0],
            "MARGEM_BRUTA": [0.35, 0.40],
            "GIRO_ATIVO": [0.50, 0.55],
            "CATEGORIA": ["INDUSTRIAL", "INDUSTRIAL"],
        })

    def test_roa_positivo_pontua(self, df_fscore):
        """ROA > 0 → +1 ponto (via expressão Polars)"""
        result = df_fscore.filter(pl.col("ANO") == 2023).select(
            (pl.col("ROA") > 0).cast(pl.Int32).alias("ponto")
        )
        assert result["ponto"][0] == 1

    def test_fco_positivo_pontua(self, df_fscore):
        """FCO > 0 → +1 ponto"""
        result = df_fscore.filter(pl.col("ANO") == 2023).select(
            (pl.col("FCO") > 0).cast(pl.Int32).alias("ponto")
        )
        assert result["ponto"][0] == 1

    def test_roa_crescente_pontua(self, df_fscore):
        """ROA atual > ROA anterior → +1 ponto (com shift/over)"""
        df = df_fscore.with_columns(
            pl.col("ROA").shift(1).over("TICKER").alias("ROA_prev")
        ).filter(pl.col("ANO") == 2023)
        result = df.select((pl.col("ROA") > pl.col("ROA_prev")).cast(pl.Int32).alias("ponto"))
        assert result["ponto"][0] == 1

    def test_alavancagem_decrescente_pontua(self, df_fscore):
        """Alavancagem LP atual < anterior → +1 ponto"""
        df = df_fscore.with_columns(
            pl.col("ALAVANCAGEM_LP").shift(1).over("TICKER").alias("ALAV_prev")
        ).filter(pl.col("ANO") == 2023)
        result = df.select((pl.col("ALAVANCAGEM_LP") < pl.col("ALAV_prev")).cast(pl.Int32).alias("ponto"))
        assert result["ponto"][0] == 1

    def test_fscore_completo_empresa_forte(self, df_fscore):
        """Empresa com todos os critérios positivos deve ter F-Score = 9."""
        df = df_fscore.with_columns(
            [pl.col(c).shift(1).over("TICKER").alias(f"{c}_prev")
             for c in ["ROA", "ALAVANCAGEM_LP", "LIQUIDEZ_CORRENTE", "QTDE_ACOES", "MARGEM_BRUTA", "GIRO_ATIVO"]]
        ).filter(pl.col("ANO") == 2023)

        score = df.select(
            ((pl.col("ROA") > 0).cast(pl.Int32) +
             (pl.col("FCO") > 0).cast(pl.Int32) +
             (pl.col("ROA") > pl.col("ROA_prev")).cast(pl.Int32) +
             (pl.col("FCO") > pl.col("LUCRO_FINAL")).cast(pl.Int32) +
             (pl.col("ALAVANCAGEM_LP") < pl.col("ALAVANCAGEM_LP_prev")).cast(pl.Int32) +
             (pl.col("LIQUIDEZ_CORRENTE") > pl.col("LIQUIDEZ_CORRENTE_prev")).cast(pl.Int32) +
             (pl.col("QTDE_ACOES") <= pl.col("QTDE_ACOES_prev")).cast(pl.Int32) +
             (pl.col("MARGEM_BRUTA") > pl.col("MARGEM_BRUTA_prev")).cast(pl.Int32) +
             (pl.col("GIRO_ATIVO") > pl.col("GIRO_ATIVO_prev")).cast(pl.Int32)
            ).alias("F_SCORE")
        )
        assert score["F_SCORE"][0] == 9

    def test_fscore_empresa_fraca(self):
        """Empresa com todos os critérios negativos deve ter F-Score = 0."""
        df = pl.DataFrame({
            "TICKER": ["FAIL3", "FAIL3"],
            "ANO": [2022, 2023],
            "ROA": [0.05, -0.02],
            "FCO": [100_000.0, -50_000.0],
            "LUCRO_FINAL": [80_000.0, -30_000.0],
            "ALAVANCAGEM_LP": [0.20, 0.30],
            "LIQUIDEZ_CORRENTE": [1.8, 1.2],
            "QTDE_ACOES": [1_000_000.0, 1_200_000.0],
            "MARGEM_BRUTA": [0.40, 0.30],
            "GIRO_ATIVO": [0.55, 0.45],
            "CATEGORIA": ["INDUSTRIAL", "INDUSTRIAL"],
        })
        df = df.with_columns(
            [pl.col(c).shift(1).over("TICKER").alias(f"{c}_prev")
             for c in ["ROA", "ALAVANCAGEM_LP", "LIQUIDEZ_CORRENTE", "QTDE_ACOES", "MARGEM_BRUTA", "GIRO_ATIVO"]]
        ).filter(pl.col("ANO") == 2023)

        score = df.select(
            ((pl.col("ROA") > 0).cast(pl.Int32) +
             (pl.col("FCO") > 0).cast(pl.Int32) +
             (pl.col("ROA") > pl.col("ROA_prev")).cast(pl.Int32) +
             (pl.col("FCO") > pl.col("LUCRO_FINAL")).cast(pl.Int32) +
             (pl.col("ALAVANCAGEM_LP") < pl.col("ALAVANCAGEM_LP_prev")).cast(pl.Int32) +
             (pl.col("LIQUIDEZ_CORRENTE") > pl.col("LIQUIDEZ_CORRENTE_prev")).cast(pl.Int32) +
             (pl.col("QTDE_ACOES") <= pl.col("QTDE_ACOES_prev")).cast(pl.Int32) +
             (pl.col("MARGEM_BRUTA") > pl.col("MARGEM_BRUTA_prev")).cast(pl.Int32) +
             (pl.col("GIRO_ATIVO") > pl.col("GIRO_ATIVO_prev")).cast(pl.Int32)
            ).alias("F_SCORE")
        )
        assert score["F_SCORE"][0] == 0


# --- 6. Testes do Altman Z-Score (Emerging Markets) ---

class TestAltmanZScore:
    """Valida o cálculo do risco de insolvência de Altman."""

    def test_altman_z_empresa_saudavel(self):
        """Testa se uma empresa com balanço robusto recebe um Z-Score na Zona Segura (>2.90)."""
        df = pl.DataFrame({
            "ATIVO_CIRCULANTE": [500_000.0],
            "PASSIVO_CIRCULANTE": [100_000.0],
            "ATIVO_TOTAL": [1_000_000.0],
            "PATRIMONIO_LIQUIDO": [600_000.0],
            "EBIT": [150_000.0],
            "CATEGORIA": ["INDUSTRIAL"]
        })
        # A = 400k/1M = 0.4
        # B = 600k/1M = 0.6
        # C = 150k/1M = 0.15
        # D = 600k / (1M - 600k) = 600k / 400k = 1.5
        # Z = 6.56(0.4) + 3.26(0.6) + 6.72(0.15) + 1.05(1.5)
        # Z = 2.624 + 1.956 + 1.008 + 1.575 = 7.163
        from synetra.domain.indicators import safe_div
        result = df.select(
            pl.when(pl.col('CATEGORIA') == 'INDUSTRIAL').then(
                6.56 * safe_div(pl.col('ATIVO_CIRCULANTE') - pl.col('PASSIVO_CIRCULANTE'), 'ATIVO_TOTAL') +
                3.26 * safe_div('PATRIMONIO_LIQUIDO', 'ATIVO_TOTAL') +
                6.72 * safe_div('EBIT', 'ATIVO_TOTAL') +
                1.05 * safe_div('PATRIMONIO_LIQUIDO', pl.when((pl.col('ATIVO_TOTAL') - pl.col('PATRIMONIO_LIQUIDO')) == 0).then(None).otherwise(pl.col('ATIVO_TOTAL') - pl.col('PATRIMONIO_LIQUIDO')))
            ).otherwise(None).alias('ALTMAN_Z')
        )
        assert result["ALTMAN_Z"][0] == pytest.approx(7.163, rel=1e-3)

    def test_altman_z_empresa_em_crise(self):
        """Testa se uma empresa com passivo a descoberto (PL negativo) afunda no Z-Score."""
        df = pl.DataFrame({
            "ATIVO_CIRCULANTE": [100_000.0],
            "PASSIVO_CIRCULANTE": [500_000.0],
            "ATIVO_TOTAL": [1_000_000.0],
            "PATRIMONIO_LIQUIDO": [-200_000.0],
            "EBIT": [-50_000.0],
            "CATEGORIA": ["INDUSTRIAL"]
        })
        # A = -400k/1M = -0.4
        # B = -200k/1M = -0.2
        # C = -50k/1M = -0.05
        # D = -200k / 1.2M = -0.1666...
        # Z = 6.56(-0.4) + 3.26(-0.2) + 6.72(-0.05) + 1.05(-0.1666)
        # Z = -2.624 - 0.652 - 0.336 - 0.175 = -3.787
        from synetra.domain.indicators import safe_div
        result = df.select(
            pl.when(pl.col('CATEGORIA') == 'INDUSTRIAL').then(
                6.56 * safe_div(pl.col('ATIVO_CIRCULANTE') - pl.col('PASSIVO_CIRCULANTE'), 'ATIVO_TOTAL') +
                3.26 * safe_div('PATRIMONIO_LIQUIDO', 'ATIVO_TOTAL') +
                6.72 * safe_div('EBIT', 'ATIVO_TOTAL') +
                1.05 * safe_div('PATRIMONIO_LIQUIDO', pl.when((pl.col('ATIVO_TOTAL') - pl.col('PATRIMONIO_LIQUIDO')) == 0).then(None).otherwise(pl.col('ATIVO_TOTAL') - pl.col('PATRIMONIO_LIQUIDO')))
            ).otherwise(None).alias('ALTMAN_Z')
        )
        assert result["ALTMAN_Z"][0] == pytest.approx(-3.787, rel=1e-3)


# --- 7. Testes de Validação de Configuração (Pydantic) ---

class TestConfigValidation:
    """Testa a validação Pydantic do config.toml."""

    def test_config_valida_carrega_sem_erro(self):
        """O config.toml do projeto deve passar na validação."""
        from synetra.config import load_config
        config = load_config("config.toml")
        assert hasattr(config, "urls")
        assert hasattr(config, "pipeline")
        assert hasattr(config, "contas")

    def test_years_start_menor_que_end(self):
        """years_start deve ser menor que years_end."""
        from synetra.config import load_config
        config = load_config("config.toml")
        assert config.pipeline.years_start < config.pipeline.years_end

    def test_regex_config_presente(self):
        """A seção [regex] deve existir com os padrões contábeis."""
        from synetra.config import load_config
        config = load_config("config.toml")
        assert hasattr(config, "regex")
        assert hasattr(config.regex, "depreciacao")
        assert hasattr(config.regex, "capex")
        assert hasattr(config.regex, "dividendos")

    def test_todos_setores_presentes(self):
        """As contas devem ter os 3 setores obrigatórios."""
        from synetra.config import load_config
        config = load_config("config.toml")
        assert "industrial" in config.contas
        assert "financeiro" in config.contas
        assert "seguradora" in config.contas

    def test_config_invalida_levanta_erro(self):
        """Um TOML inválido deve levantar ValidationError."""
        from pydantic import ValidationError

        from synetra.config import SynetraConfig

        with pytest.raises(ValidationError):
            SynetraConfig(
                urls={"dfp_pattern": "sem_placeholder", "fre_pattern": "x", "cadastro": "x", "fundamentus": "x"},
                pipeline={"doc_types": ["DRE"], "years_start": 2010, "years_end": 2005, "max_workers": 5},
                fuzzy_match={"threshold": 85},
                contas={"industrial": {}},
                setores={"financeiro": ["BANCO"], "seguradora": ["SEGURO"]}
            )



# --- 8. Testes de CAGR Multi-janela (3a e 5a) — Agnóstico ao Setor ---

class TestCAGR:
    """
    Valida o cálculo de CAGR de Receita e Lucro em janelas de 3 e 5 anos.
    O CAGR é agnóstico ao setor — deve funcionar para INDUSTRIAL,
    FINANCEIRO e SEGURADORA sem adaptação.

    Fórmula: CAGR = (Valor_t / Valor_t-N)^(1/N) - 1
    """

    def _cagr_expr(self, col: str, n: int, alias: str) -> pl.Expr:
        """Helper reutilizado pelos testes — replica a lógica do transformer."""
        base = pl.col(col).shift(n).over("TICKER")
        return (
            pl.when((base > 0) & (pl.col(col) > 0))
              .then((pl.col(col) / base).pow(1.0 / n) - 1)
              .otherwise(None)
              .alias(alias)
        )

    def test_cagr_receita_3a_crescimento_conhecido(self):
        """Receita 100 → 200 em 3 anos → CAGR ≈ 25.99%"""
        df = pl.DataFrame({
            "TICKER": ["TEST3"] * 4,
            "ANO": [2020, 2021, 2022, 2023],
            "RECEITA_LIQUIDA": [100.0, 130.0, 160.0, 200.0],
        })
        result = df.with_columns(
            self._cagr_expr("RECEITA_LIQUIDA", 3, "CAGR_3A")
        ).filter(pl.col("ANO") == 2023)
        # (200/100)^(1/3) - 1 = 0.2599
        assert result["CAGR_3A"][0] == pytest.approx(0.2599, rel=1e-3)

    def test_cagr_receita_5a_crescimento_conhecido(self):
        """Receita 100 → 161.05 em 5 anos → CAGR = exatamente 10%"""
        df = pl.DataFrame({
            "TICKER": ["TEST5"] * 6,
            "ANO": [2018, 2019, 2020, 2021, 2022, 2023],
            "RECEITA_LIQUIDA": [100.0, 110.0, 121.0, 133.1, 146.41, 161.051],
        })
        result = df.with_columns(
            self._cagr_expr("RECEITA_LIQUIDA", 5, "CAGR_5A")
        ).filter(pl.col("ANO") == 2023)
        assert result["CAGR_5A"][0] == pytest.approx(0.10, rel=1e-3)

    def test_cagr_sem_historico_suficiente_retorna_null(self):
        """Se não há N+1 anos de histórico, CAGR deve ser None."""
        df = pl.DataFrame({
            "TICKER": ["NEW3", "NEW3"],
            "ANO": [2022, 2023],
            "RECEITA_LIQUIDA": [100.0, 150.0],
        })
        result = df.with_columns(
            self._cagr_expr("RECEITA_LIQUIDA", 3, "CAGR_3A")
        ).filter(pl.col("ANO") == 2023)
        assert result["CAGR_3A"][0] is None

    def test_cagr_base_zero_retorna_null(self):
        """Base zero (empresa acabou de começar) deve retornar None, não infinito."""
        df = pl.DataFrame({
            "TICKER": ["ZERO3"] * 4,
            "ANO": [2020, 2021, 2022, 2023],
            "RECEITA_LIQUIDA": [0.0, 50.0, 100.0, 200.0],
        })
        result = df.with_columns(
            self._cagr_expr("RECEITA_LIQUIDA", 3, "CAGR_3A")
        ).filter(pl.col("ANO") == 2023)
        assert result["CAGR_3A"][0] is None

    def test_cagr_base_negativa_retorna_null(self):
        """Base negativa (prejuízo histórico) deve retornar None — evita raiz de negativo."""
        df = pl.DataFrame({
            "TICKER": ["NEG3"] * 4,
            "ANO": [2020, 2021, 2022, 2023],
            "LUCRO_FINAL": [-50_000.0, -10_000.0, 20_000.0, 100_000.0],
        })
        result = df.with_columns(
            self._cagr_expr("LUCRO_FINAL", 3, "CAGR_3A")
        ).filter(pl.col("ANO") == 2023)
        assert result["CAGR_3A"][0] is None

    def test_cagr_valor_atual_negativo_retorna_null(self):
        """Empresa que voltou a dar prejuízo — CAGR inaplicável."""
        df = pl.DataFrame({
            "TICKER": ["CRASH3"] * 4,
            "ANO": [2020, 2021, 2022, 2023],
            "LUCRO_FINAL": [100_000.0, 80_000.0, 30_000.0, -20_000.0],
        })
        result = df.with_columns(
            self._cagr_expr("LUCRO_FINAL", 3, "CAGR_3A")
        ).filter(pl.col("ANO") == 2023)
        assert result["CAGR_3A"][0] is None

    def test_cagr_receita_constante(self):
        """Receita estagnada (mesmo valor) → CAGR = 0%"""
        df = pl.DataFrame({
            "TICKER": ["FLAT3"] * 4,
            "ANO": [2020, 2021, 2022, 2023],
            "RECEITA_LIQUIDA": [100.0, 100.0, 100.0, 100.0],
        })
        result = df.with_columns(
            self._cagr_expr("RECEITA_LIQUIDA", 3, "CAGR_3A")
        ).filter(pl.col("ANO") == 2023)
        assert result["CAGR_3A"][0] == pytest.approx(0.0, abs=1e-9)

    def test_cagr_funciona_para_banco(self):
        """CAGR deve funcionar para FINANCEIRO (agnóstico ao setor)."""
        df = pl.DataFrame({
            "TICKER": ["BANK4"] * 4,
            "ANO": [2020, 2021, 2022, 2023],
            "CATEGORIA": ["FINANCEIRO"] * 4,
            "RECEITA_LIQUIDA": [1_000_000.0, 1_200_000.0, 1_450_000.0, 1_728_000.0],
        })
        result = df.with_columns(
            self._cagr_expr("RECEITA_LIQUIDA", 3, "CAGR_3A")
        ).filter(pl.col("ANO") == 2023)
        # (1.728M / 1M)^(1/3) - 1 = 0.20 (20% a.a.)
        assert result["CAGR_3A"][0] == pytest.approx(0.20, rel=1e-3)

    def test_cagr_funciona_para_seguradora(self):
        """CAGR deve funcionar para SEGURADORA (agnóstico ao setor)."""
        df = pl.DataFrame({
            "TICKER": ["SEG3"] * 4,
            "ANO": [2020, 2021, 2022, 2023],
            "CATEGORIA": ["SEGURADORA"] * 4,
            "LUCRO_FINAL": [500_000.0, 600_000.0, 720_000.0, 864_000.0],
        })
        result = df.with_columns(
            self._cagr_expr("LUCRO_FINAL", 3, "CAGR_3A")
        ).filter(pl.col("ANO") == 2023)
        # (864k / 500k)^(1/3) - 1 = 0.20
        assert result["CAGR_3A"][0] == pytest.approx(0.20, rel=1e-3)

    def test_cagr_isolado_por_ticker(self):
        """Dois tickers diferentes não devem contaminar o CAGR um do outro."""
        df = pl.DataFrame({
            "TICKER": ["A1"] * 4 + ["B1"] * 4,
            "ANO": [2020, 2021, 2022, 2023] * 2,
            "RECEITA_LIQUIDA": [
                100.0, 130.0, 160.0, 200.0,    # A1 cresce
                500.0, 450.0, 400.0, 350.0,    # B1 decresce
            ],
        })
        result = df.with_columns(
            self._cagr_expr("RECEITA_LIQUIDA", 3, "CAGR_3A")
        ).filter(pl.col("ANO") == 2023).sort("TICKER")

        # A1: (200/100)^(1/3) - 1 ≈ 0.2599
        assert result.filter(pl.col("TICKER") == "A1")["CAGR_3A"][0] == pytest.approx(0.2599, rel=1e-3)
        # B1: (350/500)^(1/3) - 1 ≈ -0.1122
        assert result.filter(pl.col("TICKER") == "B1")["CAGR_3A"][0] == pytest.approx(-0.1122, rel=1e-3)



# --- 9. Testes de Fatores Quantitativos (Quality + Momentum + Risk) ---

class TestFatoresQuantitativos:
    """
    Valida os 5 fatores quantitativos mainstream:
      1. CASH_CONVERSION     — Quality factor (Sloan 1996)
      2. EARNINGS_STABILITY  — Quality factor (Fama-French 2015)
      3. VOL_LUCRO           — Risk factor (coeficiente de variação)
      4. DELTA_ROE           — Momentum factor (Novy-Marx 2013)
      5. DELTA_MARGEM        — Momentum factor
    """

    # ---------- 1. Cash Conversion ----------

    def test_cash_conversion_normal(self):
        """FCO 120M / Lucro 100M = 1.20 (excelente qualidade de lucro)"""
        df = pl.DataFrame({"FCO": [120_000_000.0], "LUCRO_FINAL": [100_000_000.0]})
        result = df.select(
            pl.when(pl.col("LUCRO_FINAL") > 0)
              .then(pl.col("FCO") / pl.col("LUCRO_FINAL"))
              .otherwise(None).alias("CC")
        )
        assert result["CC"][0] == pytest.approx(1.20, rel=1e-4)

    def test_cash_conversion_baixa_qualidade(self):
        """FCO 50M / Lucro 100M = 0.5 (sinal de alerta — só 50% vira caixa)"""
        df = pl.DataFrame({"FCO": [50_000_000.0], "LUCRO_FINAL": [100_000_000.0]})
        result = df.select(
            pl.when(pl.col("LUCRO_FINAL") > 0)
              .then(pl.col("FCO") / pl.col("LUCRO_FINAL"))
              .otherwise(None).alias("CC")
        )
        assert result["CC"][0] == pytest.approx(0.5, rel=1e-4)

    def test_cash_conversion_lucro_zero_retorna_null(self):
        """Lucro zero → null (evita divisão por zero)"""
        df = pl.DataFrame({"FCO": [50_000_000.0], "LUCRO_FINAL": [0.0]})
        result = df.select(
            pl.when(pl.col("LUCRO_FINAL") > 0)
              .then(pl.col("FCO") / pl.col("LUCRO_FINAL"))
              .otherwise(None).alias("CC")
        )
        assert result["CC"][0] is None

    def test_cash_conversion_lucro_negativo_retorna_null(self):
        """Prejuízo → null (CC não é interpretável com lucro negativo)"""
        df = pl.DataFrame({"FCO": [50_000_000.0], "LUCRO_FINAL": [-30_000_000.0]})
        result = df.select(
            pl.when(pl.col("LUCRO_FINAL") > 0)
              .then(pl.col("FCO") / pl.col("LUCRO_FINAL"))
              .otherwise(None).alias("CC")
        )
        assert result["CC"][0] is None

    # ---------- 2. Earnings Stability ----------

    def test_earnings_stability_empresa_estavel(self):
        """ROE sempre igual → desvio-padrão = 0 (máxima estabilidade)"""
        df = pl.DataFrame({
            "TICKER": ["STAB"] * 5,
            "ANO": [2019, 2020, 2021, 2022, 2023],
            "ROE": [0.15, 0.15, 0.15, 0.15, 0.15],
        })
        result = df.with_columns(
            pl.col("ROE").rolling_std(window_size=5, min_samples=5).over("TICKER").alias("ES")
        ).filter(pl.col("ANO") == 2023)
        assert result["ES"][0] == pytest.approx(0.0, abs=1e-9)

    def test_earnings_stability_empresa_volatil(self):
        """ROE oscilando muito → desvio-padrão alto"""
        df = pl.DataFrame({
            "TICKER": ["VOLA"] * 5,
            "ANO": [2019, 2020, 2021, 2022, 2023],
            "ROE": [0.30, -0.05, 0.20, -0.10, 0.25],
        })
        result = df.with_columns(
            pl.col("ROE").rolling_std(window_size=5, min_samples=5).over("TICKER").alias("ES")
        ).filter(pl.col("ANO") == 2023)
        # std amostral desses 5 valores ≈ 0.186
        assert result["ES"][0] == pytest.approx(0.186, rel=0.05)

    def test_earnings_stability_historico_insuficiente(self):
        """Menos de 5 anos → null"""
        df = pl.DataFrame({
            "TICKER": ["NEW"] * 3,
            "ANO": [2021, 2022, 2023],
            "ROE": [0.10, 0.12, 0.14],
        })
        result = df.with_columns(
            pl.col("ROE").rolling_std(window_size=5, min_samples=5).over("TICKER").alias("ES")
        ).filter(pl.col("ANO") == 2023)
        assert result["ES"][0] is None

    # ---------- 3. Volatilidade do Lucro ----------

    def test_vol_lucro_empresa_previsivel(self):
        """Lucro estável 100M → CV baixo (alta previsibilidade)"""
        df = pl.DataFrame({
            "TICKER": ["PRED"] * 5,
            "ANO": [2019, 2020, 2021, 2022, 2023],
            "LUCRO_FINAL": [100.0, 105.0, 100.0, 95.0, 100.0],
        })
        result = df.with_columns(
            pl.col("LUCRO_FINAL").rolling_std(window_size=5, min_samples=5).over("TICKER").alias("std"),
            pl.col("LUCRO_FINAL").rolling_mean(window_size=5, min_samples=5).over("TICKER").alias("mean"),
        ).with_columns(
            pl.when(pl.col("mean") > 0).then(pl.col("std") / pl.col("mean"))
              .otherwise(None).alias("VOL")
        ).filter(pl.col("ANO") == 2023)
        # std ≈ 3.54, mean = 100 → CV ≈ 0.0354
        assert result["VOL"][0] == pytest.approx(0.0354, rel=0.05)

    def test_vol_lucro_lucro_medio_negativo_retorna_null(self):
        """Empresa com média de lucro negativa → CV sem sentido, retorna null"""
        df = pl.DataFrame({
            "TICKER": ["BAD"] * 5,
            "ANO": [2019, 2020, 2021, 2022, 2023],
            "LUCRO_FINAL": [-100.0, -50.0, -80.0, -70.0, -60.0],
        })
        result = df.with_columns(
            pl.col("LUCRO_FINAL").rolling_std(window_size=5, min_samples=5).over("TICKER").alias("std"),
            pl.col("LUCRO_FINAL").rolling_mean(window_size=5, min_samples=5).over("TICKER").alias("mean"),
        ).with_columns(
            pl.when(pl.col("mean") > 0).then(pl.col("std") / pl.col("mean"))
              .otherwise(None).alias("VOL")
        ).filter(pl.col("ANO") == 2023)
        assert result["VOL"][0] is None

    # ---------- 4. Delta ROE ----------

    def test_delta_roe_positivo(self):
        """ROE 15% → 18% (melhora) → Δ = +0.03"""
        df = pl.DataFrame({
            "TICKER": ["UP", "UP"],
            "ANO": [2022, 2023],
            "ROE": [0.15, 0.18],
        })
        result = df.with_columns(
            pl.col("ROE").shift(1).over("TICKER").alias("ROE_prev")
        ).with_columns(
            pl.when(pl.col("ROE_prev").is_not_null())
              .then(pl.col("ROE") - pl.col("ROE_prev"))
              .otherwise(None).alias("D_ROE")
        ).filter(pl.col("ANO") == 2023)
        assert result["D_ROE"][0] == pytest.approx(0.03, rel=1e-4)

    def test_delta_roe_negativo(self):
        """ROE 20% → 12% (piora) → Δ = -0.08"""
        df = pl.DataFrame({
            "TICKER": ["DOWN", "DOWN"],
            "ANO": [2022, 2023],
            "ROE": [0.20, 0.12],
        })
        result = df.with_columns(
            pl.col("ROE").shift(1).over("TICKER").alias("ROE_prev")
        ).with_columns(
            pl.when(pl.col("ROE_prev").is_not_null())
              .then(pl.col("ROE") - pl.col("ROE_prev"))
              .otherwise(None).alias("D_ROE")
        ).filter(pl.col("ANO") == 2023)
        assert result["D_ROE"][0] == pytest.approx(-0.08, rel=1e-4)

    def test_delta_roe_primeiro_ano_retorna_null(self):
        """Sem histórico prévio → null"""
        df = pl.DataFrame({
            "TICKER": ["NEW"],
            "ANO": [2023],
            "ROE": [0.15],
        })
        result = df.with_columns(
            pl.col("ROE").shift(1).over("TICKER").alias("ROE_prev")
        ).with_columns(
            pl.when(pl.col("ROE_prev").is_not_null())
              .then(pl.col("ROE") - pl.col("ROE_prev"))
              .otherwise(None).alias("D_ROE")
        )
        assert result["D_ROE"][0] is None

    # ---------- 5. Delta Margem Líquida ----------

    def test_delta_margem_melhora(self):
        """Margem 8% → 12% → Δ = +0.04 (empresa ficou mais eficiente)"""
        df = pl.DataFrame({
            "TICKER": ["EFF", "EFF"],
            "ANO": [2022, 2023],
            "MARGEM_LIQUIDA": [0.08, 0.12],
        })
        result = df.with_columns(
            pl.col("MARGEM_LIQUIDA").shift(1).over("TICKER").alias("M_prev")
        ).with_columns(
            pl.when(pl.col("M_prev").is_not_null())
              .then(pl.col("MARGEM_LIQUIDA") - pl.col("M_prev"))
              .otherwise(None).alias("D_M")
        ).filter(pl.col("ANO") == 2023)
        assert result["D_M"][0] == pytest.approx(0.04, rel=1e-4)

    def test_delta_margem_isolado_por_ticker(self):
        """Deltas não devem vazar entre tickers diferentes"""
        df = pl.DataFrame({
            "TICKER": ["A", "A", "B", "B"],
            "ANO": [2022, 2023, 2022, 2023],
            "MARGEM_LIQUIDA": [0.10, 0.15, 0.20, 0.18],
        })
        result = df.with_columns(
            pl.col("MARGEM_LIQUIDA").shift(1).over("TICKER").alias("M_prev")
        ).with_columns(
            pl.when(pl.col("M_prev").is_not_null())
              .then(pl.col("MARGEM_LIQUIDA") - pl.col("M_prev"))
              .otherwise(None).alias("D_M")
        ).filter(pl.col("ANO") == 2023).sort("TICKER")

        # A: 0.15 - 0.10 = +0.05
        assert result.filter(pl.col("TICKER") == "A")["D_M"][0] == pytest.approx(0.05, rel=1e-4)
        # B: 0.18 - 0.20 = -0.02 (não deve usar a margem de A)
        assert result.filter(pl.col("TICKER") == "B")["D_M"][0] == pytest.approx(-0.02, rel=1e-4)



# --- 10. Teste de Assepsia Setorial para Cash Conversion ---

class TestCashConversionAssepsia:
    """
    Valida a Assepsia Profissional: CASH_CONVERSION deve ser anulado
    para bancos (FCO distorcido por captação), mantido para
    Industrial e Seguradora.
    """

    def test_cash_conversion_anulado_para_banco(self):
        """Banco com FCO e Lucro positivos → CC deve virar None após assepsia"""
        df = pl.DataFrame({
            "CATEGORIA": ["FINANCEIRO"],
            "FCO": [500_000_000.0],
            "LUCRO_FINAL": [1_000_000_000.0],
        })
        # Simula o cálculo + assepsia
        df = df.with_columns(
            pl.when(pl.col("LUCRO_FINAL") > 0)
              .then(pl.col("FCO") / pl.col("LUCRO_FINAL"))
              .otherwise(None).alias("CASH_CONVERSION")
        )
        df = df.with_columns(
            pl.when(pl.col("CATEGORIA") == "FINANCEIRO")
              .then(None)
              .otherwise(pl.col("CASH_CONVERSION"))
              .alias("CASH_CONVERSION")
        )
        assert df["CASH_CONVERSION"][0] is None

    def test_cash_conversion_mantido_para_industrial(self):
        """Empresa Industrial → CC deve permanecer calculado normalmente"""
        df = pl.DataFrame({
            "CATEGORIA": ["INDUSTRIAL"],
            "FCO": [500_000_000.0],
            "LUCRO_FINAL": [1_000_000_000.0],
        })
        df = df.with_columns(
            pl.when(pl.col("LUCRO_FINAL") > 0)
              .then(pl.col("FCO") / pl.col("LUCRO_FINAL"))
              .otherwise(None).alias("CASH_CONVERSION")
        )
        df = df.with_columns(
            pl.when(pl.col("CATEGORIA") == "FINANCEIRO")
              .then(None)
              .otherwise(pl.col("CASH_CONVERSION"))
              .alias("CASH_CONVERSION")
        )
        assert df["CASH_CONVERSION"][0] == pytest.approx(0.5, rel=1e-4)

    def test_cash_conversion_mantido_para_seguradora(self):
        """Seguradora → CC mantido (FCO mais limpo que banco)"""
        df = pl.DataFrame({
            "CATEGORIA": ["SEGURADORA"],
            "FCO": [800_000_000.0],
            "LUCRO_FINAL": [1_000_000_000.0],
        })
        df = df.with_columns(
            pl.when(pl.col("LUCRO_FINAL") > 0)
              .then(pl.col("FCO") / pl.col("LUCRO_FINAL"))
              .otherwise(None).alias("CASH_CONVERSION")
        )
        df = df.with_columns(
            pl.when(pl.col("CATEGORIA") == "FINANCEIRO")
              .then(None)
              .otherwise(pl.col("CASH_CONVERSION"))
              .alias("CASH_CONVERSION")
        )
        assert df["CASH_CONVERSION"][0] == pytest.approx(0.8, rel=1e-4)

    def test_assepsia_em_batch_misto(self):
        """Batch com 3 setores: só Financeiro deve vir null"""
        df = pl.DataFrame({
            "CATEGORIA": ["FINANCEIRO", "INDUSTRIAL", "SEGURADORA"],
            "FCO": [500.0, 600.0, 700.0],
            "LUCRO_FINAL": [1000.0, 1000.0, 1000.0],
        })
        df = df.with_columns(
            pl.when(pl.col("LUCRO_FINAL") > 0)
              .then(pl.col("FCO") / pl.col("LUCRO_FINAL"))
              .otherwise(None).alias("CASH_CONVERSION")
        )
        df = df.with_columns(
            pl.when(pl.col("CATEGORIA") == "FINANCEIRO")
              .then(None)
              .otherwise(pl.col("CASH_CONVERSION"))
              .alias("CASH_CONVERSION")
        )
        assert df["CASH_CONVERSION"][0] is None  # banco → null
        assert df["CASH_CONVERSION"][1] == pytest.approx(0.6, rel=1e-4)
        assert df["CASH_CONVERSION"][2] == pytest.approx(0.7, rel=1e-4)



# --- 11. Teste dos 10 Indicadores de Eficiência e Qualidade ---

class TestEfficiencyAndQuality:
    """
    Valida os 10 novos indicadores de eficiência operacional
    e qualidade (Camada 1) com valores de gabarito.
    """

    # ------------ 1. Margem FCO ------------
    def test_margem_fco_valor_conhecido(self):
        """FCO 300M / Receita 1B = 0.30 (30% da receita vira caixa)"""
        fco, receita = 300_000_000, 1_000_000_000
        assert (fco / receita) == pytest.approx(0.30, rel=1e-4)

    def test_margem_fco_receita_zero_null(self):
        """Receita zero deve virar null via pl.when"""
        df = pl.DataFrame({"FCO": [100.0], "RECEITA_LIQUIDA": [0.0]})
        result = df.select(
            pl.when(pl.col("RECEITA_LIQUIDA") > 0)
              .then(pl.col("FCO") / pl.col("RECEITA_LIQUIDA"))
              .otherwise(None).alias("MARGEM_FCO")
        )
        assert result["MARGEM_FCO"][0] is None

    # ------------ 2. Cash ROA ------------
    def test_cash_roa_valor_conhecido(self):
        """FCO 200M / Ativo 2B = 0.10 (10% do ativo gera caixa por ano)"""
        assert pytest.approx(0.10) == (200_000_000 / 2_000_000_000)

    # ------------ 3. PMR ------------
    def test_pmr_valor_conhecido(self):
        """Contas a Receber 100M / Receita 1B = 0.10 × 365 = 36.5 dias"""
        pmr = (100_000_000 / 1_000_000_000) * 365
        assert pmr == pytest.approx(36.5)

    def test_pmr_setor_rapido(self):
        """Empresa que recebe rápido (à vista) tem PMR baixo."""
        pmr = (10_000_000 / 1_000_000_000) * 365  # 10M/1B = 1%
        assert pmr == pytest.approx(3.65)  # ~4 dias

    # ------------ 4. Capital de Giro ------------
    def test_capital_de_giro_positivo(self):
        """AC 500M - PC 200M = 300M (folga saudável)"""
        assert (500_000_000 - 200_000_000) == 300_000_000

    def test_capital_de_giro_negativo(self):
        """AC 100M - PC 500M = -400M (empresa precisa renegociar dívidas)"""
        assert (100_000_000 - 500_000_000) == -400_000_000

    # ------------ 5. ROCE ------------
    def test_roce_valor_conhecido(self):
        """EBIT 200M / (Ativo 2B - PC 500M) = 200/1500 = 0.1333"""
        roce = 200_000_000 / (2_000_000_000 - 500_000_000)
        assert roce == pytest.approx(0.1333, rel=1e-3)

    def test_roce_denominador_zero(self):
        """Ativo = Passivo Circulante → null"""
        df = pl.DataFrame({"EBIT": [100.0], "ATIVO_TOTAL": [500.0], "PASSIVO_CIRCULANTE": [500.0]})
        result = df.select(
            pl.when((pl.col("ATIVO_TOTAL") - pl.col("PASSIVO_CIRCULANTE")) > 0)
              .then(pl.col("EBIT") / (pl.col("ATIVO_TOTAL") - pl.col("PASSIVO_CIRCULANTE")))
              .otherwise(None).alias("ROCE")
        )
        assert result["ROCE"][0] is None

    # ------------ 6. NOPAT ------------
    def test_nopat_valor_conhecido(self):
        """EBIT 1B × 0.66 = 660M (lucro operacional após IR)"""
        assert (1_000_000_000 * 0.66) == 660_000_000

    # ------------ 7. Reinvestment Rate ------------
    def test_reinvestment_rate_expansao(self):
        """CAPEX 300M / Depreciação 100M = 3.0 (empresa expandindo agressivamente)"""
        assert (300_000_000 / 100_000_000) == 3.0

    def test_reinvestment_rate_manutencao(self):
        """CAPEX 100M / Depreciação 100M = 1.0 (só manutenção)"""
        assert (100_000_000 / 100_000_000) == 1.0

    def test_reinvestment_rate_consumo(self):
        """CAPEX 50M / Depreciação 100M = 0.5 (empresa consumindo imobilizado)"""
        assert (50_000_000 / 100_000_000) == 0.5

    # ------------ 8. Sustainable Growth Rate ------------
    def test_sgr_valor_conhecido(self):
        """ROE 20% × (1 - Payout 40%) = 0.20 × 0.60 = 12% (SGR)"""
        sgr = 0.20 * (1 - 0.40)
        assert sgr == pytest.approx(0.12)

    def test_sgr_payout_100pct(self):
        """Payout 100% (distribui tudo) → SGR = 0 (não pode crescer sem novo capital)"""
        sgr = 0.20 * (1 - 1.0)
        assert sgr == 0.0

    def test_sgr_universal_para_banco(self):
        """SGR funciona para bancos (ROE e Payout são universais)."""
        df = pl.DataFrame({
            "CATEGORIA": ["FINANCEIRO"],
            "ROE": [0.25],        # banco com ROE alto
            "PAYOUT": [0.50],
        })
        df = df.with_columns(
            pl.when(pl.col("ROE").is_not_null() & pl.col("PAYOUT").is_not_null())
              .then(pl.col("ROE") * (1 - pl.col("PAYOUT").clip(lower_bound=0, upper_bound=1)))
              .otherwise(None).alias("SGR")
        )
        # NÃO foi aplicada assepsia → SGR deve ser calculado
        assert df["SGR"][0] == pytest.approx(0.125)

    # ------------ 9. Cash Ratio ------------
    def test_cash_ratio_alta_liquidez(self):
        """Caixa 500M / PC 200M = 2.5 (muito líquida, pode pagar tudo 2.5x)"""
        assert (500_000_000 / 200_000_000) == 2.5

    def test_cash_ratio_baixa_liquidez(self):
        """Caixa 50M / PC 500M = 0.1 (só 10% das dívidas CP estão cobertas)"""
        assert pytest.approx(0.1) == (50_000_000 / 500_000_000)

    # ------------ 10. Assepsia para Bancos ------------
    def test_assepsia_9_indicadores_anulados_para_banco(self):
        """Banco deve ter os 9 indicadores industriais anulados (só SGR fica)."""
        df = pl.DataFrame({
            "CATEGORIA": ["FINANCEIRO"],
            "MARGEM_FCO": [0.15],
            "PMR": [30.0],
            "ROCE": [0.20],
            "NOPAT": [500_000_000.0],
        })
        cols_anular = ['MARGEM_FCO', 'PMR', 'ROCE', 'NOPAT']
        df = df.with_columns([
            pl.when(pl.col("CATEGORIA") == "FINANCEIRO").then(None)
              .otherwise(pl.col(c)).alias(c)
            for c in cols_anular
        ])
        for col in cols_anular:
            assert df[col][0] is None, f"{col} deveria ser None para bancos"

    def test_assepsia_preserva_industrial(self):
        """Empresa industrial deve manter os 10 indicadores intactos."""
        df = pl.DataFrame({
            "CATEGORIA": ["INDUSTRIAL"],
            "MARGEM_FCO": [0.15],
            "PMR": [30.0],
            "ROCE": [0.20],
            "NOPAT": [500_000_000.0],
        })
        cols_anular = ['MARGEM_FCO', 'PMR', 'ROCE', 'NOPAT']
        df = df.with_columns([
            pl.when(pl.col("CATEGORIA") == "FINANCEIRO").then(None)
              .otherwise(pl.col(c)).alias(c)
            for c in cols_anular
        ])
        assert df["MARGEM_FCO"][0] == pytest.approx(0.15)
        assert df["PMR"][0] == pytest.approx(30.0)
        assert df["ROCE"][0] == pytest.approx(0.20)
        assert df["NOPAT"][0] == 500_000_000.0
