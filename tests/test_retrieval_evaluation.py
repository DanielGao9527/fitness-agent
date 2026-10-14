"""Metric, dataset and offline evaluation safeguards; no real embedding requests."""
import json
import math
from dataclasses import replace

import pytest

from config import Settings
from scripts import build_knowledge_vectors as builder
from scripts import evaluate_retrieval as evaluation
from test_hybrid_retrieval import TODAY, FakeEmbedding, setup_retrieval


def case(identifier="q1", **changes):
    return {"id": identifier, "question": f"Question {identifier}", "group_id": "group-a",
            "split": "dev", "answerable": True, "relevant_chunk_ids": ["a:one"], **changes}


def write_cases(path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return path


def test_metrics_multiple_relevant_documents_and_rank_discount():
    value = evaluation.metrics(["noise", "a", "irrelevant", "b", "other", "c"], ["a", "b", "missing"])
    assert value["recall@3"] == pytest.approx(1 / 3)
    assert value["recall@6"] == value["recall@10"] == value["recall@20"] == pytest.approx(2 / 3)
    assert value["hit@3"] == value["hit@6"] == 1
    assert value["mrr@10"] == 0.5
    dcg = 1 / math.log2(3) + 1 / math.log2(5)
    ideal = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert value["ndcg@10"] == pytest.approx(dcg / ideal)


def test_metrics_deduplicate_results_before_ranking():
    assert evaluation.metrics(["noise", "a", "a", "b", "b"], ["a", "b"]) == evaluation.metrics(
        ["noise", "a", "b"], ["a", "a", "b"])


def test_metrics_cutoffs_do_not_reward_a_match_below_rank_ten():
    value = evaluation.metrics([f"noise-{i}" for i in range(10)] + ["answer"], ["answer"])
    assert value["recall@10"] == value["mrr@10"] == value["ndcg@10"] == 0
    assert value["recall@20"] == 1


def test_metrics_separate_unanswerable_questions_from_zero_recall():
    assert evaluation.metrics(["anything"], []) is None
    assert evaluation.metrics([], []) is None
    assert all(value == 0 for value in evaluation.metrics([], ["answer"]).values())


def test_load_cases_accepts_isolated_groups_and_unanswerable_cases(tmp_path):
    rows = [case(), case("q2"), case("q3", split="test", group_id="group-b", relevant_chunk_ids=["b:two"]),
            case("q4", split="test", group_id="unanswerable", answerable=False, relevant_chunk_ids=[])]
    path = write_cases(tmp_path / "qa.jsonl", rows)
    assert evaluation.load_cases(path, {"a:one", "b:two"}) == rows


@pytest.mark.parametrize("rows", [
    [case(), case(question="A second question")],
    [case(), case("q2", question="Question q1")],
    [case(relevant_chunk_ids=["unknown:chunk"])],
    [case(split="training")],
    [case(answerable=False)],
    [case(answerable=True, relevant_chunk_ids=[])],
    [case(answerable="true")],
    [case(), case("q2", split="test")],
    [case(), case("q2", split="test", group_id="different-group")],
    [case(question="Question 1?"), case("q2", question="Ｑｕｅｓｔｉｏｎ１！")],
    [case(question="   ")],
    [case("")],
    [case(group_id="   ")],
    [case(group_id=["not-a-string"])],
    [case(group_id=123)],
    [case(relevant_chunk_ids=["a:one", "a:one"])],
    [],
    [[]],
])
def test_load_cases_rejects_ambiguous_or_leaking_labels(tmp_path, rows):
    path = write_cases(tmp_path / "invalid.jsonl", rows)
    with pytest.raises(ValueError):
        evaluation.load_cases(path, {"a:one"})


def test_query_cache_replays_without_embedding_and_is_bound_to_identity(tmp_path):
    rows = [case(), case("q2")]
    provider = FakeEmbedding()
    vectors, first = evaluation.encode_queries(rows, provider, tmp_path, live=True)
    assert first["calls"] == 1 and not first["cache_hit"]
    provider.error = AssertionError("Cached replay must not contact a provider")
    cached, replay = evaluation.encode_queries(rows, provider, tmp_path, live=False)
    assert cached == vectors and replay["cache_hit"] and replay["calls"] == 0
    assert provider.calls == 1
    for different in [FakeEmbedding("different-model"), FakeEmbedding()]:
        if different.identity == provider.identity:
            different.dimensions = 3
        with pytest.raises(ValueError, match="missing"):
            evaluation.encode_queries(rows, different, tmp_path, live=False)
        assert different.calls == 0
    for changed_rows in [list(reversed(rows)), [case(question="Changed question"), case("q2")]]:
        with pytest.raises(ValueError, match="missing"):
            evaluation.encode_queries(changed_rows, provider, tmp_path, live=False)
    assert provider.calls == 1


@pytest.mark.parametrize("change", ["manifest", "count", "dimensions", "zero", "nonfinite"])
def test_corrupt_query_cache_is_rejected_without_a_retry(tmp_path, change):
    rows = [case(), case("q2")]
    provider = FakeEmbedding()
    evaluation.encode_queries(rows, provider, tmp_path, live=True)
    path = next(tmp_path.glob("public-queries-*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    if change == "manifest":
        data["manifest"]["identity"] = "wrong-model"
    elif change == "count":
        data["vectors"].pop()
    elif change == "dimensions":
        data["vectors"][0] = [1.0, 0.0, 0.0]
    elif change == "zero":
        data["vectors"][0] = [0.0, 0.0]
    else:
        data["vectors"][0] = [float("nan"), 0.0]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        evaluation.encode_queries(rows, provider, tmp_path, live=False)
    assert provider.calls == 1


def test_wrong_provider_result_count_cannot_publish_query_cache(tmp_path):
    class ShortProvider(FakeEmbedding):
        def embed(self, texts, *, text_type, before_call=None):
            return super().embed(texts[:1], text_type=text_type, before_call=before_call)

    provider = ShortProvider()
    with pytest.raises(ValueError):
        evaluation.encode_queries([case(), case("q2")], provider, tmp_path, live=True)
    assert not list(tmp_path.glob("public-queries-*"))


def test_query_replay_cannot_be_used_as_a_document_encoder():
    provider = FakeEmbedding()
    replay = evaluation.CachedQueries(provider, {"known": [1.0, 0.0]})
    assert replay.identity == provider.identity and replay.dimensions == provider.dimensions
    assert replay.embed(["known"], text_type="query") == [[1.0, 0.0]]
    with pytest.raises(ValueError):
        replay.embed(["known"], text_type="document")
    with pytest.raises(KeyError):
        replay.embed(["unknown"], text_type="query")
    assert provider.calls == 0


def cli_settings(tmp_path, setup_retrieval):
    lexical, _, store = setup_retrieval
    return Settings(database_path=tmp_path / "private.sqlite3", knowledge_source_path=lexical.source_path,
                    knowledge_index_path=lexical.index_path, knowledge_vector_path=store.path)


def test_vector_build_dry_run_does_not_construct_provider_or_open_private_db(
        tmp_path, setup_retrieval, monkeypatch, capsys):
    settings = cli_settings(tmp_path, setup_retrieval)
    monkeypatch.setattr(builder.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(builder, "QwenEmbeddingProvider", lambda *_args, **_kwargs: pytest.fail("Unexpected provider"))
    monkeypatch.setattr(builder.sys, "argv", ["build_knowledge_vectors.py", "--date", TODAY.isoformat()])
    builder.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "dry_run" and result["chunks"] == 2
    assert not settings.database_path.exists() and not settings.knowledge_vector_path.exists()


@pytest.mark.parametrize("collision", ["index_database", "index_source", "source_database",
                                        "output_database", "output_index", "output_source"])
def test_vector_build_rejects_path_collisions_before_opening_files(
        tmp_path, setup_retrieval, monkeypatch, collision):
    settings = cli_settings(tmp_path, setup_retrieval)
    if collision == "index_database":
        settings = replace(settings, knowledge_index_path=settings.database_path)
    elif collision == "index_source":
        settings = replace(settings, knowledge_index_path=settings.knowledge_source_path)
    elif collision == "source_database":
        settings = replace(settings, database_path=settings.knowledge_source_path)
    elif collision == "output_database":
        settings = replace(settings, knowledge_vector_path=settings.database_path)
    elif collision == "output_index":
        settings = replace(settings, knowledge_vector_path=settings.knowledge_index_path)
    else:
        settings = replace(settings, knowledge_vector_path=settings.knowledge_source_path)
    monkeypatch.setattr(builder.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(builder, "LocalKnowledgeRetriever", lambda *_args, **_kwargs: pytest.fail("Opened unsafe path"))
    monkeypatch.setattr(builder.sys, "argv", ["build_knowledge_vectors.py", "--date", TODAY.isoformat()])
    with pytest.raises(SystemExit) as error:
        builder.main()
    assert error.value.code == 2


def test_lexical_only_evaluation_keeps_holdout_and_no_answer_counts_separate(
        tmp_path, setup_retrieval, monkeypatch, capsys):
    settings = cli_settings(tmp_path, setup_retrieval)
    rows = [case("dev", question="营养标签 两份", relevant_chunk_ids=["fda-label:servings"]),
            case("test", question="蛋白质来源", group_id="group-b", split="test", relevant_chunk_ids=["fda-label:protein"]),
            case("none", question="火星飞船引擎", group_id="group-c", split="test", answerable=False, relevant_chunk_ids=[])]
    qa = write_cases(tmp_path / "qa.jsonl", rows)
    output = tmp_path / "report"
    monkeypatch.setattr(evaluation.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(evaluation, "QwenEmbeddingProvider", lambda *_args, **_kwargs: pytest.fail("Unexpected provider"))
    monkeypatch.setattr(evaluation.sys, "argv", ["evaluate_retrieval.py", "--qa", str(qa), "--output", str(output),
                                                "--date", TODAY.isoformat(), "--lexical-only", "--split", "all"])
    evaluation.main()
    emitted = json.loads(capsys.readouterr().out)
    report = json.loads((output / "results-lexical-all.json").read_text(encoding="utf-8"))
    assert emitted["usage"]["calls"] == 0 and report["model"] is None
    assert report["query_count"] == 3 and report["corpus_count"] == 2
    assert report["groups"]["dev"]["A_bigram"]["answerable_queries"] == 1
    assert report["groups"]["test"]["A_bigram"]["answerable_queries"] == 1
    assert report["groups"]["test"]["A_bigram"]["no_answer_queries"] == 1
    assert len(report["runs"]) == 6
    assert all(run["metrics"] is None for run in report["runs"] if run["query_id"] == "none")
    assert not settings.database_path.exists()
