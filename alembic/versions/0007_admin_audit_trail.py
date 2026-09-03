"""admin_id на решениях админа (кто подтвердил пополнение/заказ/сброс
пароля) — раньше был виден только факт, не актор.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "topup_requests",
        sa.Column("decided_by_admin_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
    )
    op.add_column(
        "subscription_orders",
        sa.Column("decided_by_admin_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
    )
    op.add_column(
        "password_reset_requests",
        sa.Column("completed_by_admin_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("password_reset_requests", "completed_by_admin_id")
    op.drop_column("subscription_orders", "decided_by_admin_id")
    op.drop_column("topup_requests", "decided_by_admin_id")
