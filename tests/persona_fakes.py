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
            'evidence': copy.deepcopy(proofs),
            **quantifier_proof(focus, sentence, payload), **claim_proof(focus)} for focus in sentence['clauses']]
    return output


def claim_proof(focus):
    """Only fill the protocol: the marked focus is one eligible claim. No eligibility is judged."""
    if not focus.get('plan_markers'):
        return {}
    return {'claim_checks': [{'claim': focus['text'].rstrip('。！？!?'), 'claim_type': 'recognition',
                              'eligible': True, 'reason': '假模型只列出命题，不判断资格。'}]}


def quantifier_proof(focus, sentence, payload):
    """Only fill the protocol; like the scope proof, this judges no comparison range.

    A frequency word names the same word of its source when the source has one;
    the fake never invents a stronger source word to pass the strength order.
    """
    if not focus.get('quantifiers'):
        return {}
    sources, excerpts = [], []
    for ref in sentence['evidence_refs']:
        memory = payload['evidence_by_ref'][ref]
        excerpts += [source['excerpt'] for source in memory['excerpts']]
        sources += [source['excerpt'] for source in memory['excerpts']]
        sources.append(memory.get('current_revision_text') or memory.get('content') or '')
    def source_word(word):
        return word if any(word in text for text in sources) else next(
            (match.group() for text in sources for match in [re.search(r'\w', text)] if match), '')
    def item(word):
        value = {'candidate_quantifier': word, 'candidate_scope': '', 'source_quantifier': source_word(word),
                 'source_scopes': [], 'preserved': True}
        if word in focus.get('frequency_words', []):
            value['source_instances'] = max(1, len(excerpts))
        return value
    return {'quantifier_checks': [item(word) for word in focus['quantifiers']]}


def add_scope_proof(row, sentence, payload):
    """Only fill the protocol; this deliberately permissive fake judges no scope."""
    if not sentence.get('scope_check_required') or 'scope_check' in row:
        return
    matches = []
    for ref in sentence['evidence_refs']:
        memory = payload['evidence_by_ref'][ref]
        constraint = memory.get('current_revision_text') or memory.get('content')
        texts = [constraint] if constraint else [source['excerpt'] for source in memory['excerpts']]
        for text in texts:
            for match in SequenceMatcher(None, sentence['text'], text, autojunk=False).get_matching_blocks():
                quote = text[match.b:match.b + match.size]
                if re.search(r'\w', quote):
                    matches.append((len(quote), ref, quote))
    _, ref, quote = max(matches, default=(0, '', ''))
    row['scope_check'] = {'candidate_scope': quote,
        'source_scopes': [{'ref': ref, 'quote': quote}] if quote else [], 'preserved': bool(quote)}
