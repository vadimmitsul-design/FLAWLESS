"""Несколько именованных API-ключей на клиента с лимитами расхода (2.1-2.2
доработок) + привязка usage_events к конкретному ключу

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-05

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("name", sa.Text(), nullable=False, server_default=""))
    op.add_column("api_keys", sa.Column("last_four", sa.Text(), nullable=False, server_default=""))
    op.add_column("api_keys", sa.Column("daily_limit_rub", sa.Numeric(14, 4), nullable=True))
    op.add_column("api_keys", sa.Column("monthly_limit_rub", sa.Numeric(14, 4), nullable=True))
    op.add_column("api_keys", sa.Column("admin_daily_limit_rub", sa.Numeric(14, 4), nullable=True))
    op.add_column("api_keys", sa.Column("admin_monthly_limit_rub", sa.Numeric(14, 4), nullable=True))

    op.add_column(
        "usage_events",
        sa.Column("api_key_id", sa.Integer(), sa.ForeignKey("api_keys.id"), nullable=True),
    )
    op.create_index("ix_usage_events_api_key_id", "usage_events", ["api_key_id"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_api_key_id", table_name="usage_events")
    op.drop_column("usage_events", "api_key_id")

    op.drop_column("api_keys", "admin_monthly_limit_rub")
    op.drop_column("api_keys", "admin_daily_limit_rub")
    op.drop_column("api_keys", "monthly_limit_rub")
    op.drop_column("api_keys", "daily_limit_rub")
    op.drop_column("api_keys", "last_four")
    op.drop_column("api_keys", "name")
