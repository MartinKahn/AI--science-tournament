"""Private rank feedback, warning recovery and distinct final verdicts."""
import hashlib
import json

def standings(ranking,reviews,mapping,round_number):
    result={}
    for position,row in enumerate(ranking,1):
        alias=row['alias'];own=reviews[alias];requests=[]
        for review in own:
            for key in ['priority_revisions','story_gaps','required_scope_cuts']:
                for text in review.get(key,[]):
                    retained={a['action'] for a in review.get('scope_actions',[]) if a['kind']=='keep_existing_exclusion'}
                    if key=='required_scope_cuts' and text in retained:continue
                    if text not in requests:requests.append(text)
        warning=round_number==1 and position==len(ranking)
        criteria=[{'id':'W'+str(i+1),'requirement':text} for i,text in enumerate(requests[:3])]
        if warning and not criteria:criteria=[{'id':'W1','requirement':'Strengthen the decisive scientific comparison and its distinction from the nearest prior study; language polishing is insufficient.'}]
        result[mapping[alias]]={'round':round_number,'rank':position,'population':len(ranking),
          'own_scores':{'journal':row['journal_score'],'committee':row['committee_score']},
          'notice':'All four advance now. One proposal will be eliminated after round 2; revised scientific quality decides.' if round_number==1 else 'Three directions continue through the final revision cycle.',
          'warning':warning,'warning_message':'You will be eliminated after round 2 unless the revised science substantially improves against these criteria. Language polishing alone does not count. Recovery earns reconsideration, not guaranteed survival.' if warning else '',
          'warning_criteria':criteria if warning else [],
          'required_improvements':requests[:4]}
    return result


def verify_improvement(assessment,criteria,new_draft):
    known={x['id'] for x in criteria};encoded=json.dumps(new_draft,ensure_ascii=False)
    def all_strings(v):
        if isinstance(v,str):return [v]
        if isinstance(v,dict):return sum((all_strings(x) for x in v.values()),[])
        if isinstance(v,list):return sum((all_strings(x) for x in v),[])
        return []
    strings=all_strings(new_draft);verified=[];seen=set()
    for check in assessment['criteria']:
        if check['criterion_id'] not in known:raise ValueError('Unknown warning criterion')
        if check['criterion_id'] in seen:raise ValueError('Duplicate warning criterion')
        seen.add(check['criterion_id'])
        quote=check['evidence_quote'].strip()
        # Strip presentation delimiters only; never approximate or invent quotes.
        if len(quote)>=2 and (quote[0],quote[-1]) in [('“','”'),('"','"'),('‘','’')]:quote=quote[1:-1]
        literal=bool(quote) and any(quote in s for s in strings)
        if (check['met'] or quote) and not literal:raise ValueError('Warning evidence quotation needs repair: '+check['criterion_id'])
        if check['met'] and literal:verified.append(check['criterion_id'])
    if seen!=known:raise ValueError('Warning review must assess every assigned criterion')
    if assessment['substantial'] and not verified:raise ValueError('Substantial improvement verdict lacks a supported criterion; return to reviewer')
    return bool(assessment['substantial'] and verified),verified


def verdict(row,reviews):
    facts=reviews[1];committee=reviews[2:]
    central=[c for c in facts['checks'] if c['centrality']=='central']
    contradicted=any(c['verdict'] in ['contradicted','hypothetical_conflict'] for c in central)
    baseline_conflict=any(r['baseline_contradictions'] for r in committee)
    novelty_gap=any(c['claim_kind']=='novelty' and c['verdict']=='not_verified' for c in central)
    new_cuts=[a for r in committee for a in r['scope_actions'] if a['kind']=='new_required_cut' and a['blocking']]
    conditional_path=any(r['decision']!='not_feasible' for r in committee)
    feasible=(all(r['decision']=='feasible' and r['scores']['feasibility_24_months']>=3 for r in committee) and not new_cuts)
    return {'editorial_pass':row['journal_pass'],
       'literature_status':'contradiction' if contradicted else 'provisional_novelty' if novelty_gap else 'evidence_gaps' if facts['decision']!='clear' or not row['fact_check_pass'] else 'supported_within_supplied_evidence',
       'feasibility_status':'feasible_with_validation_gates' if feasible else 'conditional_scope_revision' if conditional_path else 'not_feasible',
       'baseline_conflict':baseline_conflict,'new_blocking_scope_cuts':new_cuts,
       'full_pass':bool(row['journal_pass'] and row['integrity_pass'] and row['fact_check_pass'] and feasible),
       'conditional_recommendation_eligible':bool(row['journal_pass'] and not contradicted and not baseline_conflict and conditional_path)}


def classify_issues(actor,reviews):
    issues=[]
    for index,review in enumerate(reviews):
        for field in ['missing_primary_sources','contradictions','priority_revisions','provenance_errors','unsupported_resource_assumptions']:
            for text in review.get(field,[]):
                lower=text.lower()
                if field in ['missing_primary_sources','unsupported_resource_assumptions']:owner='lab_user' if any(word in lower for word in ['stock','pedigree','genotyp','allele records','project records']) else 'retrieval'
                else:owner='writer'
                ident=hashlib.sha256((str(actor)+str(index)+text).encode()).hexdigest()[:16]
                issues.append({'id':ident,'reviewer_index':index,'text':text,'owner':owner,
                  'status':'open','next_action':'Obtain original passages or record access gap' if owner=='retrieval' else 'Request/record actual lab information; do not invent it' if owner=='lab_user' else 'Address explicitly in the next scientific revision'})
    return issues
