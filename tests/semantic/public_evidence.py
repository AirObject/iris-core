"""Optional nonsecret evidence export for the controlled public small profile.

Only this test's owned temporary database and generated fixture wire bodies are
copied. No production configuration, credential source or old trial is read.
"""
import json
import os
from pathlib import Path
import shutil
from companion_memory.persistence.semantic_records import number


def operation_count(host) -> int:
    assert host.admission is not None
    value=host.admission.observe()
    return number(value['normal'])+number(value['completion'])


def save(root:Path,requests:list,steps:list[int],document_count:int,query_count:int) -> None:
    target=os.environ.get('SEMANTIC_TEST_EVIDENCE')
    measurements={'execution':'CONTROLLED_LOOPBACK','real_supplier_sends':0,'documents':document_count,'queries':query_count,
        'observed_http_attempts':len(requests),'per_work_persistent_steps':steps,'maximum_work_steps':max(steps),
        'owned_file_bytes':sum(p.stat().st_size for p in root.rglob('*') if p.is_file()),'unshortened_dispatch_gap_us':30000000}
    print(json.dumps(measurements,sort_keys=True))
    if target is None:return
    destination=Path(target)
    destination.mkdir(mode=0o700,parents=False,exist_ok=False)
    shutil.copytree(root,destination/'closed-instance')
    (destination/'measurements.json').write_text(json.dumps(measurements,sort_keys=True,indent=2))
    (destination/'controlled-requests.json').write_text(json.dumps(requests,ensure_ascii=False,sort_keys=True,indent=2))
