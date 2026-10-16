"""Regression for the rejected Xiao Lin demo; no external models or user data."""
import pytest

from model.factory import ModelError
from services.plan_foods import exclusions, preferred_foods, grounded_preference
from test_coach import conversation, send
from test_coach_reviews import reviews, understand, confirm
from test_meal_plans import request, plans
from test_foundation import application, client
from test_knowledge import library


@pytest.mark.parametrize("text", ["清淡口味，喜欢米饭", "口味清淡；主食优先米饭", "我喜欢吃米饭"])
def test_positive_profile_preference_does_not_exclude_rice(text):
    profile = {"preferences": text, "food_allergies": "牛肉；鸡肉"}
    assert exclusions(profile) == ["beef", "chicken"]
    assert preferred_foods(profile) == ["brown-rice", "rice"]


@pytest.mark.parametrize("text", ["不喜欢米饭", "不吃米饭", "米饭过敏", "喜欢米饭但不能吃", "喜欢火星肉", "喜欢米饭不吃牛肉"])
def test_positive_parser_cannot_swallow_negative_or_unknown_tails(text):
    profile = {"preferences": text, "food_allergies": ""}
    assert not preferred_foods(profile)
    assert not grounded_preference("喜欢米饭", text)
    if text in {"不喜欢米饭", "不吃米饭", "米饭过敏"}:
        assert {"rice", "brown-rice"} <= set(exclusions(profile))
    else:
        with pytest.raises(ModelError, match="暂不能完整识别"):
            exclusions(profile)


def test_allergy_always_overrides_positive_preference():
    profile = {"preferences": "喜欢米饭", "food_allergies": "米饭过敏"}
    assert {"rice", "brown-rice", "rice-noodles"} <= set(exclusions(profile))


@pytest.mark.parametrize("phrase", ["喜欢米饭", "主食优先米饭"])
@pytest.mark.parametrize("model_phrase", ["米饭", "优先米饭", "米饭优先", "主食以米饭为主"])
def test_conversation_preference_reaches_meal_model_without_becoming_avoid(reviews, phrase, model_phrase):
    client, app, model, meals = reviews
    client.put("/api/profile", json={"food_allergies": "牛肉"})
    data = send(client, conversation(client), f"安排午餐和晚餐，{phrase}。").json()
    def edit(_, out):
        assert _["profile"] == {}, "intent extraction must not copy profile allergies into new notes"
        assert phrase in _["soft_preferences"]
        out["notes"][0].update(preferences=[model_phrase])
        out["meal_types"] = ["lunch", "dinner"]
    model.edit = edit
    response = understand(client, data)
    assert response.status_code == 200, response.text
    ready = response.json()
    response = confirm(client, ready)
    assert response.status_code == 200, response.text
    applied = response.json()
    assert not applied["pending"]
    assert applied["constraints"]["preferences"] == ["优先米饭"]
    assert applied["constraints"]["food_avoid"] == []
    response = client.post("/api/meal-plans", json=request(coach_id=data["id"], coach_version=data["version"]))
    assert response.status_code == 200, response.text
    assert meals.messages[-1]["preferred_foods"] == ["brown-rice", "rice"]
    assert "beef" not in {food["id"] for food in meals.messages[-1]["allowed_foods"]}
    assert client.get("/api/profile").json()["preferences"] == ""


@pytest.mark.parametrize("value", ["喜欢米饭", "米饭"])
def test_hallucinated_positive_preference_is_rejected(reviews, value):
    client, _, model, _ = reviews
    data = send(client, conversation(client), "安排午餐，我不喜欢米饭").json()
    model.edit = lambda _, out: out["notes"][0].update(preferences=[value])
    assert understand(client, data).status_code != 200
