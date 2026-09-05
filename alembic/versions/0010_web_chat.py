"""Веб-чат в кабинете (2.3 доработок) — диалоги и сообщения

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "web_conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("model_alias", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_web_conversations_customer_id", "web_conversations", ["customer_id"])
    op.create_index("ix_web_conversations_created_at", "web_conversations", ["created_at"])

    op.create_table(
        "web_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("web_conversations.id"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("attachment_name", sa.Text(), nullable=True),
        sa.Column("usage_event_id", sa.Uuid(), sa.ForeignKey("usage_events.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_web_messages_conversation_id", "web_messages", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_web_messages_conversation_id", table_name="web_messages")
    op.drop_table("web_messages")
    op.drop_index("ix_web_conversations_created_at", table_name="web_conversations")
    op.drop_index("ix_web_conversations_customer_id", table_name="web_conversations")
    op.drop_table("web_conversations")
