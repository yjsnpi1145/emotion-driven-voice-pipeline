from __future__ import annotations

from alembic import op
from sqlalchemy import text

from voice_pipeline.storage.orm import metadata

revision = "0001_batch2_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata.create_all(bind=bind)
    bind.execute(
        text("INSERT INTO storage_meta (singleton_id, protected_graph_revision) VALUES (1, 0)")
    )


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
