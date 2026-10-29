"""Text extraction from uploaded files.

Kept free of the database, embeddings and settings so it can run in a small, isolated parser
process (``worker.py``). Import ``isolation.parse`` to run it the way the service does.
"""

from production_rag.parsing.extract import (
    SUPPORTED_TYPES,
    ExtractedDocument,
    ExtractionError,
    Section,
    UnsupportedDocumentError,
    content_type_for,
    discover_documents,
    extract,
    safe_filename,
    validate_upload,
)

__all__ = [
    "SUPPORTED_TYPES",
    "ExtractedDocument",
    "ExtractionError",
    "Section",
    "UnsupportedDocumentError",
    "content_type_for",
    "discover_documents",
    "extract",
    "safe_filename",
    "validate_upload",
]
