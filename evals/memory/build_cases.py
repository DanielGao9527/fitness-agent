"""Authoring source for the frozen synthetic suite. Not a production data generator."""
import hashlib
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = 'fitness-equipment-memory-v1'
INVENTORIES = [
    ('只有哑铃', ['floor', 'dumbbell']),
    ('只有弹力带', ['floor', 'band']),
    ('只有瑜伽垫', ['floor']),
    ('只有哑铃和训练凳', ['floor', 'dumbbell', 'seat']),
    ('只有壶铃', ['floor', 'kettlebell']),
]
NEGATIONS = [
    ('只有哑铃，没有训练凳', ['floor', 'dumbbell']),
    ('只有弹力带，没有哑铃', ['floor', 'band']),
    ('只有瑜伽垫，没有杠铃', ['floor']),
    ('只有哑铃，没有杠铃', ['floor', 'dumbbell']),
    ('只有壶铃，没有训练凳', ['floor', 'kettlebell']),
]
BASE_VALUE = '在家，徒手'
BASE_RESOURCES = ['floor']


def message(text, value=None, scope=None, *, outcome='candidate', session='source', confirm=True,
            choice=None, during=None):
    return {'op': 'message', 'session': session, 'text': text,
            'expect': {'outcome': outcome, 'values': [value] if value else [], 'scope': scope},
            'confirm': confirm, 'choice': choice, 'during': during}


def advice(value=BASE_VALUE, resources=None, source='profile', session='probe', actor='alice', status='ready'):
    return {'op': 'advice', 'session': session, 'actor': actor,
            'expect': {'values': [value] if value else [], 'resources': resources or BASE_RESOURCES,
                       'source': source, 'status': status}}


def build_cases():
    # Each family has five inventory/wording variants. All variants stay in the
    # same split. Labels below are authored, never obtained from the evaluator.
    families = [
        ('stable', 'dev', '明确长期条件', lambda v,r: [
            message(f'以后安排我的力量训练请记住，{v}。',v,'long_term'), advice(v,r)]),
        ('day_cross_session', 'dev', '当天新会话采用', lambda v,r: [
            message(f'今天换地方训练了，{v}，帮我按这个条件安排。',v,'day'), advice(v,r,'day')]),
        ('session_isolation', 'dev', '本次不跨会话', lambda v,r: [
            message(f'这次力量训练的条件是{v}。',v,'session'),
            advice(v,r,'current_conversation','source'), advice()]),
        ('scope_choice', 'dev', '未明确范围由用户选择', lambda v,r: [
            message(f'我训练的地方{v}，帮我看下。',v,'unspecified',choice='session'),
            advice(v,r,'current_conversation','source'), advice()]),
        ('day_expiry', 'dev', '当天条件跨零点失效', lambda v,r: [
            message(f'今晚力量训练{v}。',v,'day'), advice(v,r,'day'),
            {'op':'advance','days':1}, advice(session='tomorrow')]),
        ('session_expiry', 'dev', '本次条件跨零点失效', lambda v,r: [
            message(f'本次训练{v}，照这个条件来。',v,'session'), advice(v,r,'current_conversation','source'),
            {'op':'advance','days':1}, advice(session='source')]),
        ('profile_correction', 'dev', '档案更正覆盖旧本次条件', lambda v,r: [
            message(f'这次我能用的条件是{v}。',v,'session'), advice(v,r,'current_conversation','source'),
            {'op':'profile','value':'在家，徒手'}, {'op':'profile','value':'只有哑铃'},
            advice('只有哑铃',['floor','dumbbell'],session='source')]),
        ('profile_clear', 'dev', '清空后应补问', lambda v,r: [
            message(f'长期训练的器械条件就按{v}保存。',v,'long_term'),
            {'op':'profile','value':''}, advice(None,source=None,status='needs_input')]),
        ('no_confirmation', 'dev', '未确认不得保存', lambda v,r: [
            message(f'以后训练我的器械就是{v}。',v,'long_term',confirm=False), advice()]),
        ('uncertain_claim', 'dev', '不确定信息不能成为事实', lambda v,r: [
            message(f'训练的地方可能{v}，但我还没确认。',outcome='clarify',confirm=False), advice()]),
        ('future_condition', 'dev', '未来条件不提前使用', lambda v,r: [
            message(f'明天训练的条件是{v}，今天还不确定。',outcome='clarify',confirm=False), advice()]),
        ('mixed_scopes', 'dev', '长期和今天拆开核对', lambda v,r: [
            message(f'平时训练{v}，但今天只能徒手，帮我同时记住。',outcome='clarify',confirm=False), advice()]),
        ('semantic_correction', 'holdout', '两次口语确认后采用较新条件', lambda v,r: [
            message(f'我平时训练{v}，以后按这个来。',v,'long_term'),
            message('以后训练都改为在家，徒手。','在家，徒手','long_term',session='correction'), advice()]),
        ('day_aba', 'holdout', '当天A变B再变A旧建议仍失效', lambda v,r: [
            message(f'今天训练{v}，就用这些。',v,'day'), advice(v,r,'day'),
            message('今天训练只能徒手。','只能徒手','day',session='change'),
            message(f'今天又调整好了，训练{v}。',v,'day',session='return'),
            {'op':'stale','session':'probe'}, advice(v,r,'day',session='fresh')]),
        ('delete_day_source', 'holdout', '删除临时来源不恢复旧值', lambda v,r: [
            message(f'今天能用来训练的条件：{v}。',v,'day'), advice(v,r,'day'),
            {'op':'delete','session':'source'}, advice(session='after_delete')]),
        ('delete_durable_source', 'holdout', '删除聊天保留独立确认档案', lambda v,r: [
            message(f'以后训练条件固定为{v}，请保存。',v,'long_term'),
            {'op':'delete','session':'source'}, advice(v,r)]),
        ('restart', 'holdout', '重启仍采用本人当天条件', lambda v,r: [
            message(f'今天的训练安排请以{v}为准。',v,'day'), {'op':'restart'}, advice(v,r,'day')]),
        ('account_isolation', 'holdout', '另一账号不继承私人条件', lambda v,r: [
            message(f'以后我的训练条件就是{v}。',v,'long_term'),
            {'op':'foreign_access','session':'source'}, advice(None,source=None,status='needs_input',actor='bob'),
            advice(v,r,session='alice_probe')]),
        ('during_model_correction', 'holdout', '理解期间档案更正阻止旧草稿', lambda v,r: [
            message(f'本次训练{v}，先按这些安排。',v,'session',outcome='stale',confirm=False,
                    during={'op':'profile','value':'只有哑铃'}), advice('只有哑铃',['floor','dumbbell'])]),
        ('negated_equipment', 'holdout', '不得丢失不可用器械', lambda v,r: [
            message(f'以后我的力量训练条件是{v}。',v,'long_term'), advice(v,r)]),
    ]
    cases=[]
    for family,split,description,builder in families:
        for i,(value,resources) in enumerate(NEGATIONS if family=='negated_equipment' else INVENTORIES,1):
            cases.append({'id':f'{family}-{i:02}', 'family':family,'split':split,'description':description,
                'seed_profile':{'equipment':BASE_VALUE,'experience':'experienced','minutes_per_session':40},
                'steps':builder(value,resources)})
    return cases


def main():
    cases=build_cases()
    content=''.join(json.dumps(case,ensure_ascii=False,sort_keys=True)+'\n' for case in cases).encode('utf-8')
    target=HERE/'cases.jsonl'
    if target.exists():
        raise SystemExit('Frozen suite already exists; create a new version instead of overwriting it')
    target.write_bytes(content)
    manifest={'version':VERSION,'provenance':'self_authored_synthetic','human_independent_review':False,
        'sha256':hashlib.sha256(content).hexdigest(),'cases':len(cases),'families':20,
        'split_counts':dict(Counter(c['split'] for c in cases)), 'base_day':'2026-09-28',
        'scope':'equipment-only business lifecycle; not a third-party memory benchmark',
        'created_before_live_evaluation':True, 'split_unit':'family',
        'design_references':['https://github.com/xiaowu0162/LongMemEval','https://github.com/snap-research/locomo'],
        'copied_external_examples':False}
    (HERE/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    main()
