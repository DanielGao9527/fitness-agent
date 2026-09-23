from config import ROOT
from agent.tools import FitnessTools
from model.factory import ChatModel
from rag.rag_service import KnowledgeRetriever
from schemas import AgentRequest


class FitnessAgent:
    """Scaffold boundary, not yet a LangChain ReAct execution loop."""

    def __init__(self, tools: FitnessTools, model: ChatModel, retriever: KnowledgeRetriever):
        self.tools = tools
        self.model = model
        self.retriever = retriever

    def prepare_context(self, request: AgentRequest):
        context = self.tools.read_day(request.day)
        sources = self.retriever.search(request.message)
        context["knowledge"] = [source.model_dump() for source in sources]
        profile = context["profile"]
        context["constraints"] = {key: profile.get(key, "") for key in ("food_allergies", "preferences")}
        return context

    def respond(self, request: AgentRequest):
        context = self.prepare_context(request)
        answer = self.model.generate(
            system_prompt=(ROOT / "prompts" / "assistant.md").read_text(encoding="utf-8"),
            message=request.message, context=context,
        )
        return {"answer": answer, "sources": context["knowledge"], "saved": False}
