"""Alembic environment. Runs against the connection supplied by ``SqlMetadataStore``
(``config.attributes['connection']``) or, from the CLI, against ``OMF_DATABASE_URL``."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from omf.adapters.metadata.tables import metadata

config = context.config
connection = config.attributes.get("connection")

if connection is not None:
    context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
else:  # pragma: no cover - CLI usage
    url = os.environ.get("OMF_DATABASE_URL", "sqlite:///./var/omf.db")
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
