import math

import pytest

import comparative


def ricardian():
    # The issue's exact qualities; A can serve only one of two unit-demand classes.
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
    assert result["status"] == "converged"
    assert result["assignment"] == {"k1": "A", "k2": "B"}
    assert result["lambda"]["A"] > 0
    ranks = comparative.comparative_ranks(q, cost, result["lambda"], price)
    assert ranks["k2"][0]["agent"] == "B"
    assert max(q, key=lambda agent: q[agent]["k2"]) == "A"


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
    ranks = comparative.comparative_ranks(q, cost, result["lambda"], price)
    assert all(row["comparative_score"] is None for rows in ranks.values() for row in rows)
    budget["A"] = 0.0
    measured = comparative.shadow_prices(q, demand, cost, budget, price)
    assert measured["status"] == "converged"
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


def test_price_adjustment_is_bounded_and_does_not_claim_infeasibility_is_optimal():
    q, demand, cost, _, price = ricardian()
    result = comparative.shadow_prices(q, demand, cost, {"A": 0, "B": 0}, price, max_iter=3)
    assert result["status"] == "max_iter"
    assert result["iterations"] == 3


def test_price_and_unequal_cost_change_scores():
    q = {"A": {"k": 0.9}, "B": {"k": 0.8}}
    result = comparative.shadow_prices(
        q, {"k": 1}, {"A": {"k": 2}, "B": {"k": 1}}, {"A": 5, "B": 5}, {"A": 0.1, "B": 0}
    )
    assert result["assignment"] == {"k": "B"}
    assert result["lambda"] == {"A": 0.0, "B": 0.0}


def test_zero_burn_is_measured_and_no_alternative_is_bounded():
    result = comparative.shadow_prices(
        {"A": {"k": 0.9}}, {"k": 1}, {"A": {"k": 0}}, {"A": 0}, {"A": 0}
    )
    assert result["status"] == "converged"
    result = comparative.shadow_prices(
        {"A": {"k": 0.9}}, {"k": 1}, {"A": {"k": 1}}, {"A": 0}, {"A": 0}
    )
    assert result["status"] == "max_iter"
    assert result["reasons"]
