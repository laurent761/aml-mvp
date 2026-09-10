"""add normalized model invocation attribution links

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02 00:00:00.000000
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _link_id(invocation_id: str, episode_id: str, step_id: str | None) -> str:
    key = f"{invocation_id}\0{episode_id}\0{step_id or ''}"
    return f"modelcalllink_{hashlib.sha256(key.encode()).hexdigest()}"


def upgrade() -> None:
    op.create_table(
        "model_invocation_links",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("model_invocation_id", sa.String(length=80), nullable=False),
        sa.Column("episode_id", sa.String(length=80), nullable=False),
        sa.Column("step_id", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["model_invocation_id"],
            ["model_invocations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["episode_steps.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "model_invocation_id",
            "step_id",
            name="uq_model_invocation_step_link",
        ),
    )
    op.create_index(
        "ix_model_invocation_links_model_invocation_id",
        "model_invocation_links",
        ["model_invocation_id"],
        unique=False,
    )
    op.create_index(
        "ix_model_invocation_links_episode_id",
        "model_invocation_links",
        ["episode_id"],
        unique=False,
    )
    op.create_index(
        "ix_model_invocation_links_step_id",
        "model_invocation_links",
        ["step_id"],
        unique=False,
    )
    op.create_index(
        "uq_model_invocation_episode_only_link",
        "model_invocation_links",
        ["model_invocation_id", "episode_id"],
        unique=True,
        sqlite_where=sa.text("step_id IS NULL"),
        postgresql_where=sa.text("step_id IS NULL"),
    )

    # Preserve attribution created before this normalized relationship existed.
    invocations = sa.table(
        "model_invocations",
        sa.column("id", sa.String(length=80)),
        sa.column("episode_id", sa.String(length=80)),
        sa.column("step_id", sa.String(length=80)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    steps = sa.table(
        "episode_steps",
        sa.column("id", sa.String(length=80)),
        sa.column("episode_id", sa.String(length=80)),
    )
    links = sa.table(
        "model_invocation_links",
        sa.column("id", sa.String(length=80)),
        sa.column("model_invocation_id", sa.String(length=80)),
        sa.column("episode_id", sa.String(length=80)),
        sa.column("step_id", sa.String(length=80)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.select(
                invocations.c.id,
                invocations.c.episode_id,
                invocations.c.step_id,
                invocations.c.created_at,
            )
        ).mappings()
    )
    for row in rows:
        episode_id = row["episode_id"]
        step_id = row["step_id"]
        if episode_id is None and step_id is not None:
            episode_id = connection.scalar(
                sa.select(steps.c.episode_id).where(steps.c.id == step_id)
            )
        if episode_id is None:
            continue
        connection.execute(
            links.insert().values(
                id=_link_id(row["id"], episode_id, step_id),
                model_invocation_id=row["id"],
                episode_id=episode_id,
                step_id=step_id,
                created_at=row["created_at"],
            )
        )


def downgrade() -> None:
    op.drop_index(
        "uq_model_invocation_episode_only_link",
        table_name="model_invocation_links",
    )
    op.drop_index(
        "ix_model_invocation_links_step_id",
        table_name="model_invocation_links",
    )
    op.drop_index(
        "ix_model_invocation_links_episode_id",
        table_name="model_invocation_links",
    )
    op.drop_index(
        "ix_model_invocation_links_model_invocation_id",
        table_name="model_invocation_links",
    )
    op.drop_table("model_invocation_links")
