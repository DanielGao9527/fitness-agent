import copy
import sqlite3
from datetime import date

import pytest

from model.factory import ModelError
from rag.rag_service import EmptyKnowledgeRetriever, KnowledgeUnavailable, LocalKnowledgeRetriever
from services.meal_plans import MealPlanService, REQUIRED as MEAL_IDS
from services.training_plans import TrainingPlanService, REQUIRED as TRAINING_IDS
from test_foundation import application, client
from test_knowledge import TODAY, library, mutate
from test_meal_plans import accept, generate, plans


@pytest.mark.parametrize("service,ids,topic", [
    (MealPlanService, MEAL_IDS, "nutrition"), (TrainingPlanService, TRAINING_IDS, "training"),
])
def test_required_evidence_not_limited_to_first_twenty(library, service, ids, topic):
    expected = service(None, None, library).evidence()

    def crowd(data):
        source = copy.deepcopy(next(item for item in data["sources"] if item["topic"] == topic))
        source["id"] = "aaa-unrelated-fixture"
        section = source["sections"][0]
        source["sections"] = [{**section, "id": f"section-{i:02d}"} for i in range(25)]
        data["sources"].append(source)

    mutate(library, crowd)
    assert not set(ids) & {hit.chunk_id for hit in library.search("", 20, topic=topic)}
    assert service(None, None, library).evidence() == expected
    restarted = LocalKnowledgeRetriever(library.source_path, library.index_path)
    assert service(None, None, restarted).evidence() == expected


def test_exact_lookup_order_missing_topic_and_no_fuzzy_fallback(library):
    ids = ("fda-allergy-label:cross-contact", "missing:section", "phe-eatwell:balance")
    assert [hit.chunk_id for hit in library.get_chunks(ids, topic="nutrition", today=TODAY)] == [ids[0], ids[2]]
    assert library.get_chunks(ids, topic="training", today=TODAY) == []
    assert library.get_chunks([], today=TODAY) == []
    assert library.get_chunks(["fda-allergy-label:ingredient"], today=TODAY) == []
    assert library.get_chunks(ids, today=date(2027, 3, 17)) == []
    assert library.get_chunks(ids, today=date(2026, 9, 16)) == []
    assert library.get_chunks([ids[0]], today=date(2026, 9, 18)) == []
    assert library.get_chunks([ids[0]], today=date(2027, 3, 16))


@pytest.mark.parametrize("ids", ["phe-eatwell:balance", [None], ["x"], ["a:b' OR 1=1"],
    ["a:b", "a:b"], [f"a:b-{i}" for i in range(21)], ["a" * 81 + ":b"]])
def test_exact_lookup_rejects_invalid_ids(library, ids):
    with pytest.raises(ValueError):
        library.get_chunks(ids)
    assert not library.index_path.exists()


@pytest.mark.parametrize("action", ["withdraw", "remove", "empty", "expire", "future"])
def test_required_source_loss_blocks_plans(library, action):
    MealPlanService(None, None, library).evidence()

    def change(data):
        source = next(item for item in data["sources"] if item["id"] == "fda-allergy-label")
        if action == "withdraw":
            source["status"] = "withdrawn"
        elif action == "remove":
            data["sources"].remove(source)
        elif action == "empty":
            data["sources"].clear()
        elif action == "expire":
            source.update(reviewed_on="2025-10-01", review_due="2026-03-01")
        else:
            source.update(reviewed_on="2099-01-01", review_due="2099-06-01")

    mutate(library, change)
    with pytest.raises(ModelError) as error:
        MealPlanService(None, None, library).evidence()
    assert error.value.code == "PLAN_EVIDENCE_MISSING"
    assert not library.get_chunks(["fda-allergy-label:ingredients"], today=TODAY)


def test_exact_lookup_does_not_use_stale_or_corrupt_cache(library):
    library.get_chunks(MEAL_IDS)
    library.source_path.write_text("invalid", encoding="utf-8")
    with pytest.raises(KnowledgeUnavailable):
        library.get_chunks(MEAL_IDS)
    library.source_path.unlink()
    with pytest.raises(KnowledgeUnavailable):
        library.get_chunks(MEAL_IDS)


def test_exact_lookup_corrupt_index_fails_closed(library):
    library.sync()
    with sqlite3.connect(library.index_path) as connection:
        connection.execute("UPDATE chunks SET payload='broken' WHERE id=?", (MEAL_IDS[0],))
    with pytest.raises(KnowledgeUnavailable):
        library.get_chunks(MEAL_IDS)


@pytest.mark.parametrize("service,code", [(MealPlanService, "PLAN_EVIDENCE_MISSING"),
    (TrainingPlanService, "TRAINING_EVIDENCE")])
def test_empty_retriever_has_clear_unavailable_state(service, code):
    retriever = EmptyKnowledgeRetriever()
    assert not retriever.search("", topic="training", today=TODAY)
    with pytest.raises(ModelError) as error:
        service(None, None, retriever).evidence()
    assert error.value.code == code


@pytest.mark.parametrize("when", ["before", "during", "after"])
def test_allergy_evidence_withdrawal_never_publishes_or_accepts_stale_plan(plans, when):
    client, _, model, library = plans
    def withdraw():
        mutate(library, lambda data: next(item for item in data["sources"]
            if item["id"] == "fda-allergy-label").update(status="withdrawn"))

    if when == "before":
        withdraw()
        assert generate(client).status_code == 503
        assert not model.messages
    elif when == "during":
        model.on_call = withdraw
        assert generate(client).status_code == 503
    else:
        draft = generate(client).json()
        withdraw()
        assert accept(client, draft).status_code == 503
