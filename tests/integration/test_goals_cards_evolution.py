from __future__ import annotations

import pytest

from tests.conftest import TEST_DB_URL

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_goals_return_budget_projection_and_risk_status(client, auth_headers):
    settings_response = await client.post(
        "/api/settings",
        headers=auth_headers,
        json={"monthlyIncome": 3000, "dailyGoal": 100, "reserveAmount": 300},
    )
    assert settings_response.status_code == 200, settings_response.text

    expense_response = await client.post(
        "/api/transactions",
        headers=auth_headers,
        json={
            "title": "Mercado",
            "amount": 900,
            "type": "expense",
            "categoryId": None,
            "paymentMethod": "pix",
            "transactionDate": "2024-05-10",
            "notes": "",
            "cardId": None,
            "billingMonth": None,
            "isRecurring": False,
        },
    )
    assert expense_response.status_code == 200, expense_response.text

    response = await client.get("/api/goals?month=2024-05", headers=auth_headers)
    assert response.status_code == 200, response.text
    goals = response.json()
    assert goals["dailyGoal"] == 100
    assert goals["reserveAmount"] == 300
    assert goals["availableBudget"] == 2700
    assert goals["recommendedDailyGoal"] == 87.1
    assert goals["targetDailyGoal"] == 100
    assert goals["dailyGoal"] != goals["recommendedDailyGoal"]
    assert goals["daysAboveGoal"] == 1
    assert goals["projectedClosing"] == 900
    assert goals["goalStatus"] == "green"


@pytest.mark.asyncio
async def test_user_daily_goal_stays_separate_from_recommended_goal(client, auth_headers):
    settings_response = await client.post(
        "/api/settings",
        headers=auth_headers,
        json={"monthlyIncome": 3000, "dailyGoal": 80, "reserveAmount": 0},
    )
    assert settings_response.status_code == 200, settings_response.text

    response = await client.get("/api/goals?month=2024-05", headers=auth_headers)
    assert response.status_code == 200, response.text
    goals = response.json()

    assert goals["dailyGoal"] == 80
    assert goals["recommendedDailyGoal"] == 96.77
    assert goals["targetDailyGoal"] == 80


@pytest.mark.asyncio
async def test_effective_income_does_not_double_count_income_transaction(client, auth_headers):
    """DOM-05: settings.monthly_income somado a inflow (soma das entradas do
    mês) sem checar se já eram a mesma coisa dobrava o orçamento disponível
    quando o usuário lançava o salário como transação — o gesto natural.
    get_effective_income usa o maior entre os dois, não a soma."""
    settings_response = await client.post(
        "/api/settings",
        headers=auth_headers,
        json={"monthlyIncome": 3000, "dailyGoal": 0, "reserveAmount": 0},
    )
    assert settings_response.status_code == 200, settings_response.text

    salary_response = await client.post(
        "/api/transactions",
        headers=auth_headers,
        json={
            "title": "Salário",
            "amount": 3000,
            "type": "income",
            "paymentMethod": "pix",
            "transactionDate": "2024-05-05",
        },
    )
    assert salary_response.status_code == 200, salary_response.text

    goals_response = await client.get("/api/goals?month=2024-05", headers=auth_headers)
    assert goals_response.status_code == 200, goals_response.text
    # Sem a correção: available_budget = 3000 (configurado) + 3000
    # (lançado) = 6000.
    assert goals_response.json()["availableBudget"] == 3000

    bootstrap_response = await client.get("/api/bootstrap?month=2024-05", headers=auth_headers)
    assert bootstrap_response.status_code == 200, bootstrap_response.text
    assert bootstrap_response.json()["dashboard"]["balance"] == 3000

    # Em um mês sem nada lançado, cai para o valor configurado — não some.
    other_month_response = await client.get("/api/goals?month=2024-06", headers=auth_headers)
    assert other_month_response.status_code == 200, other_month_response.text
    assert other_month_response.json()["availableBudget"] == 3000


@pytest.mark.asyncio
async def test_goals_daily_calendar_matches_month_total_for_billed_installments(client, auth_headers):
    """FIN-02: a série diária do calendário filtrava por transaction_date
    BETWEEN, enquanto o total do mês filtra pelo mês efetivo (billing_month).
    Uma parcela comprada em agosto e faturada em setembro entrava no total
    de setembro mas sumia das barras diárias."""
    card_response = await client.post(
        "/api/cards",
        headers=auth_headers,
        json={
            "name": "Cartão",
            "brand": "Visa",
            "lastFour": "1234",
            "creditLimit": 5000,
            "closingDay": 20,
            "dueDay": 28,
            "color": "#171717",
        },
    )
    assert card_response.status_code == 200, card_response.text
    card_id = card_response.json()["id"]

    installments_response = await client.post(
        f"/api/cards/{card_id}/installments",
        headers=auth_headers,
        json={
            "title": "Compra parcelada",
            "totalAmount": 200,
            "totalInstallments": 2,
            "purchaseDate": "2026-08-25",  # depois do fechamento (dia 20)
        },
    )
    assert installments_response.status_code == 200, installments_response.text
    rows = installments_response.json()["rows"]
    first_installment = next(row for row in rows if row["installment_number"] == 1)
    assert first_installment["billing_month"] == "2026-09"
    assert first_installment["transaction_date"] == "2026-08-25"

    goals_response = await client.get("/api/goals?month=2026-09", headers=auth_headers)
    assert goals_response.status_code == 200, goals_response.text
    goals = goals_response.json()

    days_sum = sum(day["spent"] for day in goals["days"])
    assert days_sum == goals["totalOutflow"]
    assert days_sum > 0


@pytest.mark.asyncio
async def test_cards_return_commitment_grouped_installments_and_purchase_simulation(client, auth_headers):
    category_response = await client.post(
        "/api/categories",
        headers=auth_headers,
        json={"name": "Eletronicos", "type": "expense", "color": "#90caf9", "icon": "E"},
    )
    assert category_response.status_code == 200, category_response.text
    category_id = category_response.json()["id"]

    card_response = await client.post(
        "/api/cards",
        headers=auth_headers,
        json={
            "name": "Controle",
            "brand": "Mastercard",
            "lastFour": "4321",
            "creditLimit": 1000,
            "closingDay": 5,
            "dueDay": 12,
            "color": "#222222",
        },
    )
    assert card_response.status_code == 200, card_response.text
    card_id = card_response.json()["id"]

    installments_response = await client.post(
        f"/api/cards/{card_id}/installments",
        headers=auth_headers,
        json={
            "title": "Celular",
            "categoryId": category_id,
            "totalAmount": 600,
            "totalInstallments": 6,
            "purchaseDate": "2024-05-01",
            "notes": "",
        },
    )
    assert installments_response.status_code == 200, installments_response.text

    cards_response = await client.get("/api/cards?month=2024-05", headers=auth_headers)
    assert cards_response.status_code == 200, cards_response.text
    card_summary = cards_response.json()[0]
    assert card_summary["invoice"] == 100
    assert card_summary["committedLimit"] == 600
    assert card_summary["remainingInstallments"] == 6

    simulation_response = await client.post(
        f"/api/cards/{card_id}/purchase-simulation",
        headers=auth_headers,
        json={"totalAmount": 100, "totalInstallments": 3, "purchaseDate": "2024-05-15", "months": 4},
    )
    assert simulation_response.status_code == 200, simulation_response.text
    simulation = simulation_response.json()
    assert simulation["installments"] == [33.33, 33.33, 33.34]
    assert simulation["projection"][0]["projectedTotal"] == 133.33
    assert simulation["projection"][2]["projectedTotal"] == 133.34
