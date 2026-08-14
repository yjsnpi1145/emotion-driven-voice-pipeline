from __future__ import annotations

from alembic import op

from voice_pipeline.storage.orm import (
    director_analysis_chunks,
    director_edit_events,
    director_generation_items,
    director_generations,
    director_projects,
    director_roles,
    director_utterances,
    role_presets,
)

revision = "0004_director_mode"
down_revision = "0003_chapter_history_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in (
        director_projects,
        director_analysis_chunks,
        role_presets,
        director_roles,
        director_utterances,
        director_edit_events,
        director_generations,
        director_generation_items,
    ):
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        director_generation_items,
        director_generations,
        director_edit_events,
        director_utterances,
        director_roles,
        role_presets,
        director_analysis_chunks,
        director_projects,
    ):
        table.drop(bind, checkfirst=True)
