"""Replay recorded development responses through current APIs without cloud calls.

This measures downstream handling of fixed old model outputs, not new model or
prompt quality. Provider failures have no response to replay and stay listed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.memory.protocol import load_suite, summarize
from schemas import CoachUnderstanding
from scripts.evaluate_memory import Scenario, append_event, source_hashes, write_json
from scripts.report_memory_evaluation import LABELS, fraction


class RecordedModel:
    mode = 'replay'
    model_name = 'recorded-structured-response'

    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = 0
        self.last = self.step = self.during = None

    def generate(self, *, system_prompt, message):
        # The application constructs its normal prompt; gold labels and the new
        # prompt cannot change these frozen responses from the earlier run.
        if self.calls >= len(self.outputs):
            raise ValueError('No recorded response for this call')
        if self.during:
            self.during()
        self.last = CoachUnderstanding.model_validate(self.outputs[self.calls]).model_dump()
        self.calls += 1
        return json.dumps(self.last, ensure_ascii=False)


def replay(paths, output):
    manifest, cases = load_suite()
    dev = {case['id']: case for case in cases if case['split'] == 'dev'}
    baselines, input_hashes, provenance = {}, {}, None
    for path in map(Path, paths):
        meta = json.loads((path / 'run.json').read_text(encoding='utf-8'))
        summary = json.loads((path / 'summary.json').read_text(encoding='utf-8'))
        if meta['mode'] != 'live' or meta['dataset']['sha256'] != manifest['sha256'] or not summary['source_unchanged']:
            raise ValueError('Expected frozen live inputs with unchanged source and matching dataset')
        identity = {key: meta[key] for key in ('model', 'protocol', 'source_hashes')}
        if provenance is not None and identity != provenance:
            raise ValueError('Baseline source/model/protocol differs between partitions')
        provenance = identity
        raw = (path / 'cases.jsonl').read_bytes()
        input_hashes[path.as_posix()] = hashlib.sha256(raw).hexdigest()
        for line in raw.decode('utf-8').splitlines():
            row = json.loads(line)
            if row['id'] not in dev:
                continue  # No holdout output is used for this debugging replay.
            if row['id'] in baselines or row['family'] != dev[row['id']]['family'] or row['split'] != 'dev':
                raise ValueError('Duplicate or inconsistent development case')
            baselines[row['id']] = row
    if provenance is None:
        raise ValueError('At least one baseline run is required')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    hashes = source_hashes()
    hashes['scripts/replay_memory_responses.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_json(output / 'run.json', {'mode': 'recorded_dev_replay', 'dataset': manifest,
        'input_hashes': input_hashes, 'baseline': provenance, 'current_source_hashes': hashes,
        'provider_calls': 0, 'quality_claim': 'fixed_response_handling_only_not_new_model_quality'})
    before, after, unavailable = [], [], []
    for case_id, case in dev.items():
        row = baselines.get(case_id)
        outputs = [step.get('observed', {}).get('raw') for step in row['steps'] if step['op'] == 'message'] if row else []
        if len(outputs) != sum(step['op'] == 'message' for step in case['steps']) or any(
                not item or item.get('invalid_schema') for item in outputs):
            unavailable.append({'id': case_id, 'reason': 'missing_recorded_response',
                'baseline_errors': [step.get('observed', {}).get('error') for step in row['steps']
                    if step['op'] == 'message'] if row else []})
            continue
        model = RecordedModel(outputs)
        with TemporaryDirectory(prefix='fitness-memory-replay-') as directory:
            result = Scenario(case, Path(directory), model, manifest['base_day']).run()
        if model.calls != len(outputs):
            result['passed'] = False
            result['steps'].append({'op': 'replay_boundary', 'checks': {'all_recorded_outputs_consumed': False}})
        before.append(row)
        after.append(result)
        append_event(output / 'cases.jsonl', result)
        print(json.dumps({'case': case_id, 'completed': len(after), 'provider_calls': 0}), flush=True)
    current = source_hashes()
    current['scripts/replay_memory_responses.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    unchanged = current == hashes and all(hashlib.sha256((Path(path) / 'cases.jsonl').read_bytes()).hexdigest() == sha
        for path, sha in input_hashes.items())
    result = {'mode': 'recorded_dev_replay', 'source_unchanged': unchanged, 'provider_calls': 0,
        'planned_dev_cases': len(dev), 'replayed_cases': len(after), 'unavailable': unavailable,
        'before': summarize(before), 'after': summarize(after)}
    write_json(output / 'summary.json', result)
    lines = ['# E2c：固定模型输出的开发集回放', '',
        f"开发场景{len(dev)}组，实际回放{len(after)}组，缺少可回放输出{len(unavailable)}组，新增云调用0次。",
        '模型输出固定为2026-09-28的已保存结构化响应；只比较后端处理，不评价新提示词或真实模型准确率。',
        '只使用开发集；无响应的供应商失败无法重放，逐条保留在summary.json中，旧报告不变。', '',
        '| 指标 | 原处理（同一批可回放题） | 当前处理 |', '| --- | --- | --- |']
    for name in sorted(set(result['before']['metrics']) | set(result['after']['metrics'])):
        lines.append(f"| {LABELS.get(name, name)} | {fraction(result['before']['metrics'].get(name))} | {fraction(result['after']['metrics'].get(name))} |")
    lines += ['', '全步骤通过仍要求原始模型输出本身满足标签；后端修复不会把错误原始输出改成提取成功。',
        '这不是新一轮真实模型评测，不与旧的98组真实报告合并，不作为总体记忆提升率。',
        f'源码和输入运行期间未变化：{unchanged}。']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.runs, args.output)
    return 0 if result['source_unchanged'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
