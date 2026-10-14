"""Frozen inputs and independent, explicit scoring for the equipment benchmark."""
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_suite(directory=None):
    directory=Path(directory) if directory is not None else HERE
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    content=(directory/'cases.jsonl').read_bytes()
    if hashlib.sha256(content).hexdigest()!=manifest['sha256']:
        raise ValueError('Dataset differs from its frozen manifest; create a new version')
    cases=[json.loads(line) for line in content.decode('utf-8').splitlines() if line]
    if len(cases)!=manifest['cases'] or len({c['id'] for c in cases})!=len(cases):
        raise ValueError('Invalid case count or duplicate IDs')
    families=defaultdict(set)
    for case in cases:
        families[case['family']].add(case['split'])
    if any(len(splits)!=1 for splits in families.values()):
        raise ValueError('Family leakage across splits')
    if (len(families)!=manifest['families']
            or any(c['split'] not in ('dev','holdout') for c in cases)
            or dict(Counter(c['split'] for c in cases))!=manifest['split_counts']):
        raise ValueError('Family or split counts differ from the frozen manifest')
    return manifest,cases


def value_matches(value, expected):
    # No production equipment parser, embeddings or model-as-judge here. Strict
    # surface matching intentionally reports equivalent unlisted wording as a miss.
    return isinstance(value,str) and value.strip(' 。！!') in {x.strip(' 。！!') for x in expected}


def candidate_matches(candidate, expected, text, version):
    return bool(candidate and candidate.get('source_text')==text
        and candidate.get('source_version')==version
        and candidate.get('scope')==expected['scope']
        and value_matches(candidate.get('value'),expected['values']))


def advice_checks(answer, expected):
    checks={'advice_status':answer.get('status')==expected['status']}
    if expected['status']=='ready':
        checks['equipment_adoption']=(value_matches(answer.get('equipment'),expected['values'])
                                     and answer.get('equipment_source')==expected['source'])
        exercises=answer.get('exercises',[])
        checks['exercise_resources']=bool(exercises) and all(
            set(item.get('equipment',[]))<=set(expected['resources']) for item in exercises)
    else:
        checks['no_advice_without_facts']=not answer.get('exercises')
    return checks


def summarize(results):
    metrics=defaultdict(list)
    families=defaultdict(list)
    for case in results:
        families[case['family']].append(case['passed'])
        for step in case['steps']:
            for name,passed in step.get('checks',{}).items():
                metrics[name].append(bool(passed))
    def count(values):
        n=len(values)
        return {'passed':sum(values),'total':n,'rate':sum(values)/n if n else None}
    return {'cases':count([c['passed'] for c in results]),
        'metrics':{name:count(values) for name,values in sorted(metrics.items())},
        'families':{name:count(values) for name,values in sorted(families.items())}}


def latency_summary(values):
    if not values:
        return {'n':0,'mean_seconds':None,'p95_seconds':None}
    return {'n':len(values),'mean_seconds':sum(values)/len(values),
            'p95_seconds':sorted(values)[max(0,math.ceil(.95*len(values))-1)]}
