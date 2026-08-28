from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_director_role_dubbing"
down_revision = "0006_director_preprocessing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("director_roles"):
        return
    columns = {column["name"] for column in inspector.get_columns("director_roles")}
    if "dubbing_enabled" not in columns:
        op.add_column(
            "director_roles",
            sa.Column(
                "dubbing_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("director_roles"):
        return
    columns = {column["name"] for column in inspector.get_columns("director_roles")}
    if "dubbing_enabled" in columns:
        with op.batch_alter_table("director_roles") as batch:
            batch.drop_column("dubbing_enabled")
