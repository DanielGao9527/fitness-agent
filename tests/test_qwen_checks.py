import sys

from schemas import MealDraft
from scripts.check_qwen import check_result, main


def test_live_check_requires_explicit_opt_in(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_qwen.py"])
    monkeypatch.setattr("scripts.check_qwen.create_meal_text_model", lambda settings: (_ for _ in ()).throw(AssertionError("No network")))
    assert main() == 0
    assert "No requests made" in capsys.readouterr().out


def test_sample_matching_does_not_accept_wrong_grams_or_extra_foods():
    draft = MealDraft(input_type="text", items=[{"name":"熟米饭","grams":100}])
    assert check_result(draft, [("米饭",100)])
    assert not check_result(draft, [("米饭",200)])
    assert not check_result(draft, [])


def test_missing_portion_requires_a_question():
    draft = MealDraft(input_type="text", items=[{"name":"牛奶"}])
    assert not check_result(draft, [("牛奶",None)])
    draft.questions = ["请补充克数。"]
    assert check_result(draft, [("牛奶",None)])
    assert not check_result(draft, [("牛奶",250)])


def test_empty_consumption_sample():
    assert check_result(MealDraft(input_type="text", items=[]), [])
    assert not check_result(MealDraft(input_type="text", items=[{"name":"鸡肉"}]), [])
    planned = MealDraft(input_type="text", items=[], questions=["晚餐计划吃多少？"])
    assert not check_result(planned, [], "进食后")
    planned.questions = ["目前没有已摄入食物，进食后再记录。"]
    assert check_result(planned, [], "进食后")
