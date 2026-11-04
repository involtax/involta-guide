import pytest

from involta_guide.domain.rules import FACTORS, restaurant_rank_key, restaurant_score, score


@pytest.mark.parametrize("value", [None, True, False, "8", 8.0, -1, 11])
def test_incomplete_or_invalid_criterion_has_no_total(value):
    assert score({**dict.fromkeys(FACTORS, 8), FACTORS[0]: value}) is None


@pytest.mark.parametrize(
    "scores", [{}, dict.fromkeys(FACTORS[:-1], 9), {**dict.fromkeys(FACTORS, 9), "extra": 9}]
)
def test_exactly_eight_known_criteria_required(scores):
    assert score(scores) is None


@pytest.mark.parametrize("ranking", [None, {}, [], "invalid", {"scores": None}, {"totalScore": 10}])
def test_missing_scores_never_inherit_an_old_total(ranking):
    assert restaurant_score({"ranking": ranking}) is None


def test_zero_is_a_real_score_and_total_is_always_derived():
    assert (
        restaurant_score({"ranking": {"scores": dict.fromkeys(FACTORS, 0), "totalScore": 10}}) == 0
    )
    assert (
        restaurant_score({"ranking": {"scores": dict.fromkeys(FACTORS, 8), "totalScore": 1}}) == 8
    )


def test_one_order_ignores_names_stars_reviews_and_stored_totals():
    def card(number, value, **other):
        return {
            "number": number,
            "active": 1,
            "ranking": {"scores": dict.fromkeys(FACTORS, value)},
            **other,
        }

    cards = [
        card(1, None, position=0, totalScore=10, alex_star=True),
        card(2, 0),
        card(3, 8, name="Я", review_scores={"rating": 1}),
        card(4, 8, name="А", alex_star=True, review_scores={"rating": 10}),
        card(5, 10),
        card(6, 10, active=0),
    ]
    assert [r["number"] for r in sorted(cards, key=restaurant_rank_key)] == [5, 3, 4, 2, 1, 6]
