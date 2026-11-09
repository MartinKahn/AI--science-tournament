"""Version 3: prospective end-of-PhD papers, with separate editorial and feasibility judgments."""
from pathlib import Path
ROOT=Path(__file__).resolve().parent
TASK_VERSION='prospective-phd-v4-fact-checker'
LENSES = ['measurement validity', 'alternative causal explanations', 'generalization across settings', 'robustness to perturbations', 'minimal decisive experiment', 'failure-resistant project design', 'resource-efficient validation', 'unexpected alternative hypothesis', 'measurement validity', 'alternative causal explanations', 'generalization across settings', 'robustness to perturbations', 'minimal decisive experiment', 'failure-resistant project design', 'resource-efficient validation', 'unexpected alternative hypothesis', 'measurement validity', 'alternative causal explanations', 'generalization across settings', 'robustness to perturbations', 'minimal decisive experiment', 'failure-resistant project design', 'resource-efficient validation', 'unexpected alternative hypothesis']
REVIEWERS={
 'nature_1':'Nature reviewer: conceptual novelty, importance and broad interest',
 'fact_checker':'Literature fact-checker: primary-source controls, contradictions and evidence gaps; no prestige scoring',
 'committee_1':'PhD committee: scientific quality, technical execution and 24-month critical path',
 'committee_2':'PhD committee: scientific strategy, causal interpretation, thesis scope and failure resilience'
}

def obj(properties):return {'type':'object','additionalProperties':False,'properties':properties,'required':list(properties)}
def arr(items):return {'type':'array','items':items}
def string(*options):
 d={'type':'string'}
 if options:d['enum']=list(options)
 return d
def integer(lo=0,hi=5):return {'type':'integer','minimum':lo,'maximum':hi}
STR=string();STRS=arr(STR)
DRAFT_SCHEMA=obj({
 'artifact_status':string('hypothetical_end_of_phd_paper'),
 'title':STR,'abstract':STR,'core_discovery':STR,'novelty_vs_prior_work':STR,
 'baseline_use':arr(obj({'evidence_id':STR,'role':string('central','supporting','deemphasized','not_used'),'explanation':STR})),
 'claim_map':arr(obj({'claim':STR,'status':string('existing_result','anticipated_result','background','planned_method'),
                      'baseline_ids':STRS,'experiment_ids':STRS})),
 'proposed_experiments':arr(obj({'id':STR,'question':STR,'approach':STR,'anticipated_result':STR,
    'alternative_outcome_and_interpretation':STR,'critical_control':STR,'dependencies':STRS,
    'start_month':integer(1,24),'end_month':integer(1,24),
    'resource_status':string('available','unconfirmed','new_development'),
    'resource_assumptions':STR,'risk':string('low','medium','high'),'fallback':STR,
    'priority':string('essential','stretch')})),
 'milestones':arr(obj({'month':integer(1,24),'deliverable':STR,'go_no_go_criterion':STR})),
 'minimum_publishable_story':STR,'scope_cuts':STRS,'revision_notes':STR
})
LIT=arr(obj({'paper_id':STR,'overlap':STR,'implication':STR}))
NATURE_SCHEMA=obj({
 'decision':string('reject','consider_for_review','competitive_if_supported'),
 'scores':obj({k:integer() for k in ['novelty','importance','causal_completeness','broad_interest','clarity']}),
 'core_discovery':STR,'publication_case':STR,'story_gaps':STRS,
 'finding_whose_loss_collapses_case':STR,'priority_revisions':STRS,'literature_assessment':LIT
})
COMMITTEE_SCHEMA=obj({
 'decision':string('feasible','feasible_with_scope_cuts','not_feasible'),
 'scores':obj({k:integer() for k in ['project_quality','feasibility_24_months','scope_discipline','robustness']}),
 'quality_assessment':STR,'feasibility_assessment':STR,
 'baseline_contradictions':STRS,'provenance_errors':STRS,'unsupported_resource_assumptions':STRS,
 'critical_path':STRS,'required_scope_cuts':STRS,'go_no_go_assessment':STR,
 'minimum_publishable_story':STR,'priority_revisions':STRS,'literature_assessment':LIT
})
FACT_SCHEMA=obj({
 'decision':string('clear','revise_for_contradictions','insufficient_primary_evidence'),
 'checks':arr(obj({'claim':STR,'claim_kind':string('baseline','literature','novelty','prospective'),
    'centrality':string('central','supporting'),
    'verdict':string('supported','contradicted','not_verified','hypothetical_consistent','hypothetical_conflict'),
    'source_ids':STRS,'exact_evidence':STR,'location':STR,'scope':STR,'correction':STR})),
 'central_assumptions_checked':STRS,'contradictions':STRS,'missing_primary_sources':STRS,
 'priority_revisions':STRS,'literature_assessment':LIT
})
REVIEW_SCHEMAS={role:(NATURE_SCHEMA if role.startswith('nature') else FACT_SCHEMA if role=='fact_checker' else COMMITTEE_SCHEMA) for role in REVIEWERS}
JOURNAL_WEIGHTS={'novelty':.30,'importance':.30,'causal_completeness':.20,'broad_interest':.15,'clarity':.05}
COMMITTEE_WEIGHTS={'project_quality':.25,'feasibility_24_months':.40,'scope_discipline':.20,'robustness':.15}

def prompt_text(role):return (ROOT/'prompts'/f'{role}.md').read_text()

def task_contract():
 return {'version':TASK_VERSION,'prompts':{role:prompt_text(role) for role in ['writer',*REVIEWERS]},
         'draft_schema':DRAFT_SCHEMA,'review_schemas':REVIEW_SCHEMAS}

def draft_prompt(evidence,lens,word_limit,previous=None,reviews=None,literature=None):
 return {'task_version':TASK_VERSION,'task':'Design the desired completed paper after two remaining PhD years' if previous is None else 'Revise the hypothetical finished paper and its two-year plan using all four reviews',
   'instructions':prompt_text('writer'),'independent_starting_lens':lens,
   'lens_note':'This lens is a starting suggestion, not a required conclusion or mechanism. Change direction if a stronger feasible paper emerges.',
   'abstract_word_limit':word_limit,'evidence':evidence,'literature':literature or {},'previous_draft':previous,'reviews':reviews}

def review_prompt(evidence,draft,reviewer,word_limit,literature=None):
 common={'task_version':TASK_VERSION,'role':REVIEWERS[reviewer],'instructions':prompt_text(reviewer),
         'abstract_word_limit':word_limit,
         'artifact_context':'The abstract describes an explicitly hypothetical finished paper, not currently obtained results.'}
 if reviewer.startswith('nature'):
  # Old research reports discuss the student's baseline and the rejected task.
  # Supply source-level literature only, so that stale framing cannot leak in.
  fields=['id','title','year','pmid','doi','url','access_level','findings','version_note','aliases']
  common['literature']={'papers':[{k:v for k,v in paper.items() if k in fields} for paper in (literature or {}).get('papers',[])],
                         'limits':(literature or {}).get('limits',[])}
  common['submission']={k:draft.get(k,'') for k in ['title','abstract']}
 else:
  common['literature']=literature or {}
  common['reviewer_research_report']=(literature or {}).get('reviewer_reports',{}).get(reviewer,'')
  common['evidence']=evidence
  common['submission']=draft
  if reviewer=='fact_checker':
   common['primary_evidence']=(literature or {}).get('primary_evidence',{})
   common['literature']={'papers':(literature or {}).get('papers',[]),'limits':(literature or {}).get('limits',[])}
   common.pop('reviewer_research_report',None)
 return common

def draft_checks(draft,evidence,word_limit):
 # Structural validation is owned by the runner; here validate scientific provenance links.
 issues=[]
 if not draft['title'].strip() or not draft['abstract'].strip():issues.append('Empty title or abstract')
 if len(draft['abstract'].split())>word_limit:issues.append('Word limit exceeded')
 if '[' in draft['abstract'] or ']' in draft['abstract']:issues.append('Prospective abstract must state anticipated findings rather than bracketed result placeholders')
 baseline={x['id'] for x in evidence['facts']}
 used=[x['evidence_id'] for x in draft['baseline_use']]
 if set(used)!=baseline or len(used)!=len(baseline):issues.append('Planning sidecar must account for each baseline experiment once; abstract inclusion is optional')
 ids=[x['id'] for x in draft['proposed_experiments']]
 if len(ids)!=len(set(ids)):issues.append('Duplicate proposed experiment IDs')
 experiments={x['id']:x for x in draft['proposed_experiments']}
 if not any(x['priority']=='essential' for x in experiments.values()):issues.append('No essential proposed experiments')
 for x in experiments.values():
  if x['start_month']>x['end_month']:issues.append('Experiment starts after it ends: '+x['id'])
  if any(dep not in experiments for dep in x['dependencies']):issues.append('Unknown dependency: '+x['id'])
  for dep in x['dependencies']:
   if dep in experiments and experiments[dep]['end_month']>x['start_month']:
    issues.append('Prerequisite is not completed by dependent experiment start: '+x['id'])
 def visit(eid,trail):
  if eid in trail:return True
  return any(visit(dep,trail|{eid}) for dep in experiments[eid]['dependencies'] if dep in experiments)
 if any(visit(eid,set()) for eid in experiments):issues.append('Cyclic experiment dependencies')
 anticipated=0
 for claim in draft['claim_map']:
  if any(eid not in baseline for eid in claim['baseline_ids']):issues.append('Invalid baseline ID')
  if any(eid not in experiments for eid in claim['experiment_ids']):issues.append('Invalid experiment ID')
  if claim['status']=='anticipated_result':
   anticipated+=1
   if not claim['experiment_ids']:issues.append('Anticipated claim has no proposed experiment')
   elif not any(experiments.get(eid,{}).get('priority')=='essential' for eid in claim['experiment_ids']):
    issues.append('Central abstract finding depends only on stretch experiments')
  if claim['status']=='existing_result' and not claim['baseline_ids']:issues.append('Existing result lacks baseline provenance')
 if not anticipated:issues.append('No anticipated discovery; a current-data inventory does not meet this task')
 if not draft['core_discovery'].strip():issues.append('No core discovery')
 months=[x['month'] for x in draft['milestones']]
 if months!=[3,6,9,12,15,18,21,24]:issues.append('Require quarterly milestones through month 24, including analysis/repeats/writing')
 return list(dict.fromkeys(issues))

def rank(entries,selection=None,final=False):
 selection=selection or {}
 ranked=[]
 for alias,draft,reviews,issues in entries:
  if len(reviews)!=4:raise ValueError('One Nature reviewer, one fact-checker and two committee reviews are required')
  journal=[reviews[0]];facts=reviews[1];committee=reviews[2:]
  journal_score=sum(sum(r['scores'][k]*v for k,v in JOURNAL_WEIGHTS.items()) for r in journal)/len(journal)
  committee_score=sum(sum(r['scores'][k]*v for k,v in COMMITTEE_WEIGHTS.items()) for r in committee)/2
  journal_pass=all(r['decision']!='reject' and r['scores']['novelty']>=selection.get('min_nature_novelty',4)
    and r['scores']['importance']>=selection.get('min_nature_importance',4) for r in journal)
  feasibility_pass=all(r['decision']!='not_feasible' and r['scores']['feasibility_24_months']>=selection.get('min_committee_feasibility',3)
    and r['scores']['project_quality']>=selection.get('min_committee_quality',3) for r in committee)
  if final:feasibility_pass=feasibility_pass and all(r['decision']=='feasible' and not r['required_scope_cuts'] for r in committee)
  integrity=not issues and not any(r[key] for r in committee for key in ['baseline_contradictions','provenance_errors','unsupported_resource_assumptions'])
  central_checks=[x for x in facts['checks'] if x['centrality']=='central']
  grounded=[x for x in central_checks if x['claim_kind'] in ['literature','novelty']]
  def check_pass(check):
   if check['claim_kind']=='baseline':return check['verdict']=='supported'
   if check['claim_kind']=='prospective':return check['verdict']=='hypothetical_consistent'
   return check['verdict'] in ['supported','hypothetical_consistent'] and bool(check['source_ids']) and bool(check['exact_evidence'].strip())
  # New findings cannot have published proof. Require grounded central literature
  # foundations, while baseline and hypothetical findings retain their own status.
  fact_pass=facts['decision']=='clear' and bool(grounded) and not facts['contradictions'] and all(check_pass(x) for x in central_checks)
  acceptance_pass=integrity and journal_pass and feasibility_pass and fact_pass
  # Early reviews select proposals for scientific redesign, not publication.
  # Repairable planning/provenance problems remain visible to the writer.
  ranked.append({'alias':alias,'eligible':acceptance_pass if final else True,
    'acceptance_pass':acceptance_pass,'eligibility_basis':'final_acceptance' if final else 'revision_shortlist',
    'integrity_pass':integrity,'journal_pass':journal_pass,'feasibility_pass':feasibility_pass,
    'fact_check_pass':fact_pass,'fact_check_decision':facts['decision'],
    'journal_score':round(journal_score,4),'committee_score':round(committee_score,4),
    'score':round((journal_score+committee_score)/2,4),
    'nature_decisions':[r['decision'] for r in journal],'committee_decisions':[r['decision'] for r in committee],
    'local_issues':issues})
 return sorted(ranked,key=lambda x:(not x['eligible'],-min(x['journal_score'],x['committee_score']),-x['score'],x['alias']))

def mock_draft(evidence):
 return {'artifact_status':'hypothetical_end_of_phd_paper','title':'MOCK ONLY: prospective paper',
  'abstract':'MOCK ONLY. We test whether a software cache policy generalizes across held-out workloads. Anticipated controlled benchmarks distinguish policy effects from workload composition. These synthetic statements test the runner and do not establish scientific findings.',
  'core_discovery':'MOCK ONLY anticipated discovery','novelty_vs_prior_work':'MOCK ONLY',
  'baseline_use':[{'evidence_id':x['id'],'role':'supporting','explanation':'MOCK ONLY'} for x in evidence['facts']],
  'claim_map':[{'claim':'MOCK ONLY anticipated benchmark finding','status':'anticipated_result','baseline_ids':[],'experiment_ids':['P1']}],
  'proposed_experiments':[{'id':'P1','question':'MOCK ONLY','approach':'MOCK ONLY','anticipated_result':'MOCK ONLY',
   'alternative_outcome_and_interpretation':'MOCK ONLY','critical_control':'MOCK ONLY','dependencies':[],
   'start_month':1,'end_month':12,'resource_status':'unconfirmed','resource_assumptions':'MOCK ONLY',
   'risk':'medium','fallback':'MOCK ONLY','priority':'essential'}],
  'milestones':[{'month':i,'deliverable':'MOCK ONLY','go_no_go_criterion':'MOCK ONLY'} for i in range(3,25,3)],
  'minimum_publishable_story':'MOCK ONLY','scope_cuts':['MOCK ONLY'],'revision_notes':'Synthetic plumbing test, not a scientific proposal.'}

def mock_review(nature):
 lit=[{'paper_id':pid,'overlap':'MOCK ONLY','implication':'MOCK ONLY'} for pid in ['N1-P1','L-N2-01']]
 if nature:
  return {'decision':'consider_for_review','scores':{k:4 for k in JOURNAL_WEIGHTS},'core_discovery':'MOCK ONLY',
   'publication_case':'MOCK ONLY','story_gaps':[],'finding_whose_loss_collapses_case':'MOCK ONLY',
   'priority_revisions':[],'literature_assessment':lit}
 return {'decision':'feasible','scores':{k:4 for k in COMMITTEE_WEIGHTS},'quality_assessment':'MOCK ONLY',
  'feasibility_assessment':'MOCK ONLY','baseline_contradictions':[],'provenance_errors':[],
  'unsupported_resource_assumptions':[],'critical_path':['MOCK ONLY'],'required_scope_cuts':[],
  'go_no_go_assessment':'MOCK ONLY','minimum_publishable_story':'MOCK ONLY','priority_revisions':[],'literature_assessment':lit}


def mock_fact_review():
 return {'decision':'clear','checks':[{'claim':'MOCK ONLY checked premise','claim_kind':'literature','centrality':'central',
  'verdict':'supported','source_ids':['EXAMPLE-S1'],'exact_evidence':'MOCK ONLY','location':'Figure 3',
  'scope':'MOCK ONLY','correction':''}],'central_assumptions_checked':['MOCK ONLY'],
  'contradictions':[],'missing_primary_sources':[],'priority_revisions':[],
  'literature_assessment':[{'paper_id':x,'overlap':'MOCK ONLY','implication':'MOCK ONLY'} for x in ['N1-P1','N1-P3']]}
