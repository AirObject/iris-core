"""Abrupt isolated upgrade process termination at explicit durable boundaries."""
import os
import sys
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.runtime.managed_upgrade import CommunicationUpgrade


def main() -> None:
    root, key, target = sys.argv[1:]
    upgrade = CommunicationUpgrade(resolve_deployment({'deployment.data_root': root}))
    def cut(point: str):
        if point == target:
            print({'cut': point, 'exit_code': 86, 'cleanup': 'OS_PROCESS_EXIT'}, flush=True)
            os._exit(86)
    try:
        if target in ('after_backup', 'after_prepare'): upgrade.prepare(key, checkpoint=cut)
        else: upgrade.activate(key, checkpoint=cut)
    finally: upgrade.close()
    raise RuntimeError('The selected interruption point was not reached.')


if __name__ == '__main__': main()
