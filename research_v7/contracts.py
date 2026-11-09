import json
from prospective import obj,arr,string,integer,STR,STRS,COMMITTEE_SCHEMA,DRAFT_SCHEMA

SEARCH_SCHEMA=obj({'queries':arr(obj({'query':STR,'purpose':STR}))})
BRIEF_SCHEMA=obj({'question':STR,'known_findings':STR,
 'closest_studies':arr(obj({'source_id':STR,'established_result':STR,'difference_from_proposal':STR})),
 'proposed_advance':STR,'decisive_experiment':STR,'competing_explanation':STR,
 'resources':arr(obj({'item':STR,'status':string('confirmed','unconfirmed','new_development'),'validation_gate':STR})),
 'open_questions':arr(obj({'question':STR,'owner':string('retrieval','lab_user','writer','pilot'),'next_action':STR})),
 'novelty_status':string('provisional','supported_within_search','overlap_requires_pivot'), 'search_scope':STR})
BRIEF_REVIEW_SCHEMA=obj({'decision':string('ready','needs_evidence','needs_lab_info','pivot'),
 'issues':STRS,'queries':arr(obj({'query':STR,'purpose':STR})),
 'lab_questions':STRS,'required_changes':STRS})
TECH_SCHEMA=obj({'decision':string('pass','revise'),
 'issues':arr(obj({'code':string('gap','main_finding','support','significance','language','word_count','provenance','causal_overclaim'),
                  'location':STR,'problem':STR,'requested_fix':STR,'severity':string('blocking','suggestion')})),
 'summary':STR})
EDIT_SCHEMA=obj({'title':STR,'abstract':STR,'revision_notes':STR,'claim_map':json.loads(json.dumps(DRAFT_SCHEMA['properties']['claim_map']))})
IMPROVEMENT_SCHEMA=obj({'substantial':{'type':'boolean'},
 'criteria':arr(obj({'criterion_id':STR,'met':{'type':'boolean'},'evidence_quote':STR,'reason':STR})),
 'scientific_change':STR,'remaining_weaknesses':STRS})
# The existing lightweight validator intentionally supports booleans below via v7
# validate_extra; provider schemas enforce these types too.
FINAL_SCHEMA=obj({'preferred':string('A','B','C','none'),
 'ranking':STRS,'importance_comparison':STR,'evidence_comparison':STR,
 'feasibility_comparison':STR,'uncertainties':STRS})
ISSUE_UPDATE_SCHEMA=obj({'assessments':arr(obj({'issue_id':STR,
 'status':string('addressed_in_plan','evidence_added','unresolved','out_of_scope'),
 'new_submission_quote':STR,'source_id':STR,'reason':STR}))})
COMMITTEE_V7=json.loads(json.dumps(COMMITTEE_SCHEMA))
COMMITTEE_V7['properties']['scope_actions']=arr(obj({'action':STR,
 'kind':string('keep_existing_exclusion','new_required_cut','validation_gate'),
 'blocking':{'type':'boolean'},'reason':STR}))
COMMITTEE_V7['required']=list(COMMITTEE_V7['properties'])

SEARCH_PROMPT='Propose at most two focused PubMed queries for the scientific question and unresolved evidence below. Seek closest prior experiments and contrary controls, not generic reviews. Do not invent search results.'
BRIEF_PROMPT='Before drafting, defend a specific two-year research question. Cite at least three relevant supplied papers and their source IDs; do not invent sources or claim an exhaustive novelty proof. Separate established results, your proposed advance, a decisive discriminator and a competing explanation. Record unconfirmed lab resources as gates, not as existing assets. Identify each open question and who can resolve it. Change direction if prior work removes the proposed advance.'
BRIEF_REVIEW_PROMPT='Challenge the research brief before abstract writing. Check its closest prior work, exact advance, decisive experiment and resource stack. Use only supplied passages. Return targeted queries or specific lab questions for unresolved evidence. A bounded novelty search supports provisional novelty, not proof of universal absence. Do not demand that future experiments already be completed.'
TECH_PROMPT='You are a Luna structure editor, not a prestige or literature-novelty judge. Check the abstract for a specific gap, clear main answer, supporting findings, concrete significance, readability and internal consistency. Maximum 200 words, no references. Methods should clarify evidence, not become an inventory. The paper is hypothetical: future findings may read as results, with provenance outside the abstract. Cut filler, never uncertainty or causal limits. Return specific locations and actionable fixes, at most four blocking issues. Do not demand a signaling mechanism by reflex.'
EDIT_PROMPT='You are the same writer correcting structure. Revise only title and abstract using the supplied editor feedback. Preserve the central scientific hypothesis and the findings supported by the supplied plan. Add no new experiment, claim, effect size, source or completed student result. At most 200 words, one readable paragraph. Return the complete edited text, not a patch.'
IMPROVEMENT_PROMPT='Assess whether the warned writer made substantive scientific improvement against the explicit warning criteria. Wording changes, extra length and higher scores alone do not count. Look for a resolved scientific weakness, improved discriminator, valid prior-art distinction or materially more feasible plan. Supply exact quotations from the new submission for satisfied criteria. A substantial=true verdict requires at least one genuinely substantive criterion met. Do not see or speculate about competing proposals. This is a hypothetical finished paper with two years of future work: assess the proposed validation and anticipated findings, never require future experiments to have already produced real results. Formatting, missing fields and unverified quotations require a returned correction, not a negative scientific grade. Preserve unfavorable scientific judgments when repairing format.'

PROMPTS={'search':SEARCH_PROMPT,'brief':BRIEF_PROMPT,'brief_review':BRIEF_REVIEW_PROMPT,
 'technical':TECH_PROMPT,'edit':EDIT_PROMPT,'improvement':IMPROVEMENT_PROMPT,'issue_update':'Assess the supplied prior issues against this writer only. A rewritten plan can address a design problem but cannot prove a factual premise. Lab records cannot be invented. For addressed_in_plan/out_of_scope provide an exact new-submission quote. For evidence_added cite a source explicitly supported by the current fact-check. Otherwise keep unresolved. Do not close an issue merely because a later reviewer omitted it.'}
SCHEMAS={'search':SEARCH_SCHEMA,'brief':BRIEF_SCHEMA,'brief_review':BRIEF_REVIEW_SCHEMA,
 'technical':TECH_SCHEMA,'edit':EDIT_SCHEMA,'improvement':IMPROVEMENT_SCHEMA,'final':FINAL_SCHEMA,'committee':COMMITTEE_V7,'issue_update':ISSUE_UPDATE_SCHEMA}

def validate_extra(value,schema):
 from tournament import validate
 validate(value,schema)
 def walk(v,s):
  if s['type']=='boolean' and type(v) is not bool:raise ValueError('Expected boolean')
  if s['type']=='object':
   for key,sub in s['properties'].items():walk(v[key],sub)
  elif s['type']=='array':
   for item in v:walk(item,s['items'])
 walk(value,schema)
