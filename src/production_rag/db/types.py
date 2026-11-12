"""Column types that differ between SQLite and PostgreSQL.

An embedding is a float32 array. In Python it is always its raw bytes, which is what SQLite
stores. On PostgreSQL the column is a pgvector ``vector``, so the database can compare
embeddings itself; the type converts the bytes to pgvector's text form and back.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from sqlalchemy import LargeBinary
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine, UserDefinedType


def vector_literal(values: Any) -> str:
    """pgvector's text form of a vector: ``[0.1,0.2,...]``."""
    return "[" + ",".join(repr(float(v)) for v in np.asarray(values, dtype=np.float32)) + "]"


class PgVector(UserDefinedType[str]):
    """pgvector's ``vector`` type, without a fixed dimension: models differ in theirs."""

    cache_ok = True

    def get_col_spec(self, **_: Any) -> str:
        return "vector"


class Embedding(TypeDecorator[bytes]):
    """float32 bytes in Python; a BLOB on SQLite, a ``vector`` on PostgreSQL."""

    impl = LargeBinary
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PgVector())
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value: bytes | None, dialect: Dialect) -> Any:
        if value is None or dialect.name != "postgresql":
            return value
        return vector_literal(np.frombuffer(value, dtype=np.float32))

    def process_result_value(self, value: Any, dialect: Dialect) -> bytes | None:
        if value is None or dialect.name != "postgresql":
            return value  # type: ignore[no-any-return]
        if isinstance(value, bytes | memoryview):
            return bytes(value)
        numbers = np.array(str(value).strip("[]").split(","), dtype=np.float32)
        return numbers.tobytes()
