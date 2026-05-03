"""Utilitários de limpeza de texto e mapeamento de Tickers."""
import io
import re
import logging
import polars as pl
import pandas as pd
import cloudscraper
from unidecode import unidecode
from rapidfuzz import process, fuzz

logger = logging.getLogger("synetra.utils")


# ============================================================
# Expressões Polars Nativas (sem map_elements)
# ============================================================

def clean_text_expr(col_name: str) -> pl.Expr:
    """
    Retorna uma expressão Polars que normaliza texto para ASCII uppercase.
    Substitui caracteres acentuados nativamente, sem usar map_elements.
    """
    return (
        pl.col(col_name)
        .fill_null("")
        .str.to_uppercase()
        .str.strip_chars()
        .str.replace_all(r"[ÁÀÂÃÄ]", "A")
        .str.replace_all(r"[ÉÈÊË]", "E")
        .str.replace_all(r"[ÍÌÎÏ]", "I")
        .str.replace_all(r"[ÓÒÔÕÖ]", "O")
        .str.replace_all(r"[ÚÙÛÜ]", "U")
        .str.replace_all(r"Ç", "C")
        .str.replace_all(r"Ñ", "N")
    )


# ============================================================
# Funções Python (usadas apenas em datasets pequenos)
# ============================================================

def normalize_name(name: str) -> str:
    """Normaliza nome de empresa para comparação fuzzy."""
    if not name or (isinstance(name, float)):
        return ""
    n = unidecode(str(name)).upper()
    n = re.sub(r'[^A-Z0-9 ]', ' ', n)
    n = n.replace(' BCO ', ' BANCO ').replace('BCO ', 'BANCO ')
    for s in ['S A ', ' SA ', ' LTDA', ' HOLDING', ' CIA ', ' PARTICIPACOES']:
        n = n.replace(s, ' ')
    return " ".join(n.split())


# ============================================================
# Scraper do Fundamentus (Mapeamento de Tickers)
# ============================================================

class FundamentusScraper:
    """Obtém a lista de Tickers e Razões Sociais do site Fundamentus."""
    URL = "https://www.fundamentus.com.br/detalhes.php"

    def __init__(self):
        self.scraper = cloudscraper.create_scraper()

    def get_tickers(self) -> pd.DataFrame:
        """Retorna DataFrame Pandas com colunas [Papel, Nome Comercial, Razão Social]."""
        logger.info("Obtendo mapeamento de Tickers no Fundamentus...")
        r = self.scraper.get(self.URL, timeout=30)
        tables = pd.read_html(io.StringIO(r.text))
        for table in tables:
            if 'Papel' in table.columns and 'Razão Social' in table.columns:
                return table[['Papel', 'Nome Comercial', 'Razão Social']]
        raise ValueError("Tabela Fundamentus nao encontrada no HTML retornado.")


# ============================================================
# Fuzzy Match (Ticker-CNPJ)
# ============================================================

def establish_matches(df_cvm_latest: pl.DataFrame, df_fund: pd.DataFrame, threshold: int = 85) -> pl.DataFrame:
    """
    Estabelece o match Ticker-CNPJ usando Fuzzy Matching.
    Aceita um DataFrame Polars da CVM e um Pandas do Fundamentus.
    Retorna um DataFrame Polars com colunas [CNPJ_CIA, TICKER, RAZAO_CVM].

    NOTA DE PERFORMANCE: Este módulo mantém Pandas por duas razões técnicas:
    1. pd.read_html() (no FundamentusScraper) não possui equivalente no Polars.
    2. rapidfuzz opera sobre listas Python nativas, exigindo iteração.
    O dataset processado aqui é pequeno (~500 empresas), sem impacto mensurável.
    """
    logger.info("Estabelecendo Match fixo Ticker-CNPJ...")
    noms = pd.concat([
        df_fund[['Nome Comercial', 'Papel']].rename(columns={'Nome Comercial': 'NOME'}),
        df_fund[['Razão Social', 'Papel']].rename(columns={'Razão Social': 'NOME'})
    ])
    noms['NOME_NORM'] = noms['NOME'].apply(normalize_name)
    noms = noms[noms['NOME_NORM'] != ""]
    fund_list = noms['NOME_NORM'].unique().tolist()

    # Converter Polars -> lista de dicts para iteração (dataset pequeno)
    cvm_comps = df_cvm_latest.select(['CNPJ_CIA', 'DENOM_CIA']).unique().to_pandas()

    matches = []
    for _, row in cvm_comps.iterrows():
        cvm_norm = normalize_name(row['DENOM_CIA'])
        match = process.extractOne(cvm_norm, fund_list, scorer=fuzz.token_sort_ratio)
        if match and match[1] >= threshold:
            best_name = match[0]
            tickers = noms[noms['NOME_NORM'] == best_name]['Papel'].unique()
            for t in tickers:
                matches.append({'CNPJ_CIA': row['CNPJ_CIA'], 'TICKER': t, 'RAZAO_CVM': row['DENOM_CIA']})

    logger.info("Fuzzy Match concluído: %d matches encontrados.", len(matches))
    return pl.DataFrame(matches)
