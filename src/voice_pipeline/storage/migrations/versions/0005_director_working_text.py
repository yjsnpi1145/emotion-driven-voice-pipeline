from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_director_working_text"
down_revision = "0004_director_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]: column
        for column in sa.inspect(bind).get_columns("director_utterances")
    }
    if "working_text" not in columns:
        op.add_column(
            "director_utterances",
            sa.Column("working_text", sa.Text(), nullable=True),
        )
    bind.execute(
        sa.text(
            "UPDATE director_utterances "
            "SET working_text = source_text WHERE working_text IS NULL"
        )
    )
    columns = {
        column["name"]: column
        for column in sa.inspect(bind).get_columns("director_utterances")
    }
    if bool(columns["working_text"]["nullable"]):
        with op.batch_alter_table("director_utterances") as batch:
            batch.alter_column(
                "working_text",
                existing_type=sa.Text(),
                nullable=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("director_utterances")}
    if "working_text" in columns:
        with op.batch_alter_table("director_utterances") as batch:
            batch.drop_column("working_text")
