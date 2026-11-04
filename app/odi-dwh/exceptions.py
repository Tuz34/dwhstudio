"""Typed domain errors used at DWH Studio service boundaries."""


class DWHBaseError(Exception):
    """Base class for application-level errors."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.args[0]!r})"


class DatabaseConnectionError(DWHBaseError):
    """Raised when an Oracle or source connection cannot be established."""


class DatabaseQueryError(DWHBaseError):
    """Raised when a database query or write cannot be completed."""


class InvalidOwnerError(DWHBaseError):
    """Raised when an owner or schema identifier is not allowed."""


class TableNotFoundError(DWHBaseError):
    """Raised when the requested table does not exist."""


class WatermarkNotFoundError(DWHBaseError):
    """Raised when a required ETL watermark is absent."""


class RAGSearchError(DWHBaseError):
    """Raised when the local RAG search boundary fails."""


class SchemaValidationError(DWHBaseError):
    """Raised when schema validation or DDL planning fails."""


class RollbackError(DWHBaseError):
    """Raised when a reviewed rollback operation cannot be completed."""


class SKEYError(DWHBaseError):
    """Raised when a surrogate-key operation cannot be completed."""


class MappingGenerationError(DWHBaseError):
    """Raised when mapping planning or generation fails safely."""


class ReconciliationError(DWHBaseError):
    """Raised when reconciliation analysis or controlled repair fails."""
