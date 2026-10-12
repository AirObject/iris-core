"""Protocol-complete fake proofs. These deliberately do not judge entailment."""
import copy
from difflib import SequenceMatcher
import re


def add_clause_proofs(output, payload):
    output = copy.deepcopy(output)
    for row, sentence in zip(output.get('sentences', []), payload['candidate']):
        row.setdefault('persona_kind', 'admin' if sentence['origin']=='admin' else
                       'setting' if sentence['origin']=='initial_template' or sentence.get('initial_setting') else 'episode')
        proofs = []
        for ref in sentence['evidence_refs']:
            memory = payload['evidence_by_ref'][ref]
            if memory['excerpts']:
                proofs.extend({'ref': ref, 'source_index': i, 'quote': source['excerpt']}
                              for i, source in enumerate(memory['excerpts'], 1))
            else:
                proofs.append({'ref': ref, 'source_index': 0, 'quote': memory['content']})
        add_scope_proof(row, sentence, payload)
        row['clauses'] = [{'index': focus['index'], 'supported': row['supported'],
            'scene_qualified': row['scene_qualified'], 'reason': '假模型按本句来源给格式完整的证明。',
            'evidence': copy.deepcopy(proofs)} for focus in sentence['clauses']]
    return output


def add_scope_proof(row, sentence, payload):
    """Only fill the protocol; this deliberately permissive fake judges no scope."""
    if not sentence.get('scope_check_required') or 'scope_check' in row:
        return
    matches = []
    for ref in sentence['evidence_refs']:
        memory = payload['evidence_by_ref'][ref]
        texts = [memory.get('current_revision_text', ''), memory.get('content', ''),
                 *(source['excerpt'] for source in memory['excerpts'])]
        for text in texts:
            for match in SequenceMatcher(None, sentence['text'], text, autojunk=False).get_matching_blocks():
                quote = text[match.b:match.b + match.size]
                if re.search(r'\w', quote):
                    matches.append((len(quote), ref, quote))
    _, ref, quote = max(matches, default=(0, '', ''))
    row['scope_check'] = {'candidate_scope': quote,
        'source_scopes': [{'ref': ref, 'quote': quote}] if quote else [], 'preserved': bool(quote)}
