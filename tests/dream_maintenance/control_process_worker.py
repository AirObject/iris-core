"""Separate interpreter fixture for committed dream control recovery only."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

from companion_memory.persistence import Committed
from .storage_support import ControlStorage


async def run(root: Path, mode: str):
    connects: list[object] = []

    def reject_connect(_socket, address):
        connects.append(address)
        raise AssertionError('Recovery attempted a network connection')

    with patch.object(socket.socket, 'connect', reject_connect):
        fixture = await ControlStorage(root).open(mode)
        if mode == 'CREATE_NEW':
            started = await fixture.control.execute('start_background_dream', 'start', {
                'run_id': 'original-run', 'expected_revision': 1, 'mode_epoch': 1,
                'trigger': 'MANUAL', 'local_date': None}, actor='admin')
            if type(started) is not Committed:
                raise AssertionError(started)
            original = await fixture.control.execute('resume_dream', 'resume', {
                'run_id': 'original-run', 'expected_revision': 1, 'mode_epoch': 1}, actor='admin')
        else:
            original = await fixture.control.confirm('resume_dream', 'resume')
        if type(original) is not Committed:
            raise AssertionError(original)
        value = await fixture.control.inspect('original-run')
        if value is None:
            raise AssertionError('Original committed run missing')
        report = {'process': os.getpid(), 'mode': mode, 'state': value['state'], 'revision': value['revision'],
            'deadline_at_us': value['deadline_at_us'], 'dispatch_enabled': fixture.control.dispatch_enabled,
            'commit_id': original.receipt.commit_id, 'network_connect_attempts': len(connects),
            'scope': 'DREAM_CONTROL_OWNER_ONLY', 'provider_bound': False, 'source': original.source}
        if mode == 'CREATE_NEW':
            print(json.dumps(report, sort_keys=True), flush=True)
            os._exit(23)
        await fixture.close()
        print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
