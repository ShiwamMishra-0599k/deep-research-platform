"""Server-side sessions for cookie authentication (rotating refresh tokens, reuse detection, revocation).

One row per session; only SHA-256 hashes of refresh tokens are stored. Downgrade drops just this table
(no user data depends on it).

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("refresh_hash", sa.LargeBinary(), nullable=False),
        sa.Column("prev_refresh_hash", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=32), nullable=True),
        sa.Column("user_agent", sa.String(length=200), nullable=True),
        sa.Column("ip_hash", sa.LargeBinary(), nullable=True),
        sa.UniqueConstraint("refresh_hash", name="uq_auth_sessions_refresh_hash"),
    )
    op.create_index("ix_auth_sessions_prev_refresh_hash", "auth_sessions", ["prev_refresh_hash"])
    op.create_index(
        "ix_auth_sessions_user_live", "auth_sessions", ["user_id"], postgresql_where=sa.text("revoked_at IS NULL")
    )


def downgrade() -> None:
    op.drop_index("ix_auth_sessions_user_live", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_prev_refresh_hash", table_name="auth_sessions")
    op.drop_table("auth_sessions")
