"""Ручные операции админа с балансом и редактирование наценки/курса

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wallet_ledger",
        sa.Column("created_by_admin_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
    )
    op.add_column(
        "pricing_config",
        sa.Column("updated_by_admin_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
    )
    # Наценка ниже нуля = продажа дешевле себестоимости, курс <= 0 обнуляет
    # или инвертирует цену вообще всего. Оба значения теперь правятся из
    # интерфейса, поэтому опечатка админа должна отбиваться базой.
    op.create_check_constraint(
        "ck_pricing_config_markup_non_negative", "pricing_config", "markup_percent >= 0"
    )
    op.create_check_constraint("ck_pricing_config_rate_positive", "pricing_config", "usd_rub_rate > 0")


def downgrade() -> None:
    op.drop_constraint("ck_pricing_config_rate_positive", "pricing_config", type_="check")
    op.drop_constraint("ck_pricing_config_markup_non_negative", "pricing_config", type_="check")
    op.drop_column("pricing_config", "updated_by_admin_id")
    op.drop_column("wallet_ledger", "created_by_admin_id")
