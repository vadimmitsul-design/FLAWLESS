"""family accounts + prompt library + DLP + dialogue archive

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- семейный тариф ---
    op.add_column("customers", sa.Column("is_child", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("customers", sa.Column("parent_customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True))

    # --- библиотека промптов (создаём таблицу до FK на неё из usage_events) ---
    op.create_table(
        "prompts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("author_customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("price_rub", sa.Numeric(10, 4), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_prompts_author_customer_id", "prompts", ["author_customer_id"])

    # --- архиватор диалогов ---
    op.create_table(
        "dialogue_archives",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_dialogue_archives_customer_id", "dialogue_archives", ["customer_id"])
    op.create_index("ix_dialogue_archives_content_hash", "dialogue_archives", ["content_hash"])

    # --- usage_events: биллинг-плательщик (родитель для детского аккаунта),
    # ссылка на промпт, отметка DLP-редактирования ---
    op.add_column("usage_events", sa.Column("billing_customer_id", sa.Integer(), nullable=True))
    op.execute("UPDATE usage_events SET billing_customer_id = customer_id WHERE billing_customer_id IS NULL")
    op.alter_column("usage_events", "billing_customer_id", nullable=False)
    op.create_foreign_key(
        "fk_usage_events_billing_customer_id", "usage_events", "customers", ["billing_customer_id"], ["id"]
    )
    op.create_index("ix_usage_events_billing_customer_id", "usage_events", ["billing_customer_id"])

    op.add_column("usage_events", sa.Column("prompt_id", sa.Integer(), sa.ForeignKey("prompts.id"), nullable=True))
    op.add_column("usage_events", sa.Column("dlp_redactions", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_events", "dlp_redactions")
    op.drop_column("usage_events", "prompt_id")
    op.drop_index("ix_usage_events_billing_customer_id", table_name="usage_events")
    op.drop_constraint("fk_usage_events_billing_customer_id", "usage_events", type_="foreignkey")
    op.drop_column("usage_events", "billing_customer_id")

    op.drop_table("dialogue_archives")
    op.drop_table("prompts")

    op.drop_column("customers", "parent_customer_id")
    op.drop_column("customers", "is_child")
