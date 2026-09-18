"""Full encoded envelopes, registry vectors and interruption-stable gate clocks."""
from dataclasses import asdict
import unittest
from companion_memory.configuration.communication_schema import VALUES, definitions, validate
from companion_memory.configuration import create_registry_builder, Ok
from companion_memory.management.communication_protocol import SCHEMAS, LIMITS
from companion_memory.management.communication_records import ROUTE, SCOPED_TOKEN
from companion_memory.goals.communication_records import BOUNDS
from companion_memory.goals.grace_clock import GateClock, GraceWindow
from companion_memory.persistence import RecordSchema, SequenceSchema, ScalarSchema, BoundedTextSchema
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import freeze_value


def maximum(schema):
    if type(schema) is RecordSchema:
        return {f.name: maximum(f.schema) for f in schema.fields}
    if type(schema) is SequenceSchema:
        return [maximum(schema.item) for _ in range(schema.maximum)]
    if type(schema) is BoundedTextSchema:
        return '\\' * schema.max_utf8_bytes
    if type(schema) is ScalarSchema:
        if schema.kind == 'identifier': return 'x' * 128
        if schema.kind == 'boolean': return True
        if schema.kind == 'integer': return schema.maximum
        return max(schema.choices, key=len)
    raise AssertionError(schema)


class CommunicationSchemaTests(unittest.TestCase):
    def test_communication_candidate_preserves_birth_and_round_trips(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from tests.managed_runtime.test_native_host import managed_inputs
        from companion_memory.configuration.managed_resolution import resolve_managed_configuration, ManagedConfigurationOk
        from companion_memory.configuration.managed_codec import candidate_values, restore_candidate
        from companion_memory.configuration.communication_configuration import extend_candidate, policy
        with TemporaryDirectory() as directory:
            resolved = resolve_managed_configuration(*managed_inputs(Path(directory)))
            self.assertIs(type(resolved), ManagedConfigurationOk, resolved)
            assert type(resolved) is ManagedConfigurationOk
            birth = resolved.value
            original = candidate_values(birth)
            extended = extend_candidate(birth)
            encoded = candidate_values(extended)
            self.assertEqual(candidate_values(birth), original)
            self.assertEqual(sum(len(d['entries']) for d in encoded['domains']), 140)
            old_entries = {e['parameter_key']: e['body'] for d in original['domains'] for e in d['entries']}
            new_entries = {e['parameter_key']: e['body'] for d in encoded['domains'] for e in d['entries']}
            self.assertTrue(old_entries.items() <= new_entries.items())
            self.assertEqual(candidate_values(restore_candidate(encoded, birth)), encoded)
            self.assertFalse(policy(extended)['communication.ws']['enabled'])

    def test_complete_wire_and_persistent_maxima_fit(self):
        sizes = {}
        for name, schema in SCHEMAS.items():
            frozen = freeze_value(schema, maximum(schema))
            sizes[name] = len(encode_content(frozen, LIMITS[name]))
        for name, schema, limit in [('route', ROUTE, 16384), ('token', SCOPED_TOKEN, 16384),
                *((name, spec[0], spec[1]) for name, spec in BOUNDS.items())]:
            sizes[name] = len(encode_content(freeze_value(schema, maximum(schema)), limit))
        self.assertLess(sizes['goal'], 2048)
        self.assertLess(sizes['ack'], 1024)
        print({'complete_encoded_maxima': sizes})

    def test_native_registry_and_closed_vector(self):
        builder = create_registry_builder()
        for d in definitions():
            self.assertIs(type(builder.register(d)), Ok)
        self.assertIs(type(builder.freeze()), Ok)
        values = {key: dict(value) for key, value in VALUES.items()}
        self.assertFalse(validate(values)['communication.ws']['enabled'])
        values['communication.ws']['connections'] = 17
        with self.assertRaises(Exception): validate(values)
        values['communication.ws']['connections'] = 16
        values['communication.delivery']['sink_mode'] = 'WS'
        with self.assertRaises(ValueError): validate(values)
        values['communication.ws']['enabled'] = True
        self.assertEqual(validate(values)['communication.delivery']['sink_mode'], 'WS')

    def test_overlap_restarts_and_expiry_never_reset_window(self):
        second = 1000000
        gate = GateClock('initial', 1, 0, 0, None, frozenset())
        window = GraceWindow.begin('plan', 'original-config', 0, 300 * second, gate)
        self.assertEqual(window.remaining(gate, 150 * second), (150 * second, 300 * second))
        focus = gate.transition('focus', 100 * second, frozenset({'FOCUS'}))
        both = focus.transition('maintenance', 120 * second, frozenset({'FOCUS', 'MAINTENANCE'}))
        maintenance = both.transition('focus-end', 130 * second, frozenset({'MAINTENANCE'}))
        self.assertEqual(window.remaining(maintenance, 1000 * second), (200 * second, None))
        reopened = maintenance.transition('open', 1100 * second, frozenset())
        recovered = GateClock(**asdict(reopened))
        restored_window = GraceWindow(**asdict(window))
        self.assertEqual(restored_window.remaining(recovered, 1150 * second), (150 * second, 1300 * second))
        late = gate.transition('too-late', 301 * second, frozenset({'FOCUS'}))
        self.assertEqual(window.remaining(late, 400 * second), (0, None))
        resumed = late.transition('open-late', 500 * second, frozenset())
        self.assertEqual(window.remaining(resumed, 500 * second)[0], 0)
        with self.assertRaises(ValueError):
            GraceWindow.begin('plan', 'changed-config', 101 * second, 300 * second, focus)
