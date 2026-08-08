from __future__ import annotations

from alembic import op

from voice_pipeline.storage.orm import chapter_run_segments, chapter_runs

revision = "0002_batch3_chapter_runs"
down_revision = "0001_batch2_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    chapter_runs.create(bind=bind, checkfirst=True)
    chapter_run_segments.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    chapter_run_segments.drop(bind=bind, checkfirst=True)
    chapter_runs.drop(bind=bind, checkfirst=True)
