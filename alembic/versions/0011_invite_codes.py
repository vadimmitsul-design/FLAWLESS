"""Коды приглашений — закрытая регистрация для внутреннего контура

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_admin_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("used_by_customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_invite_codes_code", "invite_codes", ["code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_invite_codes_code", table_name="invite_codes")
    op.drop_table("invite_codes")
