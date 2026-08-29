from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_director_performance_controls"
down_revision = "0008_director_reference_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("director_projects"):
        return
    columns = {str(column["name"]) for column in inspector.get_columns("director_projects")}
    if "performance_direction" not in columns:
        op.add_column(
            "director_projects",
            sa.Column("performance_direction", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("director_projects"):
        return
    columns = {str(column["name"]) for column in inspector.get_columns("director_projects")}
    if "performance_direction" in columns:
        with op.batch_alter_table("director_projects") as batch:
            batch.drop_column("performance_direction")
