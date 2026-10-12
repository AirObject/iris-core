"""Protocol-complete fake proofs. These deliberately do not judge entailment."""
import copy


def add_clause_proofs(output, payload):
    output = copy.deepcopy(output)
    for row, sentence in zip(output.get('sentences', []), payload['candidate']):
        proofs = []
        for ref in sentence['evidence_refs']:
            memory = payload['evidence_by_ref'][ref]
            if memory['excerpts']:
                proofs.extend({'ref': ref, 'source_index': i, 'quote': source['excerpt']}
                              for i, source in enumerate(memory['excerpts'], 1))
            else:
                proofs.append({'ref': ref, 'source_index': 0, 'quote': memory['content']})
        row['clauses'] = [{'index': focus['index'], 'supported': row['supported'],
            'scene_qualified': row['scene_qualified'], 'reason': '假模型按本句来源给格式完整的证明。',
            'evidence': copy.deepcopy(proofs)} for focus in sentence['clauses']]
    return output
