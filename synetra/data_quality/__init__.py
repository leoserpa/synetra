"""Módulo de Data Quality — auditoria contínua de cobertura de dados.

Gera um relatório CSV e um resumo no log classificando cada ticker por:
    - Cobertura de preço Yahoo (histórico + snapshot atual)
    - Atualidade dos dados CVM
    - Flags específicas (delisted, ticker errado, stale, etc.)
    - Severidade (OK, LOW, MEDIUM, HIGH)

O relatório é **não-destrutivo**: só lê os dados gerados pelo pipeline e
produz um arquivo de auditoria (``data_quality_report.csv``).
"""
from synetra.data_quality.models import (
    FlagType,
    QualityIssue,
    QualityReport,
    Severity,
    TickerQuality,
)
from synetra.data_quality.report import (
    build_quality_report,
    export_quality_report,
    log_quality_summary,
    run_data_quality_audit,
)

__all__ = [
    "FlagType",
    "QualityIssue",
    "QualityReport",
    "Severity",
    "TickerQuality",
    "build_quality_report",
    "export_quality_report",
    "log_quality_summary",
    "run_data_quality_audit",
]
