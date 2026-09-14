"""Human-only proposition annotations and exact per-platform quality calculations.

No model output or deterministic fixture can sign a human review. An empty or
unfinished annotation remains pending, never a passing precision/recall score.
Duplicate predictions occupy the denominator but cannot create another hit.
"""
from __future__ import annotations
from .materials import GOLD,CASES


def annotation_template(platform: str) -> dict:
    if platform not in ('macos','linux'):raise ValueError('Explicit platform required.')
    return {'platform':platform,'reviewed_by':None,'review_ref':None,'package_digest':None,
        'persona':{'candidate_id':None,'candidate_digest':None,'decision':None,'notes':None},
        'batches':[{'name':name,'terminal':None,'raw_output_ref':None,'source_syntax_valid':None,
                   'output_propositions':None,'omissions':None,'world_errors':None,'person_errors':None,
                   'permission_violations':None} for name in CASES]}


def score(review: dict,*,package_digest: str) -> dict:
    """Calculate only complete signed annotations tied to the frozen package.

    Each output row has text, object_ref (nullable for rejected output), match
    (gold ID or null), supported, anchor_supported, and duplicate_of (index/null).
    Raw output propositions include invalid/rejected output whenever recoverable;
    formal-object count is not a substitute for the human proposition count.
    """
    if not review.get('reviewed_by') or not review.get('review_ref') or review.get('package_digest')!=package_digest:
        raise ValueError('Human review of the frozen package is pending.')
    if review.get('platform') not in ('macos','linux') or [b['name'] for b in review['batches']]!=list(CASES):
        raise ValueError('Both platform and all six original batches are required.')
    totals={'output':0,'hits':0,'anchors':0,'world_errors':0,'person_errors':0,'permission_violations':0}
    rows=[]
    for ordinal,batch in enumerate(review['batches']):
        gold={item[0] for item in GOLD if item[1]//2==ordinal}
        predictions=batch['output_propositions'];matches=set();anchors=0
        if type(predictions) is not list or batch['terminal'] not in ('SUCCEEDED','FAILED_DROPPED','REFUSED','UNKNOWN','NOT_SENT'):
            raise ValueError('All original outcomes and propositions need human review.')
        if (type(batch['source_syntax_valid']) is not bool or type(batch['raw_output_ref']) is not str
                or not batch['raw_output_ref']):
            raise ValueError('Original output evidence and a separate syntax check are required.')
        for i,prediction in enumerate(predictions):
            if (set(prediction)!={'text','object_ref','match','supported','anchor_supported','duplicate_of'}
                or not prediction['text'] or type(prediction['supported']) is not bool or type(prediction['anchor_supported']) is not bool):
                raise ValueError('Incomplete proposition annotation.')
            match=prediction['match'];duplicate=prediction['duplicate_of']
            if match is not None and match not in gold:raise ValueError('Match is outside the frozen target set.')
            if duplicate is not None and (type(duplicate) is not int or not 0<=duplicate<i):raise ValueError('Invalid duplicate reference.')
            if prediction['supported'] and match is not None and duplicate is None:matches.add(match)
            anchors+=int(prediction['anchor_supported'])
        if type(batch['omissions']) is not list or set(batch['omissions'])!=gold-matches:
            raise ValueError('Human omissions must retain every unmatched frozen target.')
        for key in ('world_errors','person_errors','permission_violations'):
            if type(batch[key]) is not int or batch[key]<0:raise ValueError('Explicit error counts are required.')
            totals[key]+=batch[key]
        n=len(predictions);hits=len(matches)
        rows.append({'name':batch['name'],'terminal':batch['terminal'],'output':n,'gold':len(gold),'hits':hits,
                     'precision':hits/n if n else None,'recall':hits/len(gold) if gold else None,
                     'anchor_syntax':batch['source_syntax_valid'],'anchor_support':anchors/n if n else None,
                     'false_generation_on_empty_set':n if not gold else 0})
        totals['output']+=n;totals['hits']+=hits;totals['anchors']+=anchors
    n=totals['output'];hits=totals['hits'];total_gold=len(GOLD)
    complete=all(row['terminal'] not in ('UNKNOWN','NOT_SENT') for row in rows)
    passed=(complete and all(row['anchor_syntax'] for row in rows) and n>0 and hits*10>=n*9 and hits*5>=total_gold*4 and totals['anchors']==n
            and totals['permission_violations']==0)
    return {'platform':review['platform'],'counts':totals,'gold':total_gold,'precision':hits/n if n else None,
            'recall':hits/total_gold,'anchor_support':totals['anchors']/n if n else None,
            'batches':rows,'quality_gate_passed':passed,'persona_review':review['persona']}
