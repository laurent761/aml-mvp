"""Immutable scenario versions and registered target bundles.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    document = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "scenario_versions",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("scenario_id", sa.String(100), nullable=False),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("family", sa.String(100), nullable=False),
        sa.Column("split", sa.String(20), nullable=False),
        sa.Column("public_document", document, nullable=False),
        sa.Column("private_document", document, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scenario_id", "version", name="uq_scenario_version"),
    )
    for name in ("scenario_id", "family", "split"):
        op.create_index(f"ix_scenario_versions_{name}", "scenario_versions", [name])
    op.create_table(
        "target_bundles",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column(
            "scenario_version_id",
            sa.String(80),
            sa.ForeignKey("scenario_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "target_version_id", sa.String(80), sa.ForeignKey("target_versions.id"), nullable=False
        ),
        sa.Column(
            "attack_task_id", sa.String(80), sa.ForeignKey("attack_tasks.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("target_bundles")
    op.drop_table("scenario_versions")
