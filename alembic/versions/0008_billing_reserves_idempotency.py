"""Резерв под вызов, идемпотентность, цена записи в кэш, честное списание
при обрыве стрима (доработки 1.1-1.4)

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-04

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("model_prices", sa.Column("price_per_1m_cache_write_tokens", sa.Numeric(12, 6), nullable=True))

    op.add_column("usage_events", sa.Column("cache_write_tokens", sa.Integer(), nullable=True))
    op.add_column("usage_events", sa.Column("reserved_rub", sa.Numeric(14, 4), nullable=True))
    op.add_column(
        "usage_events",
        sa.Column("billing_estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("usage_events", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.add_column("usage_events", sa.Column("idempotency_request_hash", sa.Text(), nullable=True))
    op.add_column("usage_events", sa.Column("response_snapshot", sa.Text(), nullable=True))
    op.add_column("usage_events", sa.Column("response_status_code", sa.Integer(), nullable=True))
    # По actor'у (customer_id), не по billing_customer_id — см. CLAUDE.md,
    # находка состязательного ревью 2026-09-04 (два ребёнка одного родителя
    # иначе делили бы одно пространство ключей идемпотентности).
    op.create_unique_constraint(
        "uq_usage_events_customer_idempotency_key",
        "usage_events",
        ["customer_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_usage_events_customer_idempotency_key", "usage_events", type_="unique")
    op.drop_column("usage_events", "response_status_code")
    op.drop_column("usage_events", "response_snapshot")
    op.drop_column("usage_events", "idempotency_request_hash")
    op.drop_column("usage_events", "idempotency_key")
    op.drop_column("usage_events", "billing_estimated")
    op.drop_column("usage_events", "reserved_rub")
    op.drop_column("usage_events", "cache_write_tokens")

    op.drop_column("model_prices", "price_per_1m_cache_write_tokens")
