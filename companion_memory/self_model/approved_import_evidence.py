"""Verify the single approved persona's original review and publication evidence.

The verifier accepts only the frozen original artifacts and preserves the exact
text. A successful verification is evidence, not authority to publish or replace
a persona; the self-model owner must separately check its native import grant.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.content_codec import decode_content

CANDIDATE_ID = 'persona-candidate:3591e66cfa92792b8b7afaf3b19e511e9ed580b9086bf4c744d2e24274aa7772'
CANDIDATE_DIGEST = '2b2ae798d9a5e5f317136e7946eb9f6c28c19c61cee0857871dc6a28af4d796d'
TEXT_DIGEST = '6d8693396cb608748694c8a3e824e1b0233c628beeb752e20c333f6eec36fdec'
TEXT_UTF8_DIGEST = '620673861f22bdf522112beac6e1bc502378b290dffd771dbd031109802334b8'
REVIEW_DIGEST = 'a88b99685ab78c1abca83be9029133b1cc27880950a2fd5f71e15a3245216cc1'
PUBLICATION_DIGEST = '7985604d12ebc67d8daadc3757c83957dabe33b33f06a7f4912eeb943ea87e0f'
RECONCILIATION_DIGEST = 'fc983b18b97548613bf5b69bc8e6f0e1d394905676a6034ad9eacee90e772a59'
_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class ApprovedPersonaEvidence:
    """Immutable verified origin; it contains no model attempt in the new database."""
    original_database_id: str
    original_publication_id: str
    original_generated_at_us: int
    original_candidate_id: str
    original_candidate_revision: int
    original_candidate_digest: str
    original_text_digest: str
    original_text_utf8_digest: str
    review_evidence_digest: str
    approval_ref: str
    text: str
    _issuer: object

    def __init__(self) -> None:
        raise TypeError('Verify the complete original approval evidence.')


def _object(value: object) -> dict:
    if type(value) is dict or type(value) is MappingProxyType:
        return dict(value)
    raise InvalidValue()


def verify_approved_persona(review: bytes, publication: bytes, reconciliation: bytes) -> ApprovedPersonaEvidence:
    """Check all three exact original artifacts and both distinct text hashes.

    No files, credentials, model endpoints or database are read by this function.
    Bad, incomplete or substituted evidence raises InvalidValue with no effect.
    """
    for raw, digest, maximum in ((review,REVIEW_DIGEST,8192),
            (publication,PUBLICATION_DIGEST,32768),(reconciliation,RECONCILIATION_DIGEST,4096)):
        if type(raw) is not bytes or len(raw)>maximum or sha256(raw).hexdigest()!=digest:
            raise InvalidValue()
    try:
        published = _object(decode_content(publication,32768))
        recovered = _object(decode_content(reconciliation,4096))
        original = _object(published['approved_original'])
        candidate = _object(original['candidate'])
        approval = _object(_object(_object(published['user_approval'])['candidates'])['linux'])
        current = _object(published['current_persona'])
        text = candidate['text']
        if type(text) is not str or len(text.encode('utf-8'))!=558:
            raise InvalidValue()
        if (sha256(text.encode('utf-8')).hexdigest()!=TEXT_UTF8_DIGEST
                or sha256(json.dumps(text,ensure_ascii=False).encode('utf-8')).hexdigest()!=TEXT_DIGEST
                or original['candidate_digest']!=CANDIDATE_DIGEST or candidate['object_id']!=CANDIDATE_ID
                or type(candidate['revision']) is not int or candidate['revision']!=1
                or candidate['text_digest']!=TEXT_DIGEST or current['text']!=text or current['review']!='APPROVED'
                or approval['candidate_id']!=CANDIDATE_ID or approval['candidate_digest']!=CANDIDATE_DIGEST
                or approval['revision']!=1 or approval['decision']!='APPROVE'
                or recovered['original_approval']!=approval or recovered['publication_id']!=current['publication_id']
                or recovered['published_result_digest']!=PUBLICATION_DIGEST
                or recovered['candidate_text_unchanged'] is not True or recovered['cleanup_ended'] is not True
                or recovered['recovery_added_attempts']!=0 or published['close_report'] is not True
                or text not in review.decode('utf-8')):
            raise InvalidValue()
        result=object.__new__(ApprovedPersonaEvidence)
        values = dict(original_database_id=published['database_id'],original_publication_id=current['publication_id'],original_generated_at_us=current['generated_at_us'],
            original_candidate_id=CANDIDATE_ID,original_candidate_revision=1,original_candidate_digest=CANDIDATE_DIGEST,
            original_text_digest=TEXT_DIGEST,original_text_utf8_digest=TEXT_UTF8_DIGEST,
            review_evidence_digest=REVIEW_DIGEST,approval_ref=published['user_approval']['approval_ref'],text=text,_issuer=_ISSUER)
        for name,value in values.items():object.__setattr__(result,name,value)
        return result
    except (KeyError,TypeError,ValueError,UnicodeError):
        raise InvalidValue() from None


def evidence_is_native(value: object) -> bool:
    """Verify ownership before the importing transaction consumes the evidence."""
    return type(value) is ApprovedPersonaEvidence and getattr(value,'_issuer',None) is _ISSUER
