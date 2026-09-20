"""Должность и подразделение сотрудника

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-20

Внутреннему контуру нужен ответ на вопрос «у кого кончается подписка» —
а «кто» до сих пор означало одну строку `name`, которую человек вписывал
себе сам при регистрации в поле с подписью «Имя / компания», и которую
администратор не мог поправить ничем, кроме SQL.

Два поля, не двадцать. Подразделение — единственная ось, по которой список
реально режут, когда людей становится больше двадцати, и по которой
распределяют бюджет. Всё остальное «и тд» уже есть: связь с человеком —
email (он же логин), работает ли он — `active`, сколько тратит —
/admin/customers.

`job_title`, а не `position`: POSITION в PostgreSQL — ключевое слово, и
SQLAlchemy такой идентификатор не квотирует. Формально легально, но имя
без подвоха стоит бесплатно.

Оба nullable: у существующих строк этих данных нет и взяться им неоткуда,
а NOT NULL с пустой строкой по умолчанию сделал бы «не заполнено»
неотличимым от «заполнено пустым».
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("job_title", sa.Text(), nullable=True))
    op.add_column("customers", sa.Column("department", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("customers", "department")
    op.drop_column("customers", "job_title")
