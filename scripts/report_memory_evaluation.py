"""Combine disjoint evaluation partitions without cherry-picking duplicate cases."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evals.memory.protocol import load_suite, summarize

LABELS={
    'raw_candidate_exact':'模型原始候选严格匹配',
    'accepted_candidate_exact':'业务校验后候选严格匹配',
    'manual_confirmation_required':'保留人工确认',
    'confirmation_applied':'明确确认后应用',
    'no_unconfirmed_profile_write':'未确认不改长期档案',
    'no_unconfirmed_day_write':'未确认不改当天条件',
    'clarification':'歧义/不确定/未来范围明确补问',
    'no_false_candidate':'应补问时没有记忆候选',
    'equipment_adoption':'最终建议器械及来源严格匹配',
    'exercise_resources':'最终动作所需器械符合标注',
    'advice_status':'最终建议状态符合标注',
    'advice_api_success':'训练建议接口完成',
    'no_advice_without_facts':'缺少事实不输出动作',
    'concurrent_stale_rejection':'并发更正拒绝旧草稿',
    'superseded_advice_stale':'A→B→A后旧建议仍失效',
    'account_isolation':'跨账号读/删拒绝',
    'no_actual_record_writes':'不产生实际记录',
    'step_completed':'异常步骤完成',
}


def combine(paths):
    manifest,cases=load_suite()
    known={c['id']:c for c in cases}
    rows={}
    metas=[]
    attempts=failures=responses=0
    unfinished=[]
    run_latencies=[]
    for path in map(Path,paths):
        meta=json.loads((path/'run.json').read_text(encoding='utf-8'))
        summary=json.loads((path/'summary.json').read_text(encoding='utf-8'))
        if meta['dataset']['sha256']!=manifest['sha256']:
            raise ValueError('Dataset mismatch')
        if metas and any(meta[key]!=metas[0][key] for key in ('mode','model','protocol','source_hashes')):
            raise ValueError('Cannot mix model modes, models, protocols or source versions')
        if not summary['source_unchanged']:
            raise ValueError('Source changed during run')
        metas.append(meta)
        for line in (path/'cases.jsonl').read_text(encoding='utf-8').splitlines():
            row=json.loads(line)
            if row['id'] in rows or row['id'] not in known:
                raise ValueError('Duplicate or unknown case; never select the better attempt')
            if any(row[key]!=known[row['id']][key] for key in ('split','family')):
                raise ValueError('Case partition changed')
            rows[row['id']]=row
        ledger=path/'calls.jsonl'
        events=[json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()] if ledger.exists() else []
        started={e['call'] for e in events if e['event']=='attempt'}
        ended={e['call'] for e in events if e['event'] in ('response','failure')}
        attempts+=len(started)
        responses+=sum(e['event']=='response' for e in events)
        failures+=sum(e['event']=='failure' for e in events)
        unfinished.extend({'run':path.name,'call':c} for c in sorted(started-ended))
        run_latencies.append({'run':path.name,**summary['provider_latency']})
    if not metas:
        raise ValueError('At least one run is required')
    result={'dataset':manifest,'mode':metas[0]['mode'],'model':metas[0]['model'],
        'provider_attempts':attempts,'provider_responses':responses,'provider_failures':failures,
        'unfinished_attempts':unfinished,'missing_ids':sorted(set(known)-rows.keys()),
        'run_latency':run_latencies,'runs':[Path(p).name for p in paths],
        'all':summarize(list(rows.values())),
        **{split:summarize([row for row in rows.values() if row['split']==split]) for split in ('dev','holdout')}}
    return result


def fraction(metric):
    return f"{metric['passed']}/{metric['total']}（{metric['rate']:.2%}）" if metric and metric['total'] else '不适用'


def markdown(result):
    title='真实模型固定合成场景结果' if result['mode']=='live' else '标注回放接线检查（不是模型效果）'
    lines=[f'# Fitness 记忆评测：{title}', '',
        f"题集 `{result['dataset']['version']}`；SHA-256 `{result['dataset']['sha256']}`。",
        '100组＝20个场景族×5种器械变体，开发60组/留出40组。自建合成、无独立人工复核，不能外推真实用户准确率。', '',
        f"模式：{result['mode']}；型号：{result['model'] or '模型替身'}。",
        f"供应商尝试 {result['provider_attempts']} 次，取得响应 {result['provider_responses']} 次，连接/供应商失败 {result['provider_failures']} 次。",
        f"未收到结束事件的调用 {len(result['unfinished_attempts'])} 次；未运行场景 {len(result['missing_ids'])} 组。",
        '失败保留在分母；连接失败不等于模型理解错误。未收集token或账单金额。', '',
        '| 指标 | 全部 | 开发集 | 留出集 |', '| --- | --- | --- | --- |']
    for name in result['all']['metrics']:
        values=[fraction(result[split]['metrics'].get(name)) for split in ('all','dev','holdout')]
        lines.append(f"| {LABELS.get(name,name)} | {' | '.join(values)} |")
    lines+=['',f"全步骤同时通过：{fraction(result['all']['cases'])}。它要求原始提取也正确；程序纠正模型之后的成功另见候选/建议指标。", '',
        '## 场景族', '', '| 场景族 | 全步骤通过 |', '| --- | --- |']
    lines += [f"| {family} | {fraction(metric)} |" for family,metric in result['all']['families'].items()]
    lines+=['', '## 调用耗时', '',
        '以下按运行分片统计供应商调用耗时，包含失败尝试；不是完整请求延迟。不同分片的p95不取平均。', '',
        '| 运行分片 | 调用数 | 平均秒数 | p95秒数 |', '| --- | --- | --- | --- |']
    for latency in result['run_latency']:
        mean=f"{latency['mean_seconds']:.3f}" if latency['n'] else '不适用'
        p95=f"{latency['p95_seconds']:.3f}" if latency['n'] else '不适用'
        lines.append(f"| {latency['run']} | {latency['n']} | {mean} | {p95} |")
    lines+=['','## 边界与下一步','',
        '没有旧版本匹配对照，不据此填写“增强前→增强后”的提升百分比。严格匹配使用器械原话及来源，未列出的同义摘录也会判失败；它不是通用语义正确率。',
        '本集没有测试长历史噪声检索、其他偏好或医疗推荐。保留本轮结果，后续只用开发集调试；不反复使用留出题挑选有利配置。', '',
        '运行分片：'+', '.join(result['runs'])+'。每片保留run.json、cases.jsonl和summary.json；真实调用另有calls.jsonl台账。']
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs',nargs='+',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    result=combine(args.runs)
    # A new directory preserves earlier reports and every underlying failure.
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (args.output/'REPORT.md').write_text(markdown(result),encoding='utf-8')
    print(json.dumps({'reported_cases':result['all']['cases']['total'],'attempts':result['provider_attempts'],
                      'missing_cases':len(result['missing_ids'])}))


if __name__=='__main__':
    main()
