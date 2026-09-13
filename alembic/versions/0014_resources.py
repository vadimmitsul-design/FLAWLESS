"""Ресурсы со сроком (прокси, подписки) и журнал платежей по ним

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-13

Зачем две таблицы, а не одна. Ресурс — это вещь («прокси RU у такого-то
поставщика, закреплён за Петровым»), она живёт долго. Платёж — факт: сколько
и за какой период заплатили. Если хранить только ресурс с датой окончания,
история платежей теряется при каждом продлении и на вопрос «сколько ушло на
прокси за квартал» ответить нечем. Поэтому продление = новая строка в
журнале, а `expires_at` у ресурса лишь двигается вперёд.

Деньги здесь НЕ проходят через рублёвый кошелёк сервиса: администратор
платит картой у поставщика и фиксирует факт. Поэтому wallet_ledger не
затрагивается — это отдельный журнал расходов компании.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "resources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default="proxy"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        # За кем закреплён. Сотрудник видит в кабинете только свои.
        sa.Column(
            "owner_customer_id",
            sa.Integer(),
            sa.ForeignKey("customers.id"),
            nullable=False,
            index=True,
        ),
        # Логин или идентификатор у поставщика. ПАРОЛЕЙ ЗДЕСЬ НЕТ И БЫТЬ НЕ
        # ДОЛЖНО: сервис не хранилище секретов, шифрования на диске нет.
        sa.Column("account", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        # Дата, до которой оплачено. Статус (активен/истекает/просрочен)
        # считается из неё, а не хранится: хранимый статус протухает молча.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_by_admin_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind in ('proxy','subscription','domain','service','other')",
            name="ck_resources_kind",
        ),
    )
    op.create_index("ix_resources_expires_at", "resources", ["expires_at"])

    op.create_table(
        "resource_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "resource_id",
            sa.Integer(),
            sa.ForeignKey("resources.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="RUB"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        # Период, который закрыт этим платежом. period_end становится новым
        # expires_at ресурса.
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_admin_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_resource_payments_amount_positive"),
        sa.CheckConstraint("currency in ('RUB','USD','EUR')", name="ck_resource_payments_currency"),
    )
    op.create_index("ix_resource_payments_paid_at", "resource_payments", ["paid_at"])


def downgrade() -> None:
    op.drop_index("ix_resource_payments_paid_at", table_name="resource_payments")
    op.drop_table("resource_payments")
    op.drop_index("ix_resources_expires_at", table_name="resources")
    op.drop_table("resources")
