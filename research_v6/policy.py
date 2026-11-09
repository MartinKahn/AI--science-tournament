"""One source of truth for per-stage model, effort and token allowance."""

def request_spec(config,role,stage,actor=None,reviewer=None):
    spec=dict(config['v6']['roles'][role]);spec['reasoning_effort']=config['reasoning_effort']
    schedule=config.get('reviewer_model_schedule')
    if reviewer and schedule:spec['model']=schedule[stage].get(reviewer,spec['model'])
    compute=config.get('research_compute',{})
    if role=='writer' and compute:
        if stage==0:
            phase=compute['initial_writers']
            spec['model']=phase.get('models',{}).get(actor,spec['model'])
        else:phase=compute['revision_writers']
        spec.update({key:value for key,value in phase.items() if key in ['reasoning_effort','max_input_bytes','max_output_tokens']})
        if phase.get('output_tokens_by_model'):spec['max_output_tokens']=phase['output_tokens_by_model'][spec['model']]
    if role in ['nature','committee'] and stage==len(config['round_sizes'])-2:
        spec.update(compute.get('final_reviews',{}))
    if role=='pair':spec.update(compute.get('final_comparison',{}))
    return spec


def spec_cost(config,spec):
    from api_budget import usage_upper_cost
    return usage_upper_cost({'input_tokens':spec['max_input_bytes']+4096,'output_tokens':spec['max_output_tokens']},config['pricing_profiles'][spec['model']])

def earned_repair_allowance(config,run_dir):
    """Borrow only proven unused envelopes from settled base jobs.

    Future base jobs keep their full planned allowance. Unknown charges earn no
    credit. Original reservations/charges are never cleared to fund a repair.
    """
    from pathlib import Path
    import json
    path=Path(run_dir);ledger=path/'budget.json'
    entries=json.loads(ledger.read_text())['entries'] if ledger.exists() else []
    groups={}
    for entry in entries:
        if '-repair1-' not in entry['job_id']:groups.setdefault(entry['job_id'],[]).append(entry)
    credit=0.0
    for job,charges in groups.items():
        audit_path=path/'agent_calls'/(job+'.json')
        if not audit_path.exists() or not (path/'responses'/(job+'.json')).exists():continue
        if any(e['status']!='settled_conservative_usage' for e in charges):continue
        audit=json.loads(audit_path.read_text())
        if not all(key in audit for key in ['input_byte_limit','output_token_limit','model']):continue
        allocated=spec_cost(config,{'max_input_bytes':audit['input_byte_limit'],'max_output_tokens':audit['output_token_limit'],'model':audit['model']})
        credit+=allocated-sum(e['accounted_usd'] for e in charges)
    credit=max(0.0,credit)
    used=sum(e.get('accounted_usd',e['reserved_usd']) for e in entries if '-repair1-' in e['job_id'])
    return {'base_allowance_usd':config['v6']['repair_reserve_usd'],'earned_credit_usd':credit,
            'allowance_usd':config['v6']['repair_reserve_usd']+credit,'committed_repairs_usd':used}
