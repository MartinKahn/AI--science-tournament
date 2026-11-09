"""Whole-schedule upper bounds from enforced input/output envelopes, no discounts."""
from api_budget import usage_upper_cost
from research_v6.policy import request_spec,spec_cost

def envelope_cost(config,role,model=None):
    spec=config['v6']['roles'][role]
    pricing=config['pricing_profiles'][model or spec['model']]
    # Input envelope includes 4096 tokens for framing, as api_budget does.
    return usage_upper_cost({'input_tokens':spec['max_input_bytes']+4096,
                             'output_tokens':spec['max_output_tokens']},pricing)

def preflight(config):
    sizes=config['round_sizes'][:-1];n=sum(sizes)
    if len(sizes)<3 or sizes[-1]!=2 or any(x<1 for x in sizes):raise ValueError('Require at least three rounds ending with two finalists')
    if config['max_attempts']!=1:raise ValueError('Disable automatic transport retries to preserve the planned budget')
    expected={'writer':config['model'],'nature':config.get('reviewer_models',{}).get('nature_1',config['model']),
              'committee':config.get('reviewer_models',{}).get('committee_1',config['model']),
              'fact_checker':config.get('reviewer_models',{}).get('fact_checker',config['model'])}
    if config.get('reviewer_models',{}).get('committee_2',config['model'])!=expected['committee']:
        raise ValueError('Committee models must match the shared committee envelope')
    for role,model in expected.items():
        if config['v6']['roles'][role]['model']!=model:raise ValueError('Role model does not match its budget envelope: '+role)
    counts={'writer':n,'nature':n,'committee':2*n,'fact_checker':n,
            'card':n,'group':len(sizes)-1,'meta':len(sizes)-1,'pair':1}
    if sum(counts.values())>config['max_api_calls']:raise ValueError('Call ceiling is below the base schedule')
    schedule=config.get('reviewer_model_schedule')
    if schedule and len(schedule)!=len(sizes):raise ValueError('Reviewer schedule must match all draft cycles')
    costs={role:0.0 for role in counts}
    for stage,size in enumerate(sizes):
        for index in range(size):
            costs['writer']+=spec_cost(config,request_spec(config,'writer',stage,actor='writer-'+str(index+1).zfill(2)))
        for reviewer,role in [('nature_1','nature'),('committee_1','committee'),('committee_2','committee'),('fact_checker','fact_checker')]:
            costs[role]+=size*spec_cost(config,request_spec(config,role,stage,reviewer=reviewer))
        costs['card']+=size*spec_cost(config,request_spec(config,'card',stage))
        if stage<len(sizes)-1:
            for role in ['group','meta']:costs[role]+=spec_cost(config,request_spec(config,role,stage))
    costs['pair']=spec_cost(config,request_spec(config,'pair',len(sizes)-1))
    base=sum(costs.values());repair=config['v6']['repair_reserve_usd'];ceiling=config['budget_usd']
    return {'counts':counts,'upper_costs_usd':costs,'base_upper_usd':base,
            'repair_reserve_usd':repair,'total_upper_usd':base+repair,
            'cap_usd':ceiling,'fits':base+repair<=ceiling,
            'contract':'Input envelopes enforced before billing; no transport retries; bounded repairs use the reserved allowance. Unknown outcomes keep their full reservation.'}
