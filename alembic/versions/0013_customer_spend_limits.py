"""Потолок расхода на человека (кошелёк), действует во всех трёх дверях

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("daily_limit_rub", sa.Numeric(14, 4), nullable=True))
    op.add_column("customers", sa.Column("monthly_limit_rub", sa.Numeric(14, 4), nullable=True))
    # Индекс под сам расчёт лимита: SUM(charged_rub) по кошельку за период.
    # Раньше такой выборки не было — считали только по api_key_id.
    op.create_index(
        "ix_usage_events_billing_customer_created",
        "usage_events",
        ["billing_customer_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_billing_customer_created", table_name="usage_events")
    op.drop_column("customers", "monthly_limit_rub")
    op.drop_column("customers", "daily_limit_rub")
