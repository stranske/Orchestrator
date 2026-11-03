import math
import random

import pytest

import comparative


def ricardian():
    return (
        {"A": {"k1": 0.9, "k2": 0.8}, "B": {"k1": 0.6, "k2": 0.7}},
        {"k1": 1.0, "k2": 1.0},
        {"A": {"k1": 1.0, "k2": 1.0}, "B": {"k1": 1.0, "k2": 1.0}},
        {"A": 1.0, "B": 2.0},
        {"A": 0.0, "B": 0.0},
    )


def test_ricardian_two_by_two():
    q, demand, cost, budget, price = ricardian()
    result = comparative.shadow_prices(q, demand, cost, budget, price)
    assert result["status"] == "optimal"
    assert result["assignment"] == {"k1": {"A": 1}, "k2": {"B": 1}}
    assert result["objective"] == pytest.approx(1.6)
    assert result["lambda"]["A"] > 0
    ranks = comparative.comparative_ranks(
        q, cost, result["lambda"], price, assignment=result["assignment"]
    )
    assert ranks["k2"][0]["agent"] == "B"
    assert max(q, key=lambda agent: q[agent]["k2"]) == "A"


def test_reviewer_three_by_three():
    q = dict(
        A=dict(k1=0.9, k2=0.8, k3=0.7),
        B=dict(k1=0.6, k2=0.7, k3=0.65),
        C=dict(k1=0.5, k2=0.5, k3=0.6),
    )
    cost = {a: dict.fromkeys(q[a], 1) for a in q}
    result = comparative.shadow_prices(
        q, dict.fromkeys(q["A"], 1), cost, dict.fromkeys(q, 1), dict.fromkeys(q, 0)
    )
    assert result["assignment"] == {"k1": {"A": 1}, "k2": {"B": 1}, "k3": {"C": 1}}
    assert result["objective"] == pytest.approx(2.2)
    for a in q:
        if result["usage"][a] < 1 - 1e-9:
            assert result["lambda"][a] == 0
    # Include an actually slack row (the unit-budget example has none).
    result = comparative.shadow_prices(
        q, dict.fromkeys(q["A"], 1), cost, dict(A=1, B=1, C=2), dict.fromkeys(q, 0)
    )
    assert result["usage"]["C"] < 2
    assert result["lambda"]["C"] == 0


def test_seeded_random_primal_dual_certificates():
    rng = random.Random(676)
    saw_slack = saw_binding = False
    for _ in range(300):
        q = {a: {k: rng.uniform(0.1, 1) for k in range(3)} for a in range(3)}
        cost = {a: {k: rng.uniform(0.05, 2) for k in range(3)} for a in q}
        demand = {k: rng.uniform(0.1, 3) for k in range(3)}
        # A fractional witness serves ALL demand. Budgets allow that witness,
        # while the optimum can make several capacity rows bind simultaneously.
        shares = {k: [rng.uniform(0.1, 1) for a in q] for k in demand}
        budget = {
            a: sum(cost[a][k] * demand[k] * shares[k][a] / sum(shares[k]) for k in demand)
            * rng.uniform(1, 1.2)
            for a in q
        }
        price = {a: rng.uniform(0, 0.2) for a in q}
        value = {k: rng.uniform(0.5, 2) for k in demand}
        r = comparative.shadow_prices(q, demand, cost, budget, price, value=value)
        assert r["status"] == "optimal"
        primal = 0.0
        for a in q:
            usage = sum(cost[a][k] * r["assignment"][k].get(a, 0) for k in demand)
            slack = budget[a] - usage
            assert slack >= -1e-9
            if r["lambda"][a] > 1e-9:
                saw_binding = True
                assert abs(slack) <= 1e-9
            if slack > 1e-9:
                saw_slack = True
                assert r["lambda"][a] == 0
            for k in demand:
                net = q[a][k] * value[k] - price[a] * cost[a][k]
                assert r["lambda"][a] * cost[a][k] + r["mu"][k] >= net - 1e-9
                x = r["assignment"][k].get(a, 0)
                assert x >= 0
                primal += x * net
        for k in demand:
            assert sum(r["assignment"][k].values()) <= demand[k] + 1e-9
        dual = sum(budget[a] * r["lambda"][a] for a in q) + sum(
            demand[k] * r["mu"][k] for k in demand
        )
        assert abs(primal - dual) < 1e-7
        assert r["objective"] == pytest.approx(primal, abs=1e-9)
    assert saw_slack and saw_binding


def test_slack_capacity_equals_absolute():
    q, demand, cost, _, price = ricardian()
    result = comparative.shadow_prices(q, demand, cost, {"A": 100, "B": 100}, price)
    assert result["lambda"] == {"A": 0.0, "B": 0.0}
    ranks = comparative.comparative_ranks(q, cost, result["lambda"], price)
    for task in demand:
        assert [row["agent"] for row in ranks[task]] == sorted(q, key=lambda a: -q[a][task])


def test_unmeasured_budget_is_null_not_zero():
    q, demand, cost, budget, price = ricardian()
    budget["A"] = None
    result = comparative.shadow_prices(q, demand, cost, budget, price)
    assert result["status"] == "inputs_unmeasured"
    assert result["lambda"]["A"] is None
    assert "budget[A] unmeasured" in result["reasons"]
    assert all(value is None for value in result["assignment"].values())
    budget["A"] = 0
    measured = comparative.shadow_prices(q, demand, cost, budget, price)
    assert measured["status"] == "optimal"
    assert measured["lambda"]["A"] > 0


@pytest.mark.parametrize("field", ["quality", "demand", "cost", "budget", "price"])
@pytest.mark.parametrize("bad", [None, math.nan, math.inf, True])
def test_missing_or_nonfinite_inputs_never_become_zero(field, bad):
    q, demand, cost, budget, price = ricardian()
    if field == "quality":
        q["A"]["k1"] = bad
    elif field == "demand":
        demand["k1"] = bad
    elif field == "cost":
        cost["A"]["k1"] = bad
    elif field == "budget":
        budget["A"] = bad
    else:
        price["A"] = bad
    result = comparative.shadow_prices(q, demand, cost, budget, price)
    assert result["status"] == "inputs_unmeasured"
    assert result["lambda"] == {"A": None, "B": None}


def test_invalid_negative_budget():
    q, demand, cost, budget, price = ricardian()
    budget["A"] = -1
    assert (
        comparative.shadow_prices(q, demand, cost, budget, price)["status"] == "infeasible_inputs"
    )


def test_zero_capacity_is_feasible_with_unmet_demand():
    q, demand, cost, _, price = ricardian()
    r = comparative.shadow_prices(q, demand, cost, dict(A=0, B=0), price)
    assert r["status"] == "optimal"
    assert r["objective"] == 0
    assert r["unmet_demand"] == demand


def test_price_and_unequal_cost_change_scores():
    r = comparative.shadow_prices(
        dict(A=dict(k=0.9), B=dict(k=0.8)),
        dict(k=1),
        dict(A=dict(k=2), B=dict(k=1)),
        dict(A=5, B=5),
        dict(A=0.1, B=0),
    )
    assert r["assignment"] == {"k": {"B": 1}}
    assert r["lambda"] == dict(A=0, B=0)


def test_zero_burn_and_split_assignment():
    r = comparative.shadow_prices(
        dict(A=dict(k=0.9)), dict(k=1), dict(A=dict(k=0)), dict(A=0), dict(A=0)
    )
    assert r["status"] == "optimal"
    assert r["lambda"] == dict(A=0)
    r = comparative.shadow_prices(
        dict(A=dict(k=0.9), B=dict(k=0.8)),
        dict(k=1),
        dict(A=dict(k=1), B=dict(k=1)),
        dict(A=0.4, B=1),
        dict(A=0, B=0),
    )
    assert r["assignment"]["k"] == pytest.approx(dict(A=0.4, B=0.6))
