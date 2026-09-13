"""Заявки сотрудников на подписки и прокси

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-13

Полный цикл, а не справочник: сотрудник просит → администратор оплачивает
картой у поставщика → фиксирует факт → заявка превращается в ресурс со
сроком, и дальше за сроком следит система.

Заявка и ресурс — разные вещи и разные таблицы. Заявка это намерение
(«нужна подписка на такой-то аккаунт»), она может быть отклонена и остаётся
в истории как отказ. Ресурс — то, что реально куплено и что кончается.
Одна заявка порождает максимум один ресурс, поэтому связь хранится на
заявке (`resource_id`), а не наоборот.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "resource_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False, index=True
        ),
        sa.Column("kind", sa.Text(), nullable=False, server_default="subscription"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        # На какой аккаунт оформлять. Для подписок это главное поле: админ
        # оплачивает картой внутри чужого аккаунта, а не заводит новый.
        sa.Column("account", sa.Text(), nullable=True),
        sa.Column("period_months", sa.Integer(), nullable=True),
        sa.Column("estimated_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False, server_default="RUB"),
        # Зачем нужно — чтобы админ решал по существу, а не угадывал.
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="requested"),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("decided_by_admin_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        # Заполняется, когда заявка оплачена и превратилась в ресурс.
        sa.Column("resource_id", sa.Integer(), sa.ForeignKey("resources.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status in ('requested','rejected','fulfilled')", name="ck_resource_requests_status"
        ),
        sa.CheckConstraint(
            "kind in ('proxy','subscription','domain','service','other')",
            name="ck_resource_requests_kind",
        ),
        sa.CheckConstraint(
            "estimated_amount is null or estimated_amount > 0",
            name="ck_resource_requests_amount_positive",
        ),
        sa.CheckConstraint(
            "period_months is null or period_months > 0", name="ck_resource_requests_period_positive"
        ),
    )
    op.create_index("ix_resource_requests_status", "resource_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_resource_requests_status", table_name="resource_requests")
    op.drop_table("resource_requests")
