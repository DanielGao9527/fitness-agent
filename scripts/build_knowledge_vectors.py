"""Explicit, bounded encoding of reviewed public knowledge; never opens personal DB."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from services.business_time import business_today
from model.embeddings import EmbeddingError, QwenEmbeddingProvider
from rag.rag_service import KnowledgeUnavailable, LocalKnowledgeRetriever
from rag.vector_store import VectorCacheUnavailable, VectorStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow bounded paid embedding requests")
    parser.add_argument("--date", type=date.fromisoformat, default=business_today())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-calls", type=int, default=50)
    parser.add_argument("--max-input-chars", type=int, default=500000)
    args = parser.parse_args()
    if args.max_calls < 1 or args.max_input_chars < 1:
        parser.error("Positive call and input budgets are required")
    settings = Settings.from_env()
    index_path = settings.knowledge_index_path or settings.database_path.with_name("knowledge.sqlite3")
    output = args.output or settings.knowledge_vector_path or index_path.with_name("knowledge-vectors.sqlite3")
    paths = [path.resolve() for path in (settings.database_path, settings.knowledge_source_path, index_path, output)]
    if len(set(paths)) != len(paths):
        parser.error("Use separate paths for personal data, public source, lexical index and vector cache")
    lexical = LocalKnowledgeRetriever(settings.knowledge_source_path, index_path,
                                      tokenizer=settings.retrieval_tokenizer)
    documents = lexical.export_documents(today=args.date)
    if not args.live:
        print(json.dumps({"status": "dry_run", "chunks": len(documents),
                          "input_characters": sum(len(doc.text) for doc in documents),
                          "model": settings.embedding_model, "dimensions": settings.embedding_dimensions,
                          "message": "Add --live to permit bounded cloud encoding; no personal database was opened."}))
        return
    provider = QwenEmbeddingProvider(settings, max_calls=args.max_calls, max_input_chars=args.max_input_chars)
    result = VectorStore(output).build(lexical, provider, today=args.date,
                                       batch_size=settings.embedding_batch_size)
    print(json.dumps({"status": "built", "output": str(output.resolve()), **result}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (EmbeddingError, KnowledgeUnavailable, VectorCacheUnavailable) as error:
        print(json.dumps({"status": "failed", "code": getattr(error, "code", getattr(error, "reason", "knowledge_unavailable"))}))
        sys.exit(1)
