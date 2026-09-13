"""loamdb: a relational database engine built from raw bytes up.

Page-based file storage, a B+tree, a write-ahead log for crash recovery,
transactions, and a SQL layer on top. See the README for the full
architecture and what's deliberately simplified.
"""
from .database import Database
from .sql.executor import execute, ExecutionError

__all__ = ["Database", "execute", "ExecutionError"]
__version__ = "0.1.0"
