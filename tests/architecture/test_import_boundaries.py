"""Enforce import directions and the sole model network outlet without side effects."""
import ast
from pathlib import Path
import sys
import unittest

from .imports import imports
from .rules import ALLOWED, violation

ROOT = Path(__file__).resolve().parents[2]
NETWORK = ('openai', 'anthropic', 'google.genai', 'google.generativeai', 'httpx', 'requests', 'aiohttp', 'socket', 'http.client', 'urllib.request', 'urllib3')
THIRD_PARTY = {'PIL': {'media', 'runtime'}, 'h11': {'management'}, 'wsproto': {'management'}}


class ImportBoundaryTests(unittest.TestCase):
    def test_all_production_imports_follow_declared_edges(self):
        failures = []
        packages = {p.name for p in (ROOT / 'companion_memory').iterdir() if p.is_dir() and (p / '__init__.py').exists()}
        self.assertEqual(packages, set(ALLOWED))
        for path in sorted((ROOT / 'companion_memory').rglob('*.py')):
            if path.parent == ROOT / 'companion_memory':
                continue
            module = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
            if path.name == '__init__.py':
                module = module.removesuffix('.__init__')
            for edge in imports(path.read_text(), module, package=path.name == '__init__.py'):
                issue = violation(edge.source, edge.target)
                if issue:
                    failures.append(f'{path.relative_to(ROOT)}:{edge.line}: {edge.target}: {issue}')
                network = any(edge.target == name or edge.target.startswith(name + '.') for name in NETWORK)
                health_client = module in ('companion_memory.runtime.managed_cli', 'companion_memory.runtime.application.managed.cli') and edge.target.startswith('http.client')
                if network and module.split('.')[1] != 'provider' and not health_client:
                    failures.append(f'{module}:{edge.line}: model-capable client outside Provider: {edge.target}')
                external = edge.target.split('.')[0]
                if external != 'companion_memory' and external not in sys.stdlib_module_names:
                    if module.split('.')[1] not in THIRD_PARTY.get(external, set()):
                        failures.append(f'{module}:{edge.line}: unreviewed third-party dependency: {external}')
            tree = ast.parse(path.read_text())
            aliases = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        aliases[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    for alias in node.names:
                        aliases[alias.asname or alias.name] = (node.module or '') + '.' + alias.name
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = ast.unparse(node.func)
                first, separator, rest = function.partition('.')
                function = aliases.get(first, first) + (separator + rest if separator else '')
                if function in ('asyncio.open_connection', 'asyncio.create_connection'):
                    loopback = module == 'companion_memory.goals.loopback' and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == '127.0.0.1'
                    if module.split('.')[1] != 'provider' and not loopback:
                        failures.append(f'{module}:{node.lineno}: outbound connection outside Provider')
        self.assertEqual(failures, [], '\n'.join(failures))

    def test_scanner_covers_deferred_relative_alias_and_dynamic_imports(self):
        source = '''from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..media import service
def work():
    from companion_memory import media
    import importlib as loader
    loader.import_module("companion_memory.media.service")
    from importlib import import_module as load
    load(".service", package="companion_memory.media")
'''
        edges = imports(source, 'companion_memory.provider.example')
        media = [edge for edge in edges if edge.target.startswith('companion_memory.media')]
        self.assertEqual(len(media), 5)
        self.assertTrue(all(violation(edge.source, edge.target) for edge in media))
        with self.assertRaises(ValueError):
            imports('import importlib\nimportlib.import_module(name)', 'companion_memory.provider.example')

    def test_repaired_directions_and_configuration_limits_cannot_regress(self):
        for source, target in (
            ('provider.service', 'media.service'),
            ('provider.media_input', 'media.daily_image'),
            ('provider.media_input', 'provider.service'),
            ('provider.service', 'contracts.media'),
            ('goals.communication_ledger', 'management.identity'),
            ('memory.service', 'information.records'),
            ('persistence.record_primitives', 'goals.service'),
            ('persistence.service', 'configuration.managed_activation'),
            ('persistence.service', 'configuration.RegistryBuilder'),
        ):
            with self.subTest(source=source, target=target):
                self.assertIsNotNone(violation('companion_memory.' + source, 'companion_memory.' + target))
        self.assertIsNone(violation('companion_memory.persistence.service', 'companion_memory.configuration.EffectiveSnapshot'))
