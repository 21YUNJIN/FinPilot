"""S3 理财量化服务（StockQuant bank edition, read-only).

Public surface:
    stockquant_bank.report.build_risk_report
    stockquant_bank.rank.rank_products
    stockquant_bank.stress.stress_product
    stockquant_bank.service.create_app
"""

from .canonical import METHOD_VERSION, SCHEMA_VERSION

__version__ = "1.0.0"
__all__ = ["METHOD_VERSION", "SCHEMA_VERSION", "__version__"]