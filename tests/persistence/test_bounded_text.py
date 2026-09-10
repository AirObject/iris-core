"""New bounded text preserves old descriptors and never widens audit content."""
from types import MappingProxyType
import unittest
from companion_memory.persistence import BoundedTextSchema, Field, RecordSchema, ScalarSchema, SequenceSchema
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge, freeze_value
from companion_memory.persistence._codec import schema_value, encode_value
from companion_memory.logging_service import AuditRequirement


class BoundedTextTests(unittest.TestCase):
    def test_exact_utf8_control_characters_and_surrogate_boundaries(self):
        schema=BoundedTextSchema(4)
        for value in ("", "abcd", "😀", "\n\x00\t"):
            self.assertEqual(freeze_value(schema,value),value)
        for value in ("abcde","😀a"):
            with self.assertRaises(ValueTooLarge):
                freeze_value(schema,value)
        with self.assertRaises(InvalidValue):
            freeze_value(schema,"\ud800")
        class Hostile(str):
            def encode(self,*args,**kwargs):
                raise AssertionError("No conversion hook may run.")
        with self.assertRaises(InvalidValue):
            freeze_value(schema,Hostile("a"))

    def test_old_scalar_record_sequence_descriptor_bytes_are_unchanged(self):
        scalar=ScalarSchema("integer",1,5)
        descriptor=schema_value(scalar)
        self.assertNotIn("bounded_text",repr(descriptor))
        # Fixed old bytes are an independent expected wire description.
        self.assertEqual(encode_value(descriptor,4096),b'{"choices":[],"kind":"integer","maximum":5,"minimum":1}')
        self.assertEqual(schema_value(BoundedTextSchema(4)),MappingProxyType({"bounded_text":4}))

    def test_audit_recursively_refuses_text_even_inside_sequences(self):
        for schema in (RecordSchema((Field("body",BoundedTextSchema(10)),)),
                       RecordSchema((Field("nested",SequenceSchema(RecordSchema((Field("body",BoundedTextSchema(10)),)),0,2)),))):
            with self.assertRaises(ValueError):
                AuditRequirement("provider","provider_change","PROVIDER_REQUEST_REGISTERED",1,("REGISTER",),schema)
