"""Автотесты на доработки биллинга из документа "neurohub — доработки до
продуктива" (2026-09-04), раздел 1: резерв под вызов (1.1), раздельная цена
записи в кэш (1.2), честное списание при обрыве стрима (1.3), идемпотентность
(1.4). Хелперы продублированы из других test_*.py намеренно (см. обоснование
в test_features_wave2.py)."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import billing, llm, main, pricing, reaper
from app.db import SessionLocal
from app.models import Customer, ModelPrice, UsageEvent

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "AdminPass123"


def _signup(client, email, name="Test User", password="TestPass123"):
    r = client.post(
        "/signup", data={"email": email, "name": name, "password": password}, follow_redirects=True
    )
    assert r.status_code == 200


def _issue_api_key(client):
    import re

    r = client.post("/api-key/regenerate")
    assert r.status_code == 200
    m = re.search(r"nh_[A-Za-z0-9_-]+", r.text)
    assert m
    return m.group(0)


def _admin_client():
    from fastapi.testclient import TestClient
    from app.main import app

    admin = TestClient(app)
    r = admin.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code in (200, 303)
    return admin


def _topup(client, admin, amount="50"):
    import re

    client.post("/topups/new", data={"amount_rub": amount})
    m = re.search(r"admin/topups/(\d+)/confirm", admin.get("/admin/topups").text)
    assert m
    r = admin.post(f"/admin/topups/{m.group(1)}/confirm")
    assert r.status_code in (200, 303)


def _customer_id(email):
    async def _get():
        async with SessionLocal() as session:
            customer = (await session.execute(select(Customer).where(Customer.email == email))).scalar_one()
            return customer.id

    return asyncio.run(_get())


def _balance(email):
    async def _get():
        async with SessionLocal() as session:
            customer = (await session.execute(select(Customer).where(Customer.email == email))).scalar_one()
            return customer.balance_rub

    return asyncio.run(_get())


# ---------- 1.1 резерв под вызов ----------


def test_second_reservation_blocked_while_first_still_pending(client):
    _signup(client, "reserve1@test.local")
    admin = _admin_client()
    _topup(client, admin, "10")
    customer_id = _customer_id("reserve1@test.local")

    async def _run():
        async with SessionLocal() as session:
            await billing.start_call(
                session, customer_id, customer_id, "openai", "gpt-5-mini", estimated_reserve_rub=Decimal("8")
            )
            with pytest.raises(billing.InsufficientBalance):
                await billing.start_call(
                    session, customer_id, customer_id, "openai", "gpt-5-mini", estimated_reserve_rub=Decimal("8")
                )

    asyncio.run(_run())


def test_reservation_released_once_first_call_finalizes(client):
    _signup(client, "reserve2@test.local")
    admin = _admin_client()
    _topup(client, admin, "10")
    customer_id = _customer_id("reserve2@test.local")

    async def _run():
        async with SessionLocal() as session:
            event = await billing.start_call(
                session, customer_id, customer_id, "openai", "gpt-5-mini", estimated_reserve_rub=Decimal("8")
            )
            await billing.finalize_failure(session, event, error_code="Timeout", latency_ms=10)
            # Резерв снят вместе со сменой статуса — второй вызов теперь проходит.
            await billing.start_call(
                session, customer_id, customer_id, "openai", "gpt-5-mini", estimated_reserve_rub=Decimal("8")
            )

    asyncio.run(_run())


# ---------- 1.2 раздельная цена записи в кэш ----------


def test_cache_write_tokens_priced_separately_from_cache_read():
    price = ModelPrice(
        provider="anthropic",
        model="claude-sonnet-4-6",
        price_per_1m_input_tokens=Decimal("3"),
        price_per_1m_output_tokens=Decimal("15"),
        price_per_1m_cached_tokens=Decimal("0.3"),
        price_per_1m_cache_write_tokens=Decimal("3.75"),
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    usage = pricing.UsageAmounts(
        input_text_tokens=1000, output_tokens=500, cached_tokens=200, cache_write_tokens=100
    )
    cost = pricing.compute_cost(price, usage)
    expected = (
        Decimal("3") * 1000 / 1_000_000
        + Decimal("15") * 500 / 1_000_000
        + Decimal("0.3") * 200 / 1_000_000
        + Decimal("3.75") * 100 / 1_000_000
    ).quantize(Decimal("0.000001"))
    assert cost == expected


# ---------- 1.3 честное списание при обрыве стрима ----------


async def _fake_stream_then_break(alias, messages, **kwargs):
    async def gen():
        yield {"id": "chatcmpl-test", "choices": [{"delta": {"content": "Hello "}}]}
        yield {
            "id": "chatcmpl-test",
            "choices": [{"delta": {"content": "there, this is a partial reply before the connection drops"}}],
        }
        raise RuntimeError("simulated connection drop")

    return alias, "openrouter", "openai/gpt-5-mini", gen()


def test_interrupted_stream_charges_for_delivered_partial_content(client, monkeypatch):
    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_stream_then_break)

    _signup(client, "interrupt1@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "50")
    balance_before = _balance("interrupt1@test.local")

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as r:
        assert r.status_code == 200
        list(r.iter_lines())

    balance_after = _balance("interrupt1@test.local")
    assert balance_after < balance_before  # раньше здесь было бы 0₽ списания

    async def _check():
        async with SessionLocal() as session:
            customer = (
                await session.execute(select(Customer).where(Customer.email == "interrupt1@test.local"))
            ).scalar_one()
            event = (
                await session.execute(
                    select(UsageEvent)
                    .where(UsageEvent.billing_customer_id == customer.id)
                    .order_by(UsageEvent.created_at.desc())
                )
            ).scalars().first()
            return event

    event = asyncio.run(_check())
    assert event.status == "failed"
    assert event.billing_estimated is True
    assert event.charged_rub is not None and event.charged_rub > 0


def test_stream_failing_before_any_content_charges_nothing(client, monkeypatch):
    async def _fake_fail_immediately(alias, messages, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_fail_immediately)

    _signup(client, "interrupt2@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "50")
    balance_before = _balance("interrupt2@test.local")

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as r:
        list(r.iter_lines())

    assert _balance("interrupt2@test.local") == balance_before


# ---------- 1.4 идемпотентность ----------


def test_repeated_request_with_same_idempotency_key_charges_once(client):
    _signup(client, "idem1@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "50")
    balance_after_topup = _balance("idem1@test.local")

    payload = {
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "mock_response": "idempotent reply",
    }
    headers = {"Authorization": f"Bearer {api_key}", "Idempotency-Key": "test-key-1"}

    r1 = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert r1.status_code == 200
    balance_after_first = _balance("idem1@test.local")
    assert balance_after_first < balance_after_topup

    r2 = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert _balance("idem1@test.local") == balance_after_first  # второй раз не списалось


def test_different_idempotency_keys_charge_independently(client):
    _signup(client, "idem2@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "50")
    balance_after_topup = _balance("idem2@test.local")

    payload = {
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "mock_response": "reply",
    }
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "key-a"},
        json=payload,
    )
    balance_after_a = _balance("idem2@test.local")
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "key-b"},
        json=payload,
    )
    balance_after_b = _balance("idem2@test.local")

    assert balance_after_a < balance_after_topup
    assert balance_after_b < balance_after_a


# ---------- фиксы по итогам состязательного ревью 2026-09-04 ----------
# (см. CLAUDE.md — 20 подтверждённых находок, здесь тесты на самые важные)


def test_real_disconnect_via_aclose_still_charges_and_releases_reservation(client, monkeypatch):
    """aclose() — то, что Starlette реально делает с генератором при обрыве
    соединения клиентом (доставляется как GeneratorExit, НЕ Exception).
    Старый except Exception это не ловил вообще — событие зависало в
    'pending' навсегда вместе с резервом."""

    async def _fake_stream(alias, messages, **kwargs):
        async def gen():
            yield {"id": "chatcmpl-test", "choices": [{"delta": {"content": "Hello there, a longer partial reply"}}]}
            yield {"id": "chatcmpl-test", "choices": [{"delta": {"content": " that never finishes"}}]}

        return alias, "openrouter", "openai/gpt-5-mini", gen()

    monkeypatch.setattr(llm, "chat_completion_with_fallback", _fake_stream)

    _signup(client, "disconnect1@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")
    customer_id = _customer_id("disconnect1@test.local")
    balance_before = _balance("disconnect1@test.local")

    async def _run():
        async with SessionLocal() as session:
            event = await billing.start_call(
                session, customer_id, customer_id, "openai", "gpt-5-mini", estimated_reserve_rub=Decimal("10")
            )
            gen = main._stream_chat_completion(
                session, event, "gpt-5-mini", [{"role": "user", "content": "hi"}], {}, None, customer_id
            )
            await gen.__anext__()  # клиент реально получил первый чанк...
            await gen.aclose()  # ...и тут же оборвал соединение
            return event.id

    event_id = asyncio.run(_run())

    async def _check():
        async with SessionLocal() as session:
            return await session.get(UsageEvent, event_id)

    event = asyncio.run(_check())
    assert event.status != "pending"  # резерв не завис навсегда
    assert event.billing_estimated is True
    assert event.charged_rub is not None and event.charged_rub > 0
    assert _balance("disconnect1@test.local") == balance_before - event.charged_rub


def test_missing_price_reserve_falls_back_to_nonzero_default():
    """Раньше при отсутствии строки прайса резерв был 0₽ — вся защита от
    параллельных запросов отключалась именно для непроцененной модели."""

    async def _run():
        async with SessionLocal() as session:
            pricing_cfg = await billing.get_pricing_config(session)
            provider, model = llm.resolve_alias("claude-sonnet")  # не засеян в _seed_prices
            price = await pricing.find_price(session, provider, model, None, None, datetime.now(timezone.utc))
            assert price is None
            return billing.estimate_reserve_rub(price, [{"role": "user", "content": "hi"}], {}, pricing_cfg)

    reserve = asyncio.run(_run())
    assert reserve > 0


def test_second_reservation_blocked_even_for_unpriced_model(client):
    _signup(client, "unpriced1@test.local")
    admin = _admin_client()
    _topup(client, admin, "80")
    customer_id = _customer_id("unpriced1@test.local")

    async def _run():
        async with SessionLocal() as session:
            pricing_cfg = await billing.get_pricing_config(session)
            provider, model = llm.resolve_alias("claude-sonnet")
            price = await pricing.find_price(session, provider, model, None, None, datetime.now(timezone.utc))
            reserve = billing.estimate_reserve_rub(price, [{"role": "user", "content": "hi"}], {}, pricing_cfg)
            await billing.start_call(
                session, customer_id, customer_id, provider, model, estimated_reserve_rub=reserve
            )
            with pytest.raises(billing.InsufficientBalance):
                await billing.start_call(
                    session, customer_id, customer_id, provider, model, estimated_reserve_rub=reserve
                )

    asyncio.run(_run())


def test_idempotency_race_returns_conflict_not_500(client, monkeypatch):
    """Симулирует настоящую гонку: конкурент уже вставил строку с этим же
    ключом, а наша предварительная проверка (TOCTOU) его ещё не увидела —
    ловим IntegrityError из start_call и отдаём чистый 409, а не падаем."""
    _signup(client, "idemrace@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "50")
    customer_id = _customer_id("idemrace@test.local")

    async def _seed_conflicting_event():
        async with SessionLocal() as session:
            await billing.start_call(
                session,
                customer_id,
                customer_id,
                "openai",
                "gpt-5-mini",
                estimated_reserve_rub=Decimal("1"),
                idempotency_key="race-key",
            )

    asyncio.run(_seed_conflicting_event())

    async def _miss(session, actor_customer_id, key):
        return None  # как будто конкурент ещё не закоммитил на момент предварительной проверки

    monkeypatch.setattr(billing, "find_event_by_idempotency_key", _miss)

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "race-key"},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hi"}], "mock_response": "x"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"]["type"] == "idempotency_conflict"


def test_idempotency_key_reused_with_different_request_is_rejected(client):
    _signup(client, "idemmismatch@test.local")
    api_key = _issue_api_key(client)
    admin = _admin_client()
    _topup(client, admin, "50")
    headers = {"Authorization": f"Bearer {api_key}", "Idempotency-Key": "reused-key"}

    r1 = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "first question"}], "mock_response": "a"},
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "totally different question"}], "mock_response": "b"},
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"]["type"] == "idempotency_key_reused"


def test_idempotency_key_scoped_per_child_not_shared_family_wide(client):
    """Раньше ключ идемпотентности жил на уровне billing_customer_id (семья) —
    два разных ребёнка одного родителя с одинаковым ключом получили бы 409
    или чужой кэшированный ответ вместо своего."""
    _signup(client, "idemparent@test.local")
    admin = _admin_client()
    _topup(client, admin, "50")

    r = client.post(
        "/children/new",
        data={"email": "idemkid1@test.local", "name": "Kid One", "password": "KidPass123"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    client.post(
        "/children/new",
        data={"email": "idemkid2@test.local", "name": "Kid Two", "password": "KidPass123"},
        follow_redirects=True,
    )

    from fastapi.testclient import TestClient
    from app.main import app

    kid1 = TestClient(app)
    kid1.post("/login", data={"email": "idemkid1@test.local", "password": "KidPass123"})
    kid1_key = _issue_api_key(kid1)

    kid2 = TestClient(app)
    kid2.post("/login", data={"email": "idemkid2@test.local", "password": "KidPass123"})
    kid2_key = _issue_api_key(kid2)

    shared_key = "same-key-both-kids"
    r1 = kid1.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {kid1_key}", "Idempotency-Key": shared_key},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "объясни теорему пифагора"}], "mock_response": "kid1 reply"},
    )
    r2 = kid2.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {kid2_key}", "Idempotency-Key": shared_key},
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "объясни теорему пифагора"}], "mock_response": "kid2 reply"},
    )

    assert r1.status_code == 200
    assert r2.status_code == 200  # не 409 — разные акторы, один и тот же ключ не конфликтует
    assert r1.json()["choices"][0]["message"]["content"] != r2.json()["choices"][0]["message"]["content"]


def test_prompt_fee_included_in_reservation(client):
    """Раньше плата за промпт (Prompt.price_rub) списывалась постфактум БЕЗ
    участия в резерве — N параллельных вызовов с одним и тем же платным
    промптом воспроизводили исходный баг, просто для роялти-механики."""
    from fastapi.testclient import TestClient
    from app.main import app

    author = TestClient(app)
    _signup(author, "promptauthor@test.local", name="Author")
    author.post(
        "/prompts",
        data={"title": "Помощник", "system_prompt": "Ты помощник", "price_rub": "30.00"},
        follow_redirects=True,
    )
    import re

    prompt_id = re.search(r'<td class="mono">(\d+)</td>\s*</tr>', author.get("/prompts").text).group(1)

    async def _run():
        async with SessionLocal() as session:
            pricing_cfg = await billing.get_pricing_config(session)
            provider, model = llm.resolve_alias("gpt-5-mini")
            price = await pricing.find_price(session, provider, model, None, None, datetime.now(timezone.utc))
            messages = [{"role": "user", "content": "hi"}]
            reserve_without_prompt = billing.estimate_reserve_rub(price, messages, {}, pricing_cfg)
            reserve_with_prompt = billing.estimate_reserve_rub(
                price, messages, {}, pricing_cfg, extra_fixed_rub=Decimal("30.00")
            )
            return reserve_without_prompt, reserve_with_prompt

    reserve_without_prompt, reserve_with_prompt = asyncio.run(_run())
    assert reserve_with_prompt >= reserve_without_prompt + Decimal("30.00")


def test_reaper_releases_reservation_stuck_by_a_dead_process(client):
    """finally в _stream_chat_completion ловит обрыв соединения, но не спасает
    от полного падения процесса (OOM/kill -9) между start_call и finalize_* —
    тогда не выполнится вообще никакой Python-код. Единственная страховка —
    внешняя периодическая подметка (app/reaper.py)."""
    _signup(client, "reaper1@test.local")
    admin = _admin_client()
    _topup(client, admin, "10")
    customer_id = _customer_id("reaper1@test.local")

    async def _create_stuck_event():
        async with SessionLocal() as session:
            event = await billing.start_call(
                session, customer_id, customer_id, "openai", "gpt-5-mini", estimated_reserve_rub=Decimal("8")
            )
            # Симулируем "процесс упал 20 минут назад, не успев финализировать"
            event.created_at = datetime.now(timezone.utc) - timedelta(seconds=1200)
            session.add(event)
            await session.commit()
            return event.id

    event_id = asyncio.run(_create_stuck_event())

    # Без уборщика второй вызов был бы заблокирован навсегда этим резервом.
    async def _check_blocked_before_reap():
        async with SessionLocal() as session:
            with pytest.raises(billing.InsufficientBalance):
                await billing.start_call(
                    session, customer_id, customer_id, "openai", "gpt-5-mini", estimated_reserve_rub=Decimal("8")
                )

    asyncio.run(_check_blocked_before_reap())

    async def _reap():
        async with SessionLocal() as session:
            return await reaper.reap_stale_pending_events(session)

    reaped_count = asyncio.run(_reap())
    assert reaped_count >= 1

    async def _check_after_reap():
        async with SessionLocal() as session:
            event = await session.get(UsageEvent, event_id)
            assert event.status == "failed"
            assert event.error_code == "StaleReservationReaped"
            assert event.charged_rub is None  # неизвестный вызов — денег не берём, только освобождаем резерв
            # теперь второй вызов проходит — резерв реально снят
            await billing.start_call(
                session, customer_id, customer_id, "openai", "gpt-5-mini", estimated_reserve_rub=Decimal("8")
            )

    asyncio.run(_check_after_reap())
