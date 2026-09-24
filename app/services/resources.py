"""Resource queries and operations; the caller owns the transaction commit."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TypedDict

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import EntityNotFound, InvalidInput, StateConflict
from app.db.models import (
    Customer,
    Resource,
    ResourcePayment,
    ResourceRequest,
    as_utc,
    days_left,
    days_phrase,
    utcnow,
)


class ResourceState(TypedDict):
    code: str
    title: str
    days: int | None


class PaidTotal(TypedDict):
    sign: str
    amount: Decimal


class ResourceItem(TypedDict):
    r: Resource
    state: ResourceState
    phrase: str
    kind_title: str
    paid_totals: list[PaidTotal]


class ResourceDashboard(TypedDict):
    items: list[ResourceItem]
    counts: dict[str, int]
    total_count: int
    today_line: str
    f: str
    owner: str
    pending: Sequence[ResourceRequest]
    decided: Sequence[ResourceRequest]
    people: Sequence[Customer]
    payments: Sequence[ResourcePayment]
    owners: dict[int, Customer]


@dataclass(frozen=True, slots=True)
class ResourceDetails:
    name: str
    owner_customer_id: int
    account: str = ""
    url: str = ""
    expires_at: datetime | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class PaymentDetails:
    amount: Decimal
    currency: str
    period_end: datetime | None
    paid_at: datetime | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class RequestDetails:
    kind: str
    name: str
    provider: str = ""
    account: str = ""
    period_months: int | None = None
    estimated_amount: Decimal | None = None
    currency: str = "RUB"
    reason: str = ""


# ---------- ресурсы со сроком (прокси, подписки) ----------

_CURRENCY_SIGNS = {"RUB": "₽", "USD": "$", "EUR": "€"}


RESOURCE_KIND_TITLES = {
    "proxy": "Прокси",
    "subscription": "Подписка",
    "domain": "Домен",
    "service": "Сервис",
    "other": "Другое",
}


def resource_state(resource: Resource, now: datetime, warn_days: int) -> ResourceState:
    """Состояние считается из даты, а не хранится: хранимый статус протухает
    молча в ту же секунду, как проходит срок."""
    expires = as_utc(resource.expires_at)
    if expires is None:
        return {"code": "unknown", "title": "Срок не указан", "days": None}
    left = days_left(expires, now)
    assert left is not None
    if left < 0:
        return {"code": "expired", "title": "Просрочен", "days": left}
    if left <= warn_days:
        return {"code": "soon", "title": "Истекает", "days": left}
    return {"code": "ok", "title": "Активен", "days": left}


async def resources_with_state(
    session: AsyncSession, *, owner_id: int | None = None, include_archived: bool = False
) -> list[ResourceItem]:
    stmt = select(Resource)
    if owner_id is not None:
        stmt = stmt.where(Resource.owner_customer_id == owner_id)
    if not include_archived:
        stmt = stmt.where(Resource.archived.is_(False))
    # Порядок задаётся ниже, в Python: SQL-сортировка по дате возрастанию
    # давала обратное задуманному — подписка, просроченная восемь месяцев
    # назад и всеми брошенная, вставала НАД прокси, истёкшим вчера. Чем
    # дольше строка гниёт, тем выше лезла.
    rows = (await session.execute(stmt.order_by(Resource.name))).scalars().all()

    now = utcnow()
    warn = settings.resource_expiry_warn_days
    # По каждой валюте отдельно. Раньше сумма считалась только по рублёвым
    # строкам, и подписка, оплаченная картой за $20 у зарубежного поставщика
    # (основной сценарий этого раздела), показывала «0,00 ₽» — без единого
    # признака, что часть платежей отброшена. Курс в платеже не хранится,
    # привести к рублям задним числом нечем, поэтому показываем как есть.
    paid: dict[int, dict[str, Decimal]] = {}
    for resource_id, currency, total in (
        await session.execute(
            select(
                ResourcePayment.resource_id,
                ResourcePayment.currency,
                func.coalesce(func.sum(ResourcePayment.amount), 0),
            ).group_by(ResourcePayment.resource_id, ResourcePayment.currency)
        )
    ).all():
        paid.setdefault(resource_id, {})[currency] = total
    items: list[ResourceItem] = []
    for r in rows:
        state = resource_state(r, now, warn)
        items.append(
            {
                "r": r,
                "state": state,
                "phrase": days_phrase(state["days"]),
                "kind_title": RESOURCE_KIND_TITLES.get(r.kind, r.kind),
                "paid_totals": [
                    {"sign": _CURRENCY_SIGNS.get(cur, cur), "amount": total}
                    for cur, total in sorted(paid.get(r.id, {}).items())
                ],
            }
        )
    items.sort(key=resource_order)
    return items


# Порядок корзин на дашборде. Внутри корзины сортировка РАЗНАЯ, и это
# осознанно: среди просроченных выше стоит свежий (его чинят), а не
# годичной давности (его архивируют); среди истекающих — ближайший.
_RESOURCE_BUCKETS = {"expired": 0, "soon": 1, "unknown": 2, "ok": 3}


def resource_order(item: ResourceItem) -> tuple[int, int, str]:
    state = item["state"]
    bucket = _RESOURCE_BUCKETS.get(state["code"], 9)
    days = state["days"]
    name = (item["r"].name or "").lower()
    if days is None:
        return (bucket, 0, name)
    if state["code"] == "expired":
        return (bucket, -days, name)
    return (bucket, days, name)


_RESOURCE_FILTERS = ("expired", "soon", "unknown", "ok")


def _owner_suffix(owners: dict[int, Customer], item: ResourceItem) -> str:
    """« (Иванов, бэкенд)» — или пусто, если владелец не найден."""
    person = owners.get(item["r"].owner_customer_id)
    if person is None:
        return ""
    who = person.name
    if person.job_title:
        who += f", {person.job_title}"
    return f" ({who})"


async def customer_requests(session: AsyncSession, customer_id: int) -> Sequence[ResourceRequest]:
    return (
        await session.scalars(
            select(ResourceRequest)
            .where(ResourceRequest.customer_id == customer_id)
            .order_by(ResourceRequest.created_at.desc())
            .limit(20)
        )
    ).all()


async def resource_dashboard(
    session: AsyncSession, *, include_archived: bool, filter_code: str, owner_id: int | None
) -> ResourceDashboard:
    all_items = await resources_with_state(session, include_archived=include_archived)
    counts = {code: 0 for code in _RESOURCE_FILTERS}
    for item in all_items:
        code = item["state"]["code"]
        if code in counts:
            counts[code] += 1
    items = all_items
    if owner_id is not None:
        items = [item for item in items if item["r"].owner_customer_id == owner_id]
    if filter_code in _RESOURCE_FILTERS:
        items = [item for item in items if item["state"]["code"] == filter_code]

    people = (
        await session.scalars(select(Customer).where(Customer.active).order_by(Customer.name))
    ).all()
    payments = (
        await session.scalars(
            select(ResourcePayment).order_by(ResourcePayment.paid_at.desc()).limit(30)
        )
    ).all()
    owners = {customer.id: customer for customer in (await session.scalars(select(Customer))).all()}
    pending = (
        await session.scalars(
            select(ResourceRequest)
            .where(ResourceRequest.status == "requested")
            .order_by(ResourceRequest.created_at)
        )
    ).all()
    decided = (
        await session.scalars(
            select(ResourceRequest)
            .where(ResourceRequest.status != "requested")
            .order_by(ResourceRequest.decided_at.desc())
            .limit(15)
        )
    ).all()
    # The summary always describes the whole dashboard, regardless of filters.
    burning = [item for item in all_items if item["state"]["code"] in ("expired", "soon")]
    upcoming = [item for item in all_items if item["state"]["code"] == "ok"]
    if burning:
        first = burning[0]
        today_line = (
            f"Требует внимания: {len(burning)}. Ближе всех — «{first['r'].name}»"
            f"{_owner_suffix(owners, first)}, {first['phrase']}"
        )
    elif upcoming:
        first = upcoming[0]
        today_line = (
            f"Ничего не горит. Ближайший срок — «{first['r'].name}»"
            f"{_owner_suffix(owners, first)}, {first['phrase']}"
        )
    elif all_items:
        today_line = (
            "Ни у одного ресурса не указан срок — система не сможет предупредить об окончании."
        )
    else:
        today_line = ""
    return {
        "items": items,
        "counts": counts,
        "total_count": len(all_items),
        "today_line": today_line,
        "f": filter_code if filter_code in _RESOURCE_FILTERS else "",
        "owner": str(owner_id) if owner_id is not None else "",
        "pending": pending,
        "decided": decided,
        "people": people,
        "payments": payments,
        "owners": owners,
    }


def _check_currency(currency: str) -> None:
    if currency not in ResourcePayment.CURRENCIES:
        raise InvalidInput(f"валюта {currency} не поддерживается")


def _check_amount(amount: Decimal) -> None:
    if not amount.is_finite() or amount <= 0:
        raise InvalidInput("сумма должна быть больше нуля")


def _payment_end(details: PaymentDetails) -> datetime:
    _check_amount(details.amount)
    _check_currency(details.currency)
    if details.period_end is None:
        raise InvalidInput("укажите, до какого числа оплачено")
    return details.period_end


async def _resource_for_update(session: AsyncSession, resource_id: int) -> Resource:
    resource = await session.get(
        Resource, resource_id, with_for_update=True, populate_existing=True
    )
    if resource is None:
        raise EntityNotFound("Not Found")
    return resource


async def _check_owner(session: AsyncSession, customer_id: int) -> None:
    if await session.get(Customer, customer_id) is None:
        raise EntityNotFound("сотрудник не найден")


async def create_resource(
    session: AsyncSession,
    details: ResourceDetails,
    *,
    kind: str,
    provider: str,
    admin_id: int,
) -> Resource:
    if kind not in Resource.KINDS:
        raise InvalidInput(f"неизвестный вид ресурса: {kind}")
    await _check_owner(session, details.owner_customer_id)
    resource = Resource(
        kind=kind,
        name=details.name.strip(),
        provider=provider.strip() or None,
        owner_customer_id=details.owner_customer_id,
        account=details.account.strip() or None,
        url=details.url.strip() or None,
        expires_at=details.expires_at,
        note=details.note.strip() or None,
        created_by_admin_id=admin_id,
    )
    session.add(resource)
    await session.flush()
    return resource


async def edit_resource(
    session: AsyncSession, resource_id: int, details: ResourceDetails
) -> Resource:
    """An explicit correction may move expiry backwards; a payment may not."""
    resource = await _resource_for_update(session, resource_id)
    if not details.name.strip():
        raise InvalidInput("название не может быть пустым")
    await _check_owner(session, details.owner_customer_id)
    resource.name = details.name.strip()
    resource.owner_customer_id = details.owner_customer_id
    resource.account = details.account.strip() or None
    resource.url = details.url.strip() or None
    resource.expires_at = details.expires_at
    resource.note = details.note.strip() or None
    await session.flush()
    return resource


async def record_payment(
    session: AsyncSession, resource_id: int, details: PaymentDetails, *, admin_id: int
) -> ResourcePayment:
    """Append a payment and extend expiry together without touching the wallet."""
    resource = await _resource_for_update(session, resource_id)
    end = _payment_end(details)
    payment = ResourcePayment(
        resource_id=resource.id,
        amount=details.amount,
        currency=details.currency,
        paid_at=details.paid_at or utcnow(),
        period_start=resource.expires_at,
        period_end=end,
        note=details.note.strip() or None,
        created_by_admin_id=admin_id,
    )
    session.add(payment)
    current = as_utc(resource.expires_at)
    if current is None or end > current:
        resource.expires_at = end
    await session.flush()
    return payment


async def toggle_archive(session: AsyncSession, resource_id: int) -> None:
    resource = await _resource_for_update(session, resource_id)
    resource.archived = not resource.archived
    await session.flush()


async def request_resource(
    session: AsyncSession, customer_id: int, details: RequestDetails
) -> ResourceRequest:
    if details.kind not in Resource.KINDS:
        raise InvalidInput(f"неизвестный вид: {details.kind}")
    if not details.name.strip():
        raise InvalidInput("опишите, что именно нужно")
    _check_currency(details.currency)
    if details.estimated_amount is not None:
        _check_amount(details.estimated_amount)
    if details.period_months is not None and details.period_months <= 0:
        raise InvalidInput("срок: должно быть больше нуля")
    resource_request = ResourceRequest(
        customer_id=customer_id,
        kind=details.kind,
        name=details.name.strip(),
        provider=details.provider.strip() or None,
        account=details.account.strip() or None,
        period_months=details.period_months,
        estimated_amount=details.estimated_amount,
        currency=details.currency,
        reason=details.reason.strip() or None,
    )
    session.add(resource_request)
    await session.flush()
    return resource_request


async def _pending_request(session: AsyncSession, request_id: int) -> ResourceRequest:
    resource_request = await session.get(ResourceRequest, request_id)
    if resource_request is None:
        raise EntityNotFound("Not Found")
    if resource_request.status != "requested":
        raise StateConflict("по заявке уже принято решение")
    return resource_request


async def _claim_request(
    session: AsyncSession, resource_request: ResourceRequest, *, status: str
) -> None:
    # Compare-and-set protects both fulfil/reject paths from double submission.
    claimed = await session.execute(
        update(ResourceRequest)
        .where(ResourceRequest.id == resource_request.id, ResourceRequest.status == "requested")
        .values(status=status)
        .returning(ResourceRequest.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.scalar_one_or_none() is None:
        raise StateConflict("по заявке уже принято решение")
    resource_request.status = status


async def reject_request(
    session: AsyncSession, request_id: int, *, admin_id: int, decision_note: str
) -> None:
    resource_request = await _pending_request(session, request_id)
    if not decision_note.strip():
        raise InvalidInput("укажите причину отказа")
    await _claim_request(session, resource_request, status="rejected")
    resource_request.decision_note = decision_note.strip()
    resource_request.decided_by_admin_id = admin_id
    resource_request.decided_at = utcnow()
    await session.flush()


async def fulfil_request(
    session: AsyncSession,
    request_id: int,
    payment: PaymentDetails,
    *,
    admin_id: int,
    url: str,
) -> Resource:
    """Claim the request, create its resource and append payment in one transaction."""
    resource_request = await _pending_request(session, request_id)
    end = _payment_end(payment)
    await _claim_request(session, resource_request, status="fulfilled")
    resource = Resource(
        kind=resource_request.kind,
        name=resource_request.name,
        provider=resource_request.provider,
        owner_customer_id=resource_request.customer_id,
        account=resource_request.account,
        url=url.strip() or None,
        expires_at=end,
        note=resource_request.reason,
        created_by_admin_id=admin_id,
    )
    session.add(resource)
    await session.flush()
    session.add(
        ResourcePayment(
            resource_id=resource.id,
            amount=payment.amount,
            currency=payment.currency,
            paid_at=payment.paid_at or utcnow(),
            period_start=None,
            period_end=end,
            note=payment.note.strip() or None,
            created_by_admin_id=admin_id,
        )
    )
    resource_request.decision_note = payment.note.strip() or None
    resource_request.decided_by_admin_id = admin_id
    resource_request.decided_at = utcnow()
    resource_request.resource_id = resource.id
    await session.flush()
    return resource
