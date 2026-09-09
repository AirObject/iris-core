"""Exercise fixed validators, declared dependencies, and lexical directory isolation.

All resource paths are explicit nonsecret synthetic text. No fixture directory
is created; successful lexical checks do not assert filesystem readiness.
"""

from typing import cast
from unittest.mock import patch

from companion_memory.configuration import (
    Bound, Err, LiteralDefault, MetadataValue, NoDefault, NotApplicable,
    create_registry_builder, resolve_configuration_with_logging_validation,
)
from tests.configuration.logging_support import (
    LoggingResolutionTestCase, SYNTHETIC_LOG_DIRECTORY, SYNTHETIC_MODULES,
    logging_definitions, protected_directories,
)
from tests.configuration.resolution_support import resolution_definition


class LoggingValueTests(LoggingResolutionTestCase):
    """Apply all four constraints to complete base candidates, including defaults."""

    def test_all_modules_and_levels_including_notset_are_accepted_exactly(self):
        registry = self.logging_registry()
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "NOTSET"):
            values: dict[str, MetadataValue] = {module: level for module in SYNTHETIC_MODULES}
            snapshot = self.checked_success(self.checked(registry, {"logging.module_levels": values}))
            self.present(snapshot, "logging.module_levels", values, "EXPLICIT")
        self.checked_success(self.checked(registry))

    def test_module_map_rejects_unknown_nested_null_nontext_and_over_limit_values(self):
        registry = self.logging_registry()
        cases = [{"unknown": "INFO"}, {"Bootstrap": "INFO"}, {"runtime.child": "INFO"},
                 {"bootstrap": "info"}, {"bootstrap": None}, {"bootstrap": {"level": "INFO"}},
                 {"bootstrap": ["INFO"]}, {"bootstrap": 20}, {"bootstrap": True},
                 dict.fromkeys(SYNTHETIC_MODULES + ("extra",), "INFO")]
        for value in cases:
            with self.subTest(value=value):
                self.checked_failure(self.checked(registry, {"logging.module_levels": value}),
                    "ADDITIONAL_VALIDATION_FAILED", "MODULE_LEVELS_INVALID",
                    self.definition_path(registry, "logging.module_levels"))

    def test_reserve_strict_bound_and_rotation_inclusive_bound_when_file_disabled(self):
        registry = self.logging_registry()
        for capacity, reserve, accepted in ((2, 1, True), (128, 128, False), (127, 128, False),
                                            (65536, 65535, True)):
            result = self.checked(registry, {"logging.sink_capacity": capacity,
                "logging.warning_reserve": reserve, "logging.file_enabled": False})
            if accepted:
                self.checked_success(result)
            else:
                self.checked_failure(result, "ADDITIONAL_VALIDATION_FAILED",
                    "RESERVE_NOT_LESS_THAN_CAPACITY", self.definition_path(registry, "logging.warning_reserve"))
        for event, rotation, accepted in ((512, 512, True), (4096, 4096, True),
                                          (4096, 4095, False), (65536, 1073741824, True)):
            result = self.checked(registry, {"logging.event_max_bytes": event,
                "logging.rotation_bytes": rotation, "logging.file_enabled": False})
            if accepted:
                self.checked_success(result)
            else:
                self.checked_failure(result, "ADDITIONAL_VALIDATION_FAILED", "EVENT_EXCEEDS_ROTATION",
                                     self.definition_path(registry, "logging.rotation_bytes"))

    def test_default_candidates_are_validated_against_explicit_dependencies(self):
        registry = self.logging_registry()
        self.checked_failure(self.checked(registry, {"logging.sink_capacity": 128}),
            "ADDITIONAL_VALIDATION_FAILED", "RESERVE_NOT_LESS_THAN_CAPACITY",
            self.definition_path(registry, "logging.warning_reserve"))
        self.checked_failure(self.checked(registry, {"logging.rotation_bytes": 512}),
            "ADDITIONAL_VALIDATION_FAILED", "EVENT_EXCEEDS_ROTATION",
            self.definition_path(registry, "logging.rotation_bytes"))
        with patch("companion_memory.configuration.logging_validation._validate_module_levels",
                   return_value="MODULE_LEVELS_INVALID") as validator:
            self.checked_failure(self.checked(registry), "ADDITIONAL_VALIDATION_FAILED", "MODULE_LEVELS_INVALID",
                                 self.definition_path(registry, "logging.module_levels"))
        self.assertEqual(validator.call_args.args[0], {})

    def test_every_numeric_range_and_boolean_type_is_checked_with_file_disabled(self):
        registry = self.logging_registry()
        for item in logging_definitions():
            if item["type"] == "integer":
                limits = self.declared(item["range"])
                self.assertIs(type(limits.lower), Bound)
                self.assertIs(type(limits.upper), Bound)
                bad_values = (cast(Bound, limits.lower).value - 1, cast(Bound, limits.upper).value + 1)
                reason = "OUT_OF_RANGE"
            elif item["type"] == "boolean":
                bad_values, reason = (0, 1), "TYPE_MISMATCH"
            else:
                continue
            for value in bad_values:
                self.checked_failure(self.checked(registry, {"logging.file_enabled": False, item["key"]: value}),
                    "INVALID_CONFIGURATION_VALUE", reason, self.definition_path(registry, item["key"]))

    def test_file_directory_stays_required_even_if_output_is_disabled(self):
        registry = self.logging_registry()
        self.checked_failure(resolve_configuration_with_logging_validation(registry,
            {"logging.file_enabled": False}, protected_directories()), "REQUIRED_VALUE_MISSING",
            "MISSING_REQUIRED", self.definition_path(registry, "logging.file_directory"))

    def test_timeout_values_have_no_unapproved_cross_parameter_relation(self):
        registry = self.logging_registry()
        self.checked_success(self.checked(registry, {"logging.io_timeout_ms": 60000,
            "logging.flush_timeout_ms": 1, "logging.close_timeout_ms": 1, "logging.probe_interval_ms": 1}))


class LoggingDependencyTests(LoggingResolutionTestCase):
    """Dependencies require presence but do not compute or order candidate values."""

    def test_extra_dependencies_forward_self_cycles_and_no_validator_are_allowed(self):
        first = resolution_definition(key="a", dependencies=["b", "a"], default=LiteralDefault("alpha"))
        second = resolution_definition(key="b", dependencies=["a"], default=LiteralDefault("beta"))
        registry = self.logging_registry({
            "logging.module_levels": {"dependencies": ["b", "logging.module_levels"]},
            "logging.console_enabled": {"dependencies": ["logging.file_enabled"]},
            "logging.file_enabled": {"dependencies": ["logging.console_enabled"]},
            "logging.warning_reserve": {"dependencies": ["b", "logging.sink_capacity", "a"]},
        }, extras=(second, first))
        snapshot = self.checked_success(self.checked(registry))
        self.present(snapshot, "a", "alpha", "DEFAULT")
        self.present(snapshot, "b", "beta", "DEFAULT")

    def test_optional_dependency_missing_uses_declaration_order_and_required_missing_precedes_it(self):
        extras = (
            resolution_definition(key="a", dependencies=["z", "b"], default=LiteralDefault("alpha")),
            resolution_definition(key="b", required=False), resolution_definition(key="z", required=False),
        )
        registry = self.logging_registry(extras=extras)
        self.checked_failure(self.checked(registry), "ADDITIONAL_VALIDATION_FAILED", "DEPENDENCY_VALUE_MISSING",
                             ("definitions", 0, "dependencies", 0))
        registry = self.logging_registry(extras=(*extras[:2], resolution_definition(key="z")))
        self.checked_failure(self.checked(registry), "REQUIRED_VALUE_MISSING", "MISSING_REQUIRED",
                             self.definition_path(registry, "z"))

    def test_optional_null_dependency_is_present_without_derived_constraints(self):
        extra = resolution_definition(key="extra", nullable=True, required=False,
            enum=NotApplicable("No enum."), default=LiteralDefault(None), dependencies=["extra"])
        registry = self.logging_registry({"logging.rotation_bytes": {
            "dependencies": ["extra", "logging.event_max_bytes"]}}, extras=(extra,))
        snapshot = self.checked_success(self.checked(registry))
        self.present(snapshot, "extra", None, "DEFAULT")

    def test_missing_extra_dependency_of_validator_is_not_skipped(self):
        extra = resolution_definition(key="extra", required=False)
        registry = self.logging_registry({"logging.module_levels": {"dependencies": ["extra"]}}, extras=(extra,))
        self.checked_failure(self.checked(registry), "ADDITIONAL_VALIDATION_FAILED", "DEPENDENCY_VALUE_MISSING",
                             self.definition_path(registry, "logging.module_levels", "dependencies", (0,)))

    def test_unknown_dependency_still_fails_registry_freeze(self):
        builder = create_registry_builder()
        for item in logging_definitions():
            if item["key"] == "logging.module_levels":
                item["dependencies"] = ["unknown"]
            self.success(builder.register(item))
        result = builder.freeze()
        self.assertIs(type(result), Err)
        error = cast(Err, result).error
        self.assertEqual(error.code, "UNRESOLVED_DEPENDENCY")
        self.assertEqual(error.issues[0].reason, "MISSING_DEPENDENCY")
        self.success(builder.register(resolution_definition(key="unknown", required=False)))
        self.success(builder.freeze())

    def test_optional_validated_logging_definition_is_rejected_by_schema_first(self):
        registry = self.logging_registry({"logging.module_levels": {"required": False, "default": NoDefault()}})
        self.checked_failure(self.checked(registry), "INVALID_LOGGING_SCHEMA", "LOGGING_DEFINITION_MISMATCH",
                             self.schema_path("logging.module_levels"))


class LoggingDirectoryTests(LoggingResolutionTestCase):
    """Canonical POSIX text and component overlap checks require no resource access."""

    def test_directory_context_requires_exact_complete_nonempty_five_category_shape(self):
        registry = self.logging_registry()
        cases: list[tuple[object, tuple[str | int, ...]]] = [(None, ()), ([], ()), ({}, ())]
        for category in protected_directories():
            missing = protected_directories()
            del missing[category]
            cases.append((missing, ()))
            for value in ([], (), None, "/synthetic-public/directory", {"directory"}):
                cases.append((dict(protected_directories(), **{category: value}), (category,)))
            cases.append((dict(protected_directories(), **{category: [1]}), (category, 0)))
        cases.append((dict(protected_directories(), unknown=[]), ()))
        # This table intentionally passes malformed directory carriers to the public API.
        for context, suffix in cases:
            self.checked_failure(resolve_configuration_with_logging_validation(
                registry, {}, cast(dict[str, list[str] | tuple[str, ...]], context)),
                "INVALID_VALIDATION_CONTEXT", "INVALID_SHAPE", ("protected_directories",) + suffix)

    def test_all_directory_text_uses_canonical_absolute_syntax_and_control_limits(self):
        registry = self.logging_registry()
        invalid = ("", "relative", "~/logs", "$HOME/logs", "https://example.test/logs", "//logs",
                   "/logs/", "/logs//child", "/logs/./child", "/logs/../child",
                   "/logs\x00child", "/logs\nchild", "/logs\x7fchild", "/logs\x85child", "/" + "a" * 4096)
        for text in invalid:
            self.checked_failure(self.checked(registry, {"logging.file_directory": text}),
                "ADDITIONAL_VALIDATION_FAILED", "PATH_SYNTAX_INVALID",
                self.definition_path(registry, "logging.file_directory"))
            for category in protected_directories():
                context = protected_directories()
                context[category] = [text]
                self.checked_failure(resolve_configuration_with_logging_validation(registry, {}, context),
                    "INVALID_VALIDATION_CONTEXT", "PATH_SYNTAX_INVALID", ("protected_directories", category, 0))

    def test_equal_ancestor_descendant_overlap_and_similar_prefixes(self):
        registry = self.logging_registry()
        for category in protected_directories():
            context = protected_directories()
            context[category] = ["/isolated/media"]
            for path, accepted in (("/isolated/media", False), ("/isolated", False),
                                   ("/isolated/media/logs", False), ("/isolated/media2", True),
                                   ("/isolated/med", True), ("/isolated/Media", True)):
                result = resolve_configuration_with_logging_validation(registry, {"logging.file_directory": path}, context)
                if accepted:
                    self.checked_success(result)
                else:
                    self.checked_failure(result, "ADDITIONAL_VALIDATION_FAILED", "PATH_OVERLAP",
                                         self.definition_path(registry, "logging.file_directory"))

    def test_root_protected_directory_overlaps_every_log_path_and_log_root_is_invalid(self):
        registry = self.logging_registry()
        context = protected_directories()
        context["backup"] = ["/"]
        self.checked_failure(resolve_configuration_with_logging_validation(registry,
            {"logging.file_directory": SYNTHETIC_LOG_DIRECTORY}, context), "ADDITIONAL_VALIDATION_FAILED",
            "PATH_OVERLAP", self.definition_path(registry, "logging.file_directory"))
        self.checked_failure(self.checked(registry, {"logging.file_directory": "/"}),
            "ADDITIONAL_VALIDATION_FAILED", "PATH_SYNTAX_INVALID",
            self.definition_path(registry, "logging.file_directory"))

    def test_character_limit_unicode_and_literal_components_do_not_normalize_or_expand(self):
        registry = self.logging_registry()
        for path in ("/" + "界" * 4095, "/synthetic-public/é", "/synthetic-public/e\u0301",
                     "/synthetic-public/$HOME", "/synthetic-public/~", "/synthetic-public/log name"):
            snapshot = self.checked_success(self.checked(registry, {"logging.file_directory": path}))
            self.present(snapshot, "logging.file_directory", path, "EXPLICIT")
        context: dict[str, list[str] | tuple[str, ...]] = {key: tuple(paths) for key, paths in protected_directories().items()}
        self.checked_success(resolve_configuration_with_logging_validation(registry,
            {"logging.file_directory": SYNTHETIC_LOG_DIRECTORY}, context))

    def test_directory_errors_use_fixed_category_order_then_member_order(self):
        registry = self.logging_registry()
        context = dict(reversed(tuple(protected_directories().items())))
        context["backup"] = ["bad"]
        # Preserve the invalid later member to verify the earlier syntax error wins.
        context["media"] = cast(list[str], ["/valid", "bad", None])
        self.checked_failure(resolve_configuration_with_logging_validation(registry, {}, context),
            "INVALID_VALIDATION_CONTEXT", "PATH_SYNTAX_INVALID", ("protected_directories", "media", 1))
