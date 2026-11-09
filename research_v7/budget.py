"""Explicit worst-case accounting, including conditional structure/brief rewrites."""
from research_v6.policy import request_spec,spec_cost

def workflow_spec(config,role,stage,actor=None,reviewer=None):
    if role=='brief':return request_spec(config,'writer',0,actor=actor)
    if role in ['nature','committee','fact_checker','card','group','meta','pair']:
        return request_spec(config,role,stage,reviewer=reviewer)
    return dict(config['v7']['roles'][role])

def preflight(config):
    sizes=config['round_sizes'][:-1]
    if sizes!=[4,4,3,3,3,3]:raise ValueError('V7 schedule must be 4,4,3,3,3,3 before final recommendation')
    if config['word_limit']!=200:raise ValueError('Nature Communications abstract limit must be 200')
    if config['max_attempts']!=1:raise ValueError('Unknown transport outcomes cannot be retried automatically')
    costs={};counts={}
    def add(role,n,stage=0,actor=None,reviewer=None):
        counts[role]=counts.get(role,0)+n
        costs[role]=costs.get(role,0)+n*spec_cost(config,workflow_spec(config,role,stage,actor,reviewer))
    for actor in ['writer-01','writer-02','writer-03','writer-04']:add('brief',1,actor=actor)
    add('brief_review',8);add('brief_revision',4)
    for stage,n in enumerate(sizes):
        add('search',n,stage);add('composition',n,stage)
        add('technical',2*n,stage);add('edit',n,stage)
        for reviewer,role in [('nature_1','nature'),('committee_1','committee'),('committee_2','committee'),('fact_checker','fact_checker')]:add(role,n,stage,reviewer=reviewer)
        add('card',n,stage)
        if stage<len(sizes)-1:add('group',1,stage);add('meta',1,stage)
    add('improvement',1,1);add('pair',1,5);add('issue_update',sum(sizes[1:]),1)
    # issue reconciliation occurs after each round; no model call, no charge.
    maximum_calls=sum(counts.values())
    if maximum_calls>config['max_api_calls']:raise ValueError('Call ceiling below the bounded complete schedule')
    base=sum(costs.values());reserve=config['v6']['repair_reserve_usd']
    return {'counts_including_conditional_calls':counts,'maximum_calls':maximum_calls,
      'upper_costs_usd':costs,'base_upper_usd':base,'repair_reserve_usd':reserve,
      'total_upper_usd':base+reserve,'cap_usd':config['budget_usd'],'fits':base+reserve<=config['budget_usd'],
      'assumptions':['Every possible one-pass structure correction and research-brief correction is funded in this upper scenario.',
      'Unchanged submissions reuse cached checks. Actual calls can be fewer; smaller realized usage is not guaranteed.',
      'Enforced byte and output-token envelopes include reasoning; pinned standard tariffs, no cache discounts.',
      'Paid launch is blocked if this complete bound exceeds the configured cap.']}
