from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_chapter_history_soft_delete"
down_revision = "0002_batch3_chapter_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("chapter_runs")}
    if "deleted_at_utc" not in columns:
        op.add_column("chapter_runs", sa.Column("deleted_at_utc", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("chapter_runs")}
    if "deleted_at_utc" in columns:
        op.drop_column("chapter_runs", "deleted_at_utc")
