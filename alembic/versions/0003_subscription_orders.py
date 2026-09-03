"""products + subscription orders (платёжный агент)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_rub", sa.Numeric(10, 2), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "subscription_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("account_email", sa.Text(), nullable=False),
        sa.Column("price_rub", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="paid"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('paid','fulfilled','refunded')", name="ck_subscription_orders_status"),
    )
    op.create_index("ix_subscription_orders_customer_id", "subscription_orders", ["customer_id"])

    op.add_column(
        "wallet_ledger",
        sa.Column("subscription_order_id", sa.Integer(), sa.ForeignKey("subscription_orders.id"), nullable=True),
    )
    op.drop_constraint("ck_wallet_ledger_entry_type", "wallet_ledger", type_="check")
    op.create_check_constraint(
        "ck_wallet_ledger_entry_type",
        "wallet_ledger",
        "entry_type IN ('topup','usage','subscription','refund','adjustment')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_wallet_ledger_entry_type", "wallet_ledger", type_="check")
    op.create_check_constraint(
        "ck_wallet_ledger_entry_type",
        "wallet_ledger",
        "entry_type IN ('topup','usage','refund','adjustment')",
    )
    op.drop_column("wallet_ledger", "subscription_order_id")
    op.drop_table("subscription_orders")
    op.drop_table("products")
