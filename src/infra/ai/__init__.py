"""Stage 13 AI adapter composition."""

from .adapters import (
    AIAdapters,
    AIConfigurationError,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_VERSION,
    EMBEDDING_PROVIDER,
    LLM_MODEL,
    RERANKER_MODEL,
    LocalCrossEncoderReranker,
    OpenAIEmbeddingTransport,
    OpenAILanguageModel,
    OpenAIServiceError,
    OpenAITokenTruncator,
    build_ai_adapters,
    validate_ai_configuration,
)

__all__ = [
    "AIAdapters",
    "AIConfigurationError",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_MODEL",
    "EMBEDDING_MODEL_VERSION",
    "EMBEDDING_PROVIDER",
    "LLM_MODEL",
    "RERANKER_MODEL",
    "LocalCrossEncoderReranker",
    "OpenAIEmbeddingTransport",
    "OpenAILanguageModel",
    "OpenAIServiceError",
    "OpenAITokenTruncator",
    "build_ai_adapters",
    "validate_ai_configuration",
]
