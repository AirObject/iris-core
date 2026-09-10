"""Disposable child process with explicit barriers around durable provider effects.

The parent owns the retained database identity and kills this interpreter only
once the requested boundary is reached. No remote resource or production path is
used; barriers and simulation results are independently reported.
"""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
from companion_memory.provider import Completed, Pending, Ready, Scenario
from tests.provider.support import Fixture, completed, found, record, records, success, until


def barrier() -> None:
    sys.stdout.write("PROVIDER_BARRIER\n")
    sys.stdout.flush()
    sys.stdin.buffer.readline()


async def main(directory: Path, action: str):
    started,release=threading.Event(),threading.Event()
    first=Scenario("TRANSIENT_FAILURE",None,{"coverage":"COMPLETE","billing_input_units":10,"billing_output_units":0}) if action=="failed_before_retry" else success()
    scenario=replace(first,started=started,release=release) if action=="adapter_started" else first
    f=Fixture(directory,(scenario,success()))
    result=await f.initialize("OPEN_EXISTING")
    assert type(result) is Ready,result
    if action=="inspect":
        result=await f.work.generate(f.request())
        if type(result) is Completed:
            observed=result.record
            text=None if result.result is None else result.result.get("text")
        else:
            assert type(result) is Pending,result
            observed=record(record(found(await f.observer.get_request(result.reference["request_id"])))["request"])
            text=None
        budget=records(found(await f.observer.get_budget_state()))[0]
        print(json.dumps({"phase":observed["phase"],"outcome":observed["outcome"],"attempts":observed["attempt_count"],
                          "cost":budget["known_subtotal_atoms"],"held":budget["held_atoms"],"calls":len(f.adapter.calls),"text":text}))
        await f.close()
        return
    commits=0
    writing=False
    def after(sql):
        nonlocal commits,writing
        if sql.startswith(("INSERT INTO provider_requests","UPDATE provider_requests")):
            writing=True
        if sql=="COMMIT" and writing:
            writing=False
            commits+=1
            if action=="prepared_commit" and commits==1 or action in ("completed_commit","failed_before_retry") and commits==2:
                barrier()
    def before(sql):
        if action=="result_received" and sql.startswith("UPDATE provider_requests"):
            barrier()
    f.hooks.after=after;f.hooks.before=before
    task=asyncio.create_task(f.work.generate(f.request()))
    if action=="adapter_started":
        await until(started.is_set)
        barrier()
    await task
    raise AssertionError("The requested crash barrier was not reached.")


if __name__=="__main__":
    asyncio.run(main(Path(sys.argv[1]),sys.argv[2]))
