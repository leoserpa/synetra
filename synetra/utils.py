"""Utilitários de limpeza de texto vetorizada para o Synetra."""
from __future__ import annotations

import polars as pl


def clean_text_expr(col_name: str) -> pl.Expr:
    """Normaliza texto para ASCII uppercase usando expressões Polars.

    Remove acentos, converte para uppercase e faz strip. Toda a operação
    usa expressões nativas, sem callbacks Python.

    Args:
        col_name: Nome da coluna de texto a normalizar.

    Returns:
        Expressão Polars para uso em ``.with_columns()`` ou ``.select()``.
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


def setup_logging() -> None:
    """Configura o logger padrão do Python para o Synetra.

    Formato: `YYYY-MM-DD HH:MM:SS [LEVEL] name: message`
    """
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def display_df_preview(df: pl.DataFrame, n_rows: int = 10) -> None:
    """Exibe um preview formatado do DataFrame no terminal usando Polars.

    Args:
        df: DataFrame a exibir
        n_rows: Número de linhas a mostrar (padrão 10)
    """
    import sys

    from loguru import logger
    try:
        # sys.stdout.reconfigure existe em TextIOWrapper mas o type stub usa
        # o tipo base TextIO (que não tem esse método). Usamos type: ignore.
        sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
        with pl.Config(tbl_cols=-1, tbl_rows=n_rows, fmt_float="full"):
            print(df.head(n_rows))
    except Exception:
        logger.info("Exibição em tabela indisponível no terminal. Veja o arquivo CSV gerado.")
