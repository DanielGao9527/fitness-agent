"""Offline, frozen-response comparison; no provider, credential or private database access."""
import argparse
import hashlib
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import ROOT
from model.deadlines import Deadline
from model.factory import ModelError
from rag.rag_service import LocalKnowledgeRetriever, RetrievalSnapshot
from services.knowledge_answers import answer_question
from services.knowledge_policy import KnowledgePolicy, POLICY_VERSION

DEFAULT_SUITE = ROOT / "evals/retrieval_loop/v1"
DEFAULT_SOURCE = ROOT / "evals/corpora/reviewed-training-notes-2026-09-23.2.json"


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Page(StrictInput):
    ids: list[str] = Field(max_length=6)
    elapsed_ms: int = Field(default=0, ge=0, le=120000)
    empty_corpus: bool = False


class Expected(StrictInput):
    status: Literal["answered", "insufficient", "personal_scope", "error"]
    source_ids: list[str] = Field(default_factory=list)
    stop_reason: str


class Case(StrictInput):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    question: str = Field(min_length=1, max_length=1000)
    pages: list[Page] = Field(min_length=1, max_length=2)
    outputs: list[str] = Field(max_length=2)
    judgment_elapsed_ms: list[int] = Field(default_factory=list, max_length=2)
    withdraw_after_judgments: int = Field(default=0, ge=0, le=2)
    withdrawn_ids: list[str] = Field(default_factory=list)
    expected: dict[Literal["single", "bounded"], Expected]


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class Records:
    def get_profile(self):
        return {}


def load_suite(directory, source):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    raw = (directory / "cases.jsonl").read_bytes()
    if (manifest["version"] != "knowledge-loop-replay-v1" or manifest["synthetic_public"] is not True
            or manifest["cases_sha256"] != hashlib.sha256(raw).hexdigest()
            or manifest["corpus_sha256"] != hashlib.sha256(source.read_bytes()).hexdigest()):
        raise ValueError("Frozen replay suite or corpus mismatch")
    cases = [Case.model_validate_json(line) for line in raw.splitlines() if line.strip()]
    if len(cases) != manifest["cases"] or len({case.id for case in cases}) != len(cases):
        raise ValueError("Replay case count or IDs mismatch")
    for case in cases:
        if set(case.expected) != {"single", "bounded"} or any(value < 0 or value > 120000 for value in case.judgment_elapsed_ms):
            raise ValueError("Replay contract incomplete")
    return manifest, cases


def replay_case(case, base, today, rounds):
    clock, trace = Clock(), {}
    searches, judgments = 0, 0

    class Retriever:
        def search_snapshot(self, query, limit=6):
            nonlocal searches
            if searches >= len(case.pages):
                raise ValueError("Replay lacks a retrieval response")
            page = case.pages[searches]
            searches += 1
            clock.value += page.elapsed_ms / 1000
            hits = base.get_chunks(page.ids, today=today)
            offered = {hit.chunk_id: hit for hit in hits}
            if set(offered) != set(page.ids) or len(set(page.ids)) != len(page.ids):
                raise ValueError("Replay contains unavailable or duplicate public IDs")
            return RetrievalSnapshot([offered[key] for key in page.ids], {
                "mode": "hybrid", "requested_mode": "hybrid", "embedding_called": True,
                "reason": "empty_corpus" if page.empty_corpus else ""})

        def get_chunks(self, ids):
            withdrawn = (case.withdrawn_ids if case.withdraw_after_judgments
                         and judgments >= case.withdraw_after_judgments else [])
            return base.get_chunks([key for key in ids if key not in withdrawn], today=today)

    class Model:
        def generate(self, *, system_prompt, message):
            nonlocal judgments
            if json.loads(message)["question"] != case.question:
                raise ValueError("Policy changed the original judgment question")
            if judgments >= len(case.outputs):
                raise ValueError("Replay lacks a judgment response")
            raw = case.outputs[judgments]
            if judgments < len(case.judgment_elapsed_ms):
                clock.value += case.judgment_elapsed_ms[judgments] / 1000
            judgments += 1
            return raw

    try:
        result = answer_question(case.question, Retriever(), Records(), Model, "fixed-responses",
            policy=KnowledgePolicy(max_rounds=rounds), deadline=Deadline(clock=clock), audit=trace)
        actual = {"status": result["status"], "source_ids": [s["chunk_id"] for s in result["sources"]],
                  "stop_reason": trace["stop_reason"]}
    except ModelError:
        actual = {"status": "error", "source_ids": [], "stop_reason": trace["stop_reason"]}
    name = "single" if rounds == 1 else "bounded"
    return {"case_id": case.id, "policy": name, **actual,
            "contract_passed": actual == case.expected[name].model_dump(),
            "simulated_calls": trace["calls"], "trace": trace}


def replay_suite(directory=DEFAULT_SUITE, *, source=DEFAULT_SOURCE):
    directory, source = Path(directory), Path(source)
    manifest, cases = load_suite(directory, source)
    today = date.fromisoformat(manifest["source_date"])
    with tempfile.TemporaryDirectory(prefix="fitness-knowledge-replay-") as temporary:
        base = LocalKnowledgeRetriever(source, Path(temporary) / "public-index.sqlite3", tokenizer="jieba")
        rows = [replay_case(case, base, today, rounds) for case in cases for rounds in (1, 2)]
    pairs = [(rows[index], rows[index+1]) for index in range(0, len(rows), 2)]
    files = ("services/knowledge_answers.py", "services/knowledge_policy.py", "model/deadlines.py",
             "prompts/knowledge_answer.md", "scripts/replay_knowledge_loop.py")
    return {"suite": manifest, "policy_version": POLICY_VERSION,
        "evaluation_corpus": "frozen_historical_v1_not_current_runtime_quality",
        "source_hashes": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files},
        "real_provider_calls": 0, "uses_private_database": False, "not_model_quality": True,
        "phase_timings_are_virtual": True,
        "contracts": {name: {"passed": sum(row["contract_passed"] for row in rows if row["policy"] == name),
                             "total": len(cases)} for name in ("single", "bounded")},
        "transitions": {
            "additional_answers": sum(left["status"] != "answered" and right["status"] == "answered" for left,right in pairs),
            "fewer_answers": sum(left["status"] == "answered" and right["status"] != "answered" for left,right in pairs),
            "changed_outcome": sum((left["status"], left["source_ids"], left["stop_reason"]) !=
                                   (right["status"], right["source_ids"], right["stop_reason"]) for left,right in pairs)},
        "cases": rows}


def markdown(result):
    lines = ["# 有界检索：固定响应离线回放", "",
        "这份自建合成流程集不是独立语义评测。模型输出和候选均已固定，只验证策略、引用和停止契约；更多 answered 不代表真实问答准确率提高。",
        "阶段时间由虚拟时钟注入，只用于截止时间测试，不是线上延迟。供应商调用为 0，不读取私人业务库。", "",
        "| 策略 | 流程契约通过 |", "| --- | --- |"]
    for name, value in result["contracts"].items():
        lines.append(f"| {name} | {value['passed']}/{value['total']} |")
    lines += ["", "逐例保留引用 ID、停止原因、模拟调用和阶段时间，见同目录 report.json。",
        "零命中恢复会增加一次模型判断的潜在成本；第二次仍无候选则不再判断。真实问题、真实输出及单轮/两轮质量和费用对照仍待有预算的独立验证。", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = replay_suite(args.suite)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (args.output / "REPORT.md").write_text(markdown(result),encoding="utf-8")
    print(json.dumps({"contracts": result["contracts"], "real_provider_calls": 0, "not_model_quality": True}))
    return 0 if all(v["passed"] == v["total"] for v in result["contracts"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
