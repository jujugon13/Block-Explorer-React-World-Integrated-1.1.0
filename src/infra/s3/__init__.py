"""AWS S3 deployment adapter."""

from .adapter import (
    S3CompatibilityError,
    S3Config,
    S3ConfigurationError,
    S3DependencyError,
    S3Storage,
    build_s3_storage,
    verify_s3,
)

__all__ = [
    "S3CompatibilityError",
    "S3Config",
    "S3ConfigurationError",
    "S3DependencyError",
    "S3Storage",
    "build_s3_storage",
    "verify_s3",
]
