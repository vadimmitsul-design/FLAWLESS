"""CHECK-констрейнты: price_rub/amount_rub строго положительные
(защита от бага — отрицательная цена промпта позволяла печатать себе баланс)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint("ck_prompts_price_positive", "prompts", "price_rub > 0")
    op.create_check_constraint("ck_topup_requests_amount_positive", "topup_requests", "amount_rub > 0")


def downgrade() -> None:
    op.drop_constraint("ck_topup_requests_amount_positive", "topup_requests", type_="check")
    op.drop_constraint("ck_prompts_price_positive", "prompts", type_="check")
