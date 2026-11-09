"""Explicit API identities and authorized conversation histories, for independent research roles."""
import json
from pathlib import Path
from prospective import LENSES, REVIEWERS, DRAFT_SCHEMA

VERSION='api-personas-v1'

def roster(config):
    count=config['round_sizes'][0]
    result={}
    for index in range(count):
        actor=f'writer-{index+1:02d}'
        result[actor]={'id':actor,'role':'writer','model':config['model'],
            'perspective':LENSES[index],
            'system_prompt':f'You are {actor}, one independent research scientist designing a two-year PhD paper. Your starting research perspective is {LENSES[index]}. This is a perspective, not a predetermined hypothesis. You may change direction on scientific grounds. Preserve a distinctive independent scientific argument, respond critically to reviews and do not imitate a real professor. This is a hypothetical planning exercise. Return only the requested JSON; brief justifications belong in the specified fields, not hidden reasoning traces.',
            'history_policy':'own prior API requests and responses only; no competing proposals or grades'}
    if config.get('research_compute'):
        initial=config['research_compute']['initial_writers'].get('models',{})
        for actor_id,actor in result.items():
            actor['model_schedule']=[initial.get(actor_id,actor['model'])]+[actor['model']]*(len(config['round_sizes'])-2)
    for role,description in REVIEWERS.items():
        if role=='nature_1' and config.get('editorial'):description=config['editorial']['persona']
        result[role]={'id':role,'role':'reviewer','model':config.get('reviewer_models',{}).get(role,config['model']),'perspective':description,
            'system_prompt':f'You are {role}: {description}. Maintain this professional evaluation perspective and apply the supplied rubric consistently. Use only the authorized submission and literature in this request. Judge independently; do not imitate a real person. Return only the requested JSON.',
            'history_policy':'fresh context per candidate; stable persona, no competing submissions or other reviewer grades'}
    if config.get('reviewer_model_schedule'):
        for role in REVIEWERS:result[role]['model_schedule']=[stage.get(role,result[role]['model']) for stage in config['reviewer_model_schedule']]
    return result

def actor_for_job(job_id,run_dir,config,schema):
    if schema != DRAFT_SCHEMA:
        for role in REVIEWERS:
            if job_id.endswith('-'+role):return role
        raise ValueError('Cannot identify reviewer for '+job_id)
    candidate=job_id.removesuffix('-draft')
    round_id=candidate.split('-')[0]
    mapping=json.loads((Path(run_dir)/'private'/f'{round_id}-mapping.json').read_text())
    return f'writer-{mapping[candidate]+1:02d}'

def conversation(job_id,prompt,schema,run_dir,config,actors):
    """Reconstruct exactly the authorized writer conversation; reviewers stay fresh."""
    run_dir=Path(run_dir)
    actor_id=actor_for_job(job_id,run_dir,config,schema)
    actor=actors[actor_id]
    messages=[{'role':'system','content':actor['system_prompt']}]
    if actor['role']=='writer':
        previous=[]
        for path in (run_dir/'agent_calls').glob('*.json') if (run_dir/'agent_calls').exists() else []:
            item=json.loads(path.read_text())
            if item['actor_id']==actor_id and item['job_id']!=job_id and (run_dir/'jobs'/f"{item['job_id']}.json").exists():
                if item['job_id'].split('-')[0] < job_id.split('-')[0]:previous.append(item)
        for item in sorted(previous,key=lambda x:x['job_id']):
            messages.append({'role':'user','content':item['user_message']})
            output=json.loads((run_dir/'jobs'/f"{item['job_id']}.json").read_text())['output']
            messages.append({'role':'assistant','content':json.dumps(output,ensure_ascii=False,separators=(',',':'))})
        current=dict(prompt)
        if previous:
            # Baseline/literature remain in the initial user turn; own last draft
            # is already the assistant turn. New reviews remain in this request.
            for key in ['evidence','literature','previous_draft']:current.pop(key,None)
        user=json.dumps(current,ensure_ascii=False,separators=(',',':'))
    else:user=json.dumps(prompt,ensure_ascii=False,separators=(',',':'))
    messages.append({'role':'user','content':user})
    audit={'actor_id':actor_id,'job_id':job_id,'model':actor['model'],'messages':messages,
           'user_message':user,'identity_version':VERSION,'history_policy':actor['history_policy']}
    return audit
