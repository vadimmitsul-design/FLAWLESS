"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="customer"),
        sa.Column("balance_rub", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('customer','admin')", name="ck_customers_role"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_api_keys_customer_id", "api_keys", ["customer_id"])

    op.create_table(
        "model_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("quality", sa.Text(), nullable=True),
        sa.Column("size", sa.Text(), nullable=True),
        sa.Column("price_per_image", sa.Numeric(12, 6), nullable=True),
        sa.Column("price_per_1m_input_tokens", sa.Numeric(12, 6), nullable=True),
        sa.Column("price_per_1m_input_image_tokens", sa.Numeric(12, 6), nullable=True),
        sa.Column("price_per_1m_output_tokens", sa.Numeric(12, 6), nullable=True),
        sa.Column("price_per_1m_cached_tokens", sa.Numeric(12, 6), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False, server_default="USD"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "pricing_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("markup_percent", sa.Numeric(6, 2), nullable=False),
        sa.Column("usd_rub_rate", sa.Numeric(10, 4), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "usage_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("price_id", sa.Integer(), sa.ForeignKey("model_prices.id"), nullable=True),
        sa.Column("litellm_cost", sa.Numeric(12, 6), nullable=True),
        sa.Column("markup_percent", sa.Numeric(6, 2), nullable=True),
        sa.Column("usd_rub_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("charged_rub", sa.Numeric(14, 4), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending','success','failed')", name="ck_usage_events_status"),
    )
    op.create_index("ix_usage_events_customer_id", "usage_events", ["customer_id"])
    op.create_index("ix_usage_events_created_at", "usage_events", ["created_at"])

    op.create_table(
        "topup_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("amount_rub", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="requested"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('requested','confirmed','rejected')", name="ck_topup_requests_status"),
    )
    op.create_index("ix_topup_requests_customer_id", "topup_requests", ["customer_id"])

    op.create_table(
        "wallet_ledger",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("delta_rub", sa.Numeric(14, 4), nullable=False),
        sa.Column("usage_event_id", sa.Uuid(), sa.ForeignKey("usage_events.id"), nullable=True),
        sa.Column("topup_request_id", sa.Integer(), sa.ForeignKey("topup_requests.id"), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "entry_type IN ('topup','usage','refund','adjustment')", name="ck_wallet_ledger_entry_type"
        ),
    )
    op.create_index("ix_wallet_ledger_customer_id", "wallet_ledger", ["customer_id"])


def downgrade() -> None:
    op.drop_table("wallet_ledger")
    op.drop_table("topup_requests")
    op.drop_table("usage_events")
    op.drop_table("pricing_config")
    op.drop_table("model_prices")
    op.drop_table("api_keys")
    op.drop_table("customers")
