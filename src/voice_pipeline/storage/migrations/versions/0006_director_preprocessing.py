from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from voice_pipeline.storage.orm import director_preprocess_paragraphs

revision = "0006_director_preprocessing"
down_revision = "0005_director_working_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("director_projects")
    }
    if "preprocessing_mode" not in columns:
        op.add_column(
            "director_projects",
            sa.Column(
                "preprocessing_mode",
                sa.String(length=16),
                nullable=False,
                server_default="structural",
            ),
        )
    if "structural_text" not in columns:
        op.add_column(
            "director_projects",
            sa.Column("structural_text", sa.Text(), nullable=True),
        )
    if "preprocessed_text" not in columns:
        op.add_column(
            "director_projects",
            sa.Column("preprocessed_text", sa.Text(), nullable=True),
        )
    if "preprocess_revision" not in columns:
        op.add_column(
            "director_projects",
            sa.Column(
                "preprocess_revision",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    director_preprocess_paragraphs.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    director_preprocess_paragraphs.drop(bind, checkfirst=True)
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("director_projects")
    }
    with op.batch_alter_table("director_projects") as batch:
        for name in (
            "preprocess_revision",
            "preprocessed_text",
            "structural_text",
            "preprocessing_mode",
        ):
            if name in columns:
                batch.drop_column(name)
