"""Read-only real-send preflight with explicit nonsecret material authorization.

This entry reports every current blocker before credential resolution or any
model attempt. Protocol and final native launch requirements remain visible; a changed key
or an unsigned JSON file cannot establish strict structured-output support.
"""
from __future__ import annotations
import argparse
from pathlib import Path
from .files import digest,read_json
from .prepare import BLOCKERS,MATERIAL_APPROVAL


def inspect(package_path: Path,approval_path: Path|None=None) -> dict:
    packet=read_json(package_path)
    if type(packet) is not dict:raise ValueError('A complete review package is required.')
    checksum=digest(packet);approved=False
    if approval_path is not None:
        approval=read_json(approval_path,16384)
        approved=(type(approval) is dict and approval.get('package_digest')==checksum
                  and approval.get('materials_approved') is True and bool(approval.get('approved_by'))
                  and bool(approval.get('approval_ref')))
    missing=[item for item in BLOCKERS if not (approved and item==MATERIAL_APPROVAL)]
    return {'package_digest':checksum,'materials_approved':approved,'ready_to_send':False,
            'credential_resolutions':0,'provider_requests':0,'blocking_conditions':missing}


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package',type=Path);parser.add_argument('--approval',type=Path)
    args=parser.parse_args()
    import json
    print(json.dumps(inspect(args.package,args.approval),ensure_ascii=False,indent=2))
    raise SystemExit(2)

if __name__=='__main__':main()
