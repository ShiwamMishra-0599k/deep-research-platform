"""Composite indexes for the hot queries and a CHECK on messages.role.

- conversations by (user_id, updated_at): the sidebar list, newest first
- messages by (conversation_id, created_at): transcript load and history assembly

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_conversations_user_updated", "conversations", ["user_id", "updated_at"])
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])
    # Fails (and rolls back, changing nothing) if any existing row has an unexpected role.
    op.create_check_constraint("ck_messages_role", "messages", "role IN ('USER', 'ASSISTANT')")


def downgrade() -> None:
    op.drop_constraint("ck_messages_role", "messages", type_="check")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_index("ix_conversations_user_updated", table_name="conversations")
