"""Structured scientific evolution, anonymous clustering, and filtered meta-review."""
from prospective import obj,arr,string,integer,STR,STRS
CARD_SCHEMA=obj({'hypothesis':STR,'mechanism':STR,'decisive_experiment':STR,'competing_explanation':STR,
                 'revision_strategy':string('initial','strengthen','pivot','simplify','resolve_evidence_conflict'),
                 'scientific_change':STR})
GROUP_SCHEMA=obj({'groups':arr(obj({'id':STR,'members':STRS,'causal_distinction':STR}))})
META_SCHEMA=obj({'methodological_weaknesses':STRS,'unresolved_disagreements':STRS,
                 'proposed_corrections':arr(obj({'text':STR,'source_id':STR,'exact_passage':STR})),
                 'evidence_requests':arr(obj({'query':STR,'reason':STR,'centrality':string('central','supporting')}))})
PAIR_SCHEMA=obj({'preferred':string('A','B','neither'),'importance_comparison':STR,
                 'evidence_comparison':STR,'feasibility_comparison':STR,'uncertainties':STRS})

def validate_groups(result,aliases):
    members=[x for group in result['groups'] for x in group['members']]
    if len(members)!=len(set(members)) or set(members)!=set(aliases):
        raise ValueError('Grouping must cover each candidate exactly once')
    ids=[group['id'] for group in result['groups']]
    if len(ids)!=len(set(ids)) or any(not g['members'] for g in result['groups']):raise ValueError('Empty/duplicate groups')

def select_diverse(ranking,groups,count):
    """One eligible member per group, then fill by rank; never bypass eligibility."""
    selected=[];seen=set();membership={alias:g['id'] for g in groups['groups'] for alias in g['members']}
    for row in ranking:
        group=membership[row['alias']]
        if row['eligible'] and group not in seen:
            selected.append(row);seen.add(group)
            if len(selected)==count:return selected
    for row in ranking:
        if row['eligible'] and row not in selected:
            selected.append(row)
            if len(selected)==count:break
    return selected

def safe_meta(review,store):
    """Only deterministic source-verified corrections enter the shared channel.

    Methodological summaries stay private: generated free text cannot reliably be
    guaranteed to exclude a candidate's proposed mechanism or experimental idea.
    """
    accepted=[];rejected=[]
    for correction in review['proposed_corrections']:
        source=store.data['sources'].get(correction['source_id'])
        if source and correction['exact_passage'] and correction['exact_passage'] in source['text']:
            # Share the passage itself, not an unverified model interpretation.
            accepted.append({'source_id':source['id'],'paper_id':source['paper_id'],
                             'location':source.get('location',''),'exact_passage':correction['exact_passage']})
        else:rejected.append(correction)
    return {'shared':{'verified_passages':accepted,
                     'general_reminders':['Separate correlation from causality.','Plan interpretable alternative outcomes.','Keep essential work within 24 months.']},
            'private':{'methodological_weaknesses':review['methodological_weaknesses'],
                       'unresolved_disagreements':review['unresolved_disagreements'],
                       'evidence_requests':review['evidence_requests'],'rejected_corrections':rejected}}

def meta_review_brief(review):
    issues=[]
    for key in ['contradictions','missing_primary_sources','priority_revisions','story_gaps','required_scope_cuts','critical_path']:
        for text in review.get(key,[]):
            item=str(text)[:300]
            if item not in issues:issues.append(item)
    return {'decision':review['decision'],'issues':issues[:3]}

def revision_path_exists(committee):
    """A conditional feasible plan is eligible for repair, not final acceptance."""
    return (not any(review['baseline_contradictions'] for review in committee)
            and any(review['decision'] in ['feasible','feasible_with_scope_cuts'] for review in committee))
