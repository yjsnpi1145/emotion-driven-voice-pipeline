from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

dubbing_tasks = Table(
    "dubbing_tasks",
    metadata,
    Column("task_id", String(36), primary_key=True),
    Column("title", Text, nullable=False),
    Column("source_text", Text, nullable=False),
    Column("source_text_sha256", String(64), nullable=False),
    Column("target_language", String(8), nullable=False),
    Column("output_spec_json", Text, nullable=False),
    Column("revision", Integer, nullable=False, server_default="0"),
    Column("created_at_utc", Text, nullable=False),
    Column("updated_at_utc", Text, nullable=False),
    CheckConstraint("revision >= 0", name="ck_dubbing_tasks_revision_nonnegative"),
)

segments = Table(
    "segments",
    metadata,
    Column("segment_id", String(36), primary_key=True),
    Column(
        "task_id",
        String(36),
        ForeignKey("dubbing_tasks.task_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("source_start", Integer, nullable=False),
    Column("source_end", Integer, nullable=False),
    Column("source_text", Text, nullable=False),
    Column("synthesis_text", Text, nullable=False),
    Column("target_language", String(8), nullable=False),
    Column("llm_emotion_vector_json", Text, nullable=False),
    Column("current_emotion_vector_json", Text, nullable=False),
    Column("ref_text_cn", Text, nullable=False),
    Column("speed_factor", Float, nullable=False),
    Column("pause_after_ms", Integer, nullable=False),
    Column("seed", Integer, nullable=False),
    Column("ref_draft_revision", Integer, nullable=False, server_default="0"),
    Column("gsv_draft_revision", Integer, nullable=False, server_default="0"),
    Column("selection_revision", Integer, nullable=False, server_default="0"),
    Column("active_ref_version_id", String(36), nullable=True),
    Column("active_gsv_version_id", String(36), nullable=True),
    Column("revision", Integer, nullable=False, server_default="0"),
    Column("created_at_utc", Text, nullable=False),
    Column("updated_at_utc", Text, nullable=False),
    UniqueConstraint("task_id", "ordinal", name="uq_segments_task_ordinal"),
    CheckConstraint("source_start >= 0", name="ck_segments_source_start_nonnegative"),
    CheckConstraint("source_end > source_start", name="ck_segments_source_range"),
)

model_profiles = Table(
    "model_profiles",
    metadata,
    Column("profile_id", String(36), primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("source_kind", String(16), nullable=False),
    Column("declared_family", Text, nullable=True),
    Column("relative_directory", Text, nullable=False, unique=True),
    Column("gpt_relative_path", Text, nullable=False),
    Column("sovits_relative_path", Text, nullable=False),
    Column("gpt_sha256", String(64), nullable=False),
    Column("sovits_sha256", String(64), nullable=False),
    Column("gpt_size_bytes", Integer, nullable=False),
    Column("sovits_size_bytes", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at_utc", Text, nullable=False),
    Column("archived_at_utc", Text, nullable=True),
    CheckConstraint("gpt_size_bytes > 0", name="ck_model_profiles_gpt_size_positive"),
    CheckConstraint("sovits_size_bytes > 0", name="ck_model_profiles_sovits_size_positive"),
)

project_settings = Table(
    "project_settings",
    metadata,
    Column("key", String(128), primary_key=True),
    Column("value", Text, nullable=False),
)

generation_jobs = Table(
    "generation_jobs",
    metadata,
    Column("job_id", String(36), primary_key=True),
    Column("request_id", String(36), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("stage", String(128), nullable=False),
    Column(
        "task_id",
        String(36),
        ForeignKey("dubbing_tasks.task_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column(
        "segment_id",
        String(36),
        ForeignKey("segments.segment_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column(
        "retry_of_job_id",
        String(36),
        ForeignKey("generation_jobs.job_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("attempt", Integer, nullable=False),
    Column("request_snapshot_json", Text, nullable=False),
    Column("request_snapshot_sha256", String(64), nullable=False),
    Column("model_fingerprint_json", Text, nullable=False),
    Column("model_profile_snapshot_json", Text, nullable=True),
    Column("output_spec_json", Text, nullable=True),
    Column("segment_snapshot_json", Text, nullable=True),
    Column("cancel_requested_at_utc", Text, nullable=True),
    Column("runner_instance_id", String(36), nullable=True),
    Column("result_json", Text, nullable=True),
    Column("error_json", Text, nullable=True),
    Column("activation_outcome", String(32), nullable=False, server_default="'not_applicable'"),
    Column("created_at_utc", Text, nullable=False),
    Column("started_at_utc", Text, nullable=True),
    Column("finished_at_utc", Text, nullable=True),
    CheckConstraint("attempt >= 1", name="ck_generation_jobs_attempt_positive"),
)

artifact_blobs = Table(
    "artifact_blobs",
    metadata,
    Column("content_sha256", String(64), primary_key=True),
    Column("relative_path", Text, nullable=False, unique=True),
    Column("byte_size", Integer, nullable=False),
    Column("frames", Integer, nullable=False),
    Column("sample_rate", Integer, nullable=False),
    Column("channels", Integer, nullable=False),
    Column("duration_seconds", Float, nullable=False),
    Column("rms_dbfs", Float, nullable=False),
    Column("peak_dbfs", Float, nullable=False),
    Column("lifecycle_state", String(16), nullable=False),
    Column("created_at_utc", Text, nullable=False),
    Column("checked_at_utc", Text, nullable=False),
)

artifact_versions = Table(
    "artifact_versions",
    metadata,
    Column("version_id", String(36), primary_key=True),
    Column(
        "segment_id",
        String(36),
        ForeignKey("segments.segment_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("artifact_type", String(16), nullable=False),
    Column("display_ordinal", Integer, nullable=True),
    Column(
        "source_job_id",
        String(36),
        ForeignKey("generation_jobs.job_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "blob_sha256",
        String(64),
        ForeignKey("artifact_blobs.content_sha256", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("manifest_relative_path", Text, nullable=False, unique=True),
    Column("ref_version_id", String(36), nullable=True),
    Column("ref_content_sha256", String(64), nullable=True),
    Column("input_snapshot_json", Text, nullable=False),
    Column("input_snapshot_sha256", String(64), nullable=False),
    Column("model_fingerprint_json", Text, nullable=False),
    Column("model_fingerprint_sha256", String(64), nullable=False),
    Column("model_profile_snapshot_json", Text, nullable=True),
    Column("quality_profile_version", String(64), nullable=False),
    Column("quality_result_json", Text, nullable=False),
    Column("complete_cache_key", String(64), nullable=True),
    Column("created_at_utc", Text, nullable=False),
    UniqueConstraint(
        "segment_id", "artifact_type", "display_ordinal", name="uq_artifact_versions_ordinal"
    ),
)

artifact_version_state = Table(
    "artifact_version_state",
    metadata,
    Column(
        "version_id",
        String(36),
        ForeignKey("artifact_versions.version_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("state", String(16), nullable=False),
    Column("diagnostic_json", Text, nullable=False),
    Column("checked_at_utc", Text, nullable=False),
)

job_artifacts = Table(
    "job_artifacts",
    metadata,
    Column(
        "job_id",
        String(36),
        ForeignKey("generation_jobs.job_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "version_id",
        String(36),
        ForeignKey("artifact_versions.version_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("role", String(16), primary_key=True),
    Column("stage_index", Integer, nullable=False),
)

cache_entries = Table(
    "cache_entries",
    metadata,
    Column("cache_key", String(64), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("canonical_payload_json", Text, nullable=False),
    Column(
        "blob_sha256",
        String(64),
        ForeignKey("artifact_blobs.content_sha256", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "source_version_id",
        String(36),
        ForeignKey("artifact_versions.version_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("state", String(16), nullable=False),
    Column("created_at_utc", Text, nullable=False),
    Column("last_hit_at_utc", Text, nullable=False),
    Column("hit_count", Integer, nullable=False),
)

quality_cache_entries = Table(
    "quality_cache_entries",
    metadata,
    Column("cache_key", String(64), primary_key=True),
    Column("audio_sha256", String(64), nullable=False),
    Column("expected_text_sha256", String(64), nullable=False),
    Column("policy_fingerprint_sha256", String(64), nullable=False),
    Column("report_json", Text, nullable=False),
    Column("state", String(16), nullable=False),
    Column("created_at_utc", Text, nullable=False),
    Column("last_hit_at_utc", Text, nullable=False),
)

retention_plans = Table(
    "retention_plans",
    metadata,
    Column("plan_id", String(36), primary_key=True),
    Column("storage_revision", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("scope_json", Text, nullable=False),
    Column("summary_json", Text, nullable=False),
    Column("created_at_utc", Text, nullable=False),
    Column("applied_at_utc", Text, nullable=True),
)

retention_candidates = Table(
    "retention_candidates",
    metadata,
    Column(
        "plan_id",
        String(36),
        ForeignKey("retention_plans.plan_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "version_id",
        String(36),
        ForeignKey("artifact_versions.version_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("artifact_type", String(16), nullable=False),
    Column("reason", Text, nullable=False),
    Column("action", String(16), nullable=False),
    Column("protection_reason", Text, nullable=True),
    Column("ordinal", Integer, nullable=False),
)

instance_recovery_runs = Table(
    "instance_recovery_runs",
    metadata,
    Column("recovery_run_id", String(36), primary_key=True),
    Column("instance_id", String(36), nullable=False),
    Column("started_at_utc", Text, nullable=False),
    Column("finished_at_utc", Text, nullable=True),
    Column("summary_json", Text, nullable=True),
)

storage_meta = Table(
    "storage_meta",
    metadata,
    Column("singleton_id", Integer, primary_key=True),
    Column("protected_graph_revision", Integer, nullable=False),
    CheckConstraint("singleton_id = 1", name="ck_storage_meta_singleton"),
)
