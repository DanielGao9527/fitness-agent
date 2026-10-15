"""Plan or explicitly budget public query encoding for the fixed release gate. No document encoding."""
import argparse
import json
import math
import sys
import sqlite3
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Settings
from model.embeddings import QwenEmbeddingProvider
from model.factory import ModelError
from rag.rag_service import LocalKnowledgeRetriever
from scripts.evaluate_retrieval import load_cases, encode_queries
from services import knowledge_quality
from services.backups import readonly, inspect_connection
from contextlib import closing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--max-calls', type=int)
    parser.add_argument('--max-input-chars', type=int)
    parser.add_argument('--acknowledge-previous-attempt', action='store_true')
    args = parser.parse_args(argv)
    if not args.database.is_absolute() or args.live and (not args.max_calls or not args.max_input_chars or args.max_calls < 1 or args.max_input_chars < 1):
        parser.error('Use an existing absolute database path; --live requires explicit positive call and character budgets')
    receipt = None
    try:
        with closing(readonly(args.database)) as connection:
            if inspect_connection(connection)['schema'] != 22:
                raise ValueError('Current schema v22 required')
        settings = Settings.from_env()
        protocol, protocol_hash, dataset = knowledge_quality.protocol()
        # Public source parsing only; never initializes or reads private business rows.
        from rag.catalog import Corpus
        corpus = Corpus.model_validate_json(settings.knowledge_source_path.read_bytes())
        known = {source.id+':'+section.id for source in corpus.sources for section in source.sections}
        rows = [row for row in load_cases(dataset, known) if row['split'] == 'test']
        provider = QwenEmbeddingProvider(settings, max_calls=args.max_calls, max_input_chars=args.max_input_chars)
        directory = args.database.resolve().parent/'release-evaluation'
        if directory.is_symlink() or directory.resolve().parent != args.database.resolve().parent:
            raise ValueError('Query cache must stay beside the private database')
        cache = knowledge_quality.cache_path(directory, provider, rows)
        if cache.is_symlink():
            raise ValueError('Symlink query cache is not accepted')
        summary = {'query_count':len(rows), 'input_chars':sum(len(row['question']) for row in rows),
            'planned_batches':math.ceil(len(rows)/settings.embedding_batch_size), 'protocol_sha256':protocol_hash,
            'independent_review_ready':protocol['review_status']=='reviewed', 'cache_exists':cache.exists(), 'provider_calls':0}
        if not args.live:
            print(json.dumps(summary));return 0
        if not summary['independent_review_ready'] or not protocol.get('label_review_sha256'):
            raise ValueError('Independent label review must finish before paid encoding')
        directory.mkdir(exist_ok=True)
        with (directory/'prepare.lock').open('x'):
            pass
        try:
            if cache.exists():
                encode_queries(rows,provider,directory,live=False)
                print(json.dumps({**summary,'cache_hit':True}));return 0
            attempts=directory/'attempts';attempts.mkdir(exist_ok=True)
            if attempts.resolve().parent != directory.resolve() or attempts.is_symlink():
                raise ValueError('Attempt receipts must stay in the private cache directory')
            previous=[json.loads(path.read_text(encoding='utf-8')) for path in attempts.glob('*.json')]
            if any(row.get('cache_key')==cache.name and row.get('status')!='complete' for row in previous) and not args.acknowledge_previous_attempt:
                raise ValueError('Previous uncertain attempt requires explicit acknowledgment and a new budget')
            receipt=attempts/(uuid4().hex+'.json')
            value={**summary,'cache_key':cache.name,'status':'started','max_calls':args.max_calls,'max_input_chars':args.max_input_chars}
            receipt.write_text(json.dumps(value),encoding='utf-8')
            try:
                _, usage=encode_queries(rows,provider,directory,live=True)
            except Exception:
                receipt.write_text(json.dumps({**value,'status':'failed','provider_calls':provider.calls}),encoding='utf-8')
                raise
            receipt.write_text(json.dumps({**value,'status':'complete','provider_calls':provider.calls}),encoding='utf-8')
            print(json.dumps({**summary,'provider_calls':usage['calls'],'cache_hit':usage['cache_hit']}));return 0
        finally:
            (directory/'prepare.lock').unlink()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ModelError):
        print('Preparation stopped; no automatic retry. Check protocol, explicit budgets, cache, and private attempt receipts.',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
