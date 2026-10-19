"""Base class for domain and API models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    # Fields with defaults are always present in serialized output; say so in the
    # JSON schema so generated clients (TypeScript SDK) type them as non-optional.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
