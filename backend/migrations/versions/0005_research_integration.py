"""Research ownership, durable commands and immutable lineage.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("artifacts", "size_bytes", existing_type=sa.Integer(), type_=sa.BigInteger())

    op.create_table('research_owners',
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('reserved_cost', sa.Float(), nullable=False),
    sa.Column('spent_cost', sa.Float(), nullable=False),
    sa.Column('reserved_bytes', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('research_records',
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.Column('owner_id', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('request_key', sa.String(length=200), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('document', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_id', 'kind', 'request_key', name='uq_research_request')
    )
    op.create_index(op.f('ix_research_records_kind'), 'research_records', ['kind'], unique=False)
    op.create_index(op.f('ix_research_records_owner_id'), 'research_records', ['owner_id'], unique=False)
    op.create_table('research_sessions',
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.Column('owner_id', sa.String(length=100), nullable=False),
    sa.Column('request_key', sa.String(length=200), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('campaign_id', sa.String(length=80), nullable=False),
    sa.Column('run_id', sa.String(length=100), nullable=True),
    sa.Column('document', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('state', sa.String(length=30), nullable=False),
    sa.Column('episode_id', sa.String(length=80), nullable=True),
    sa.Column('step_index', sa.Integer(), nullable=False),
    sa.Column('fence', sa.Integer(), nullable=False),
    sa.Column('worker_id', sa.String(length=100), nullable=True),
    sa.Column('job_id', sa.String(length=80), nullable=False),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('stop_reason', sa.String(length=100), nullable=True),
    sa.Column('capsule_handle', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('reservation_released', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ),
    sa.ForeignKeyConstraint(['episode_id'], ['episodes.id'], ),
    sa.ForeignKeyConstraint(['job_id'], ['work_leases.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['research_records.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('campaign_id'),
    sa.UniqueConstraint('owner_id', 'request_key', name='uq_session_request')
    )
    op.create_index(op.f('ix_research_sessions_owner_id'), 'research_sessions', ['owner_id'], unique=False)
    op.create_index(op.f('ix_research_sessions_state'), 'research_sessions', ['state'], unique=False)
    op.create_table('artifact_uploads',
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.Column('owner_id', sa.String(length=100), nullable=False),
    sa.Column('request_key', sa.String(length=200), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('document', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('artifact_id', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_id', 'request_key', name='uq_upload_request')
    )
    op.create_index(op.f('ix_artifact_uploads_owner_id'), 'artifact_uploads', ['owner_id'], unique=False)
    op.create_table('episode_commands',
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.Column('session_id', sa.String(length=100), nullable=False),
    sa.Column('request_key', sa.String(length=200), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('fence', sa.Integer(), nullable=False),
    sa.Column('result', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['research_sessions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id', 'request_key', name='uq_command_request')
    )
    op.create_index(op.f('ix_episode_commands_session_id'), 'episode_commands', ['session_id'], unique=False)



def downgrade() -> None:

    op.drop_index(op.f('ix_episode_commands_session_id'), table_name='episode_commands')
    op.drop_table('episode_commands')
    op.drop_index(op.f('ix_artifact_uploads_owner_id'), table_name='artifact_uploads')
    op.drop_table('artifact_uploads')
    op.drop_index(op.f('ix_research_sessions_state'), table_name='research_sessions')
    op.drop_index(op.f('ix_research_sessions_owner_id'), table_name='research_sessions')
    op.drop_table('research_sessions')
    op.drop_index(op.f('ix_research_records_owner_id'), table_name='research_records')
    op.drop_index(op.f('ix_research_records_kind'), table_name='research_records')
    op.drop_table('research_records')
    op.drop_table('research_owners')

