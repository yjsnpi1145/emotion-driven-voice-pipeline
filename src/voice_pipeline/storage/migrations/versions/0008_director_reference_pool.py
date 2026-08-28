from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from voice_pipeline.storage.orm import director_reference_pool_entries

revision = "0008_director_reference_pool"
down_revision = "0007_director_role_dubbing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    director_reference_pool_entries.create(bind, checkfirst=True)
    inspector = sa.inspect(bind)
    if not inspector.has_table("director_generation_items"):
        return
    columns = {
        str(column["name"])
        for column in inspector.get_columns("director_generation_items")
    }
    additions = (
        sa.Column(
            "reference_mode",
            sa.String(16),
            nullable=False,
            server_default="independent",
        ),
        sa.Column("reference_pool_entry_id", sa.String(36), nullable=True),
        sa.Column("reference_emotion_bucket", sa.String(16), nullable=True),
        sa.Column("reference_degraded_from", sa.String(16), nullable=True),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("director_generation_items", column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("director_generation_items"):
        columns = {
            str(column["name"])
            for column in inspector.get_columns("director_generation_items")
        }
        for name in (
            "reference_degraded_from",
            "reference_emotion_bucket",
            "reference_pool_entry_id",
            "reference_mode",
        ):
            if name in columns:
                with op.batch_alter_table("director_generation_items") as batch:
                    batch.drop_column(name)
    director_reference_pool_entries.drop(bind, checkfirst=True)
