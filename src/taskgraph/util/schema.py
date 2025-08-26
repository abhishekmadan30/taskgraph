# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.


import pprint
from typing import List

import msgspec

import taskgraph
from taskgraph.util.keyed_by import evaluate_keyed_by, iter_dot_path


class Any:
    """Validator that accepts any of the provided values."""

    def __init__(self, *validators):
        self.validators = validators

    def __call__(self, value):
        for validator in self.validators:
            if validator == value or (callable(validator) and validator(value)):
                return value
        raise ValueError(f"Value {value} not in allowed values: {self.validators}")


class Required:
    """Marks a field as required in a schema."""

    def __init__(self, key):
        self.key = key
        self.schema = key  # For compatibility


class Optional:
    """Marks a field as optional in a schema."""

    def __init__(self, key):
        self.key = key
        self.schema = key  # For compatibility


def validate_schema(schema, obj, msg_prefix, use_msgspec=False):
    """
    Validate that object satisfies schema.  If not, generate a useful exception
    beginning with msg_prefix.

    Args:
        schema: Either a Schema instance or msgspec.Struct type
        obj: Object to validate
        msg_prefix: Prefix for error messages
        use_msgspec: If True, use msgspec for validation (default: False)
    """
    if taskgraph.fast:
        return

    # Handle Schema instances
    if isinstance(schema, Schema):
        try:
            schema(obj)
        except Exception as exc:
            raise Exception(f"{msg_prefix}\n{exc}\n{pprint.pformat(obj)}")
        return

    # Auto-detect msgspec schemas
    if isinstance(schema, type) and issubclass(schema, msgspec.Struct):
        use_msgspec = True

    if use_msgspec:
        # Handle msgspec validation
        try:
            if isinstance(schema, type) and issubclass(schema, msgspec.Struct):
                # For msgspec.Struct types, validate by converting
                msgspec.convert(obj, schema)
            else:
                # For other msgspec validators
                schema.decode(msgspec.json.encode(obj))
        except (msgspec.ValidationError, msgspec.DecodeError) as exc:
            msg = [msg_prefix, str(exc)]
            raise Exception("\n".join(msg) + "\n" + pprint.pformat(obj))
    else:
        # Try to call the schema as a validator
        try:
            schema(obj)
        except Exception as exc:
            raise Exception(f"{msg_prefix}\n{exc}\n{pprint.pformat(obj)}")


def optionally_keyed_by(*arguments):
    """
    Mark a schema value as optionally keyed by any of a number of fields.  The
    schema is the last argument, and the remaining fields are taken to be the
    field names.  For example:

        'some-value': optionally_keyed_by(
            'test-platform', 'build-platform',
            Any('a', 'b', 'c'))

    The resulting schema will allow nesting of `by-test-platform` and
    `by-build-platform` in either order.
    """
    schema = arguments[-1]
    fields = arguments[:-1]

    def validator(obj):
        if isinstance(obj, dict) and len(obj) == 1:
            k, v = list(obj.items())[0]
            if k.startswith("by-") and k[len("by-") :] in fields:
                res = {}
                for kk, vv in v.items():
                    try:
                        res[kk] = validator(vv)
                    except Exception as e:
                        if hasattr(e, "prepend"):
                            e.prepend([k, kk])
                        raise
                return res
            elif k.startswith("by-"):
                # Unknown by-field
                raise ValueError(f"Unknown key {k}")
        # Validate against the schema
        if isinstance(schema, Schema):
            return schema(obj)
        elif schema is str:
            # String validation
            if not isinstance(obj, str):
                raise TypeError(f"Expected string, got {type(obj).__name__}")
            return obj
        elif schema is int:
            # Int validation
            if not isinstance(obj, int):
                raise TypeError(f"Expected int, got {type(obj).__name__}")
            return obj
        elif isinstance(schema, type):
            # Type validation for built-in types
            if not isinstance(obj, schema):
                raise TypeError(f"Expected {schema.__name__}, got {type(obj).__name__}")
            return obj
        elif callable(schema):
            # Other callable validators
            try:
                return schema(obj)
            except:
                raise
        else:
            # Simple type validation
            if not isinstance(obj, schema):
                raise TypeError(
                    f"Expected {getattr(schema, '__name__', str(schema))}, got {type(obj).__name__}"
                )
            return obj

    # set to assist autodoc
    setattr(validator, "schema", schema)
    setattr(validator, "fields", fields)
    return validator


def resolve_keyed_by(
    item, field, item_name, defer=None, enforce_single_match=True, **extra_values
):
    """
    For values which can either accept a literal value, or be keyed by some
    other attribute of the item, perform that lookup and replacement in-place
    (modifying `item` directly).  The field is specified using dotted notation
    to traverse dictionaries.

    For example, given item::

        task:
            test-platform: linux128
            chunks:
                by-test-platform:
                    macosx-10.11/debug: 13
                    win.*: 6
                    default: 12

    a call to `resolve_keyed_by(item, 'task.chunks', item['thing-name'])`
    would mutate item in-place to::

        task:
            test-platform: linux128
            chunks: 12

    The `item_name` parameter is used to generate useful error messages.

    If extra_values are supplied, they represent additional values available
    for reference from by-<field>.

    Items can be nested as deeply as the schema will allow::

        chunks:
            by-test-platform:
                win.*:
                    by-project:
                        ash: ..
                        cedar: ..
                linux: 13
                default: 12

    Args:
        item (dict): Object being evaluated.
        field (str): Name of the key to perform evaluation on.
        item_name (str): Used to generate useful error messages.
        defer (list):
            Allows evaluating a by-* entry at a later time. In the example
            above it's possible that the project attribute hasn't been set yet,
            in which case we'd want to stop before resolving that subkey and
            then call this function again later. This can be accomplished by
            setting `defer=["project"]` in this example.
        enforce_single_match (bool):
            If True (default), each task may only match a single arm of the
            evaluation.
        extra_values (kwargs):
            If supplied, represent additional values available
            for reference from by-<field>.

    Returns:
        dict: item which has also been modified in-place.
    """
    for container, subfield in iter_dot_path(item, field):
        container[subfield] = evaluate_keyed_by(
            value=container[subfield],
            item_name=f"`{field}` in `{item_name}`",
            defer=defer,
            enforce_single_match=enforce_single_match,
            attributes=dict(item, **extra_values),
        )

    return item


# Schemas for YAML files should use dashed identifiers by default.  If there are
# components of the schema for which there is a good reason to use another format,
# they can be excepted here.
EXCEPTED_SCHEMA_IDENTIFIERS = [
    # upstream-artifacts and artifact-map are handed directly to scriptWorker,
    # which expects interCaps
    "upstream-artifacts",
    "artifact-map",
]


class Schema:
    """
    A schema validator that wraps msgspec.Struct types.

    This provides a consistent interface for schema validation across the codebase.
    """

    def __init__(self, schema, check=True, **kwargs):
        # Check if schema is a msgspec.Struct type
        if isinstance(schema, type) and issubclass(schema, msgspec.Struct):
            self._msgspec_schema = schema
            self._is_msgspec = True
        elif isinstance(schema, dict):
            # Legacy dict schema - convert to a simple validator
            self._msgspec_schema = None
            self._is_msgspec = False
            self._dict_schema = schema
        else:
            # Assume it's a callable validator
            self._msgspec_schema = None
            self._is_msgspec = False
            self._validator = schema

        self.check = check
        self.schema = schema  # Store original schema for compatibility
        self._extensions = []
        self.allow_extra = False  # By default, don't allow extra keys

    def extend(self, *args, **kwargs):
        """Extend the schema. For msgspec schemas, this stores extensions separately."""
        if self._is_msgspec:
            # For msgspec schemas, store extensions for validation time
            self._extensions.extend(args)
            return self
        elif hasattr(self, "_dict_schema"):
            # For dict schemas, create a new Schema with the combined schemas
            new_schema = self._dict_schema.copy()
            for arg in args:
                if isinstance(arg, dict):
                    new_schema.update(arg)
            # Handle extra parameter
            allow_extra = kwargs.get("extra") is not None
            new_instance = Schema(new_schema)
            new_instance.allow_extra = allow_extra
            return new_instance
        # For other schemas, just return self
        return self

    def _validate_msgspec(self, data):
        """Validate data against the msgspec schema."""
        try:
            return msgspec.convert(data, self._msgspec_schema)
        except (msgspec.ValidationError, msgspec.DecodeError) as e:
            raise Exception(str(e))

    def __call__(self, data):
        """Validate data against the schema."""
        if taskgraph.fast:
            return data

        if self._is_msgspec:
            return self._validate_msgspec(data)
        elif hasattr(self, "_dict_schema"):
            # Simple dict validation
            if not isinstance(data, dict):
                raise Exception(f"Expected dict, got {type(data).__name__}")

            # Collect valid keys
            valid_keys = set()
            for key in self._dict_schema.keys():
                if hasattr(key, "key"):
                    valid_keys.add(key.key)
                else:
                    valid_keys.add(key)

            # Check for extra keys (strict mode by default for dict schemas)
            extra_keys = set(data.keys()) - valid_keys
            if extra_keys and not getattr(self, "allow_extra", False):
                raise Exception(f"Extra keys not allowed: {extra_keys}")

            # Validate required keys and values
            for key, validator in self._dict_schema.items():
                # Handle Required/Optional keys
                if hasattr(key, "key"):
                    actual_key = key.key
                    is_required = isinstance(key, Required)
                else:
                    actual_key = key
                    is_required = True

                if actual_key in data:
                    value = data[actual_key]
                    # Validate the value
                    if validator is int and not isinstance(value, int):
                        raise Exception(
                            f"Key {actual_key}: Expected int, got {type(value).__name__}"
                        )
                    elif validator is str and not isinstance(value, str):
                        raise Exception(
                            f"Key {actual_key}: Expected str, got {type(value).__name__}"
                        )
                elif is_required:
                    raise Exception(f"Missing required key: {actual_key}")
            return data
        elif hasattr(self, "_validator"):
            return self._validator(data)
        return data

    def __getitem__(self, item):
        if self._is_msgspec:
            # For msgspec schemas, provide backward compatibility
            # by returning appropriate validators for known fields
            # This is a workaround to support legacy code that accesses schema fields
            field_validators = {
                "description": str,
                "priority": Any(
                    "highest",
                    "very-high",
                    "high",
                    "medium",
                    "low",
                    "very-low",
                    "lowest",
                ),
                "attributes": {str: object},
                "task-from": str,
                "dependencies": {str: object},
                "soft-dependencies": [str],
                "if-dependencies": [str],
                "requires": Any("all-completed", "all-resolved"),
                "deadline-after": str,
                "expires-after": str,
                "routes": [str],
                "scopes": [str],
                "tags": {str: str},
                "extra": {str: object},
                "treeherder": object,  # Complex type
                "index": object,  # Complex type
                "run-on-projects": object,  # Uses optionally_keyed_by
                "run-on-tasks-for": [str],
                "run-on-git-branches": [str],
                "shipping-phase": Any(None, "build", "promote", "push", "ship"),
                "always-target": bool,
                "optimization": OptimizationSchema,
                "needs-sccache": bool,
                "worker-type": str,
            }
            return field_validators.get(item, str)
        elif hasattr(self, "_dict_schema"):
            return self._dict_schema.get(item, str)
        return str  # Default fallback


# Optimization schema types using msgspec
class IndexSearchOptimization(msgspec.Struct, kw_only=True, rename="kebab"):
    """Search the index for the given index namespaces."""

    index_search: List[str]


class SkipUnlessChangedOptimization(msgspec.Struct, kw_only=True, rename="kebab"):
    """Skip this task if none of the given file patterns match."""

    skip_unless_changed: List[str]


# Task reference types using msgspec
class TaskReference(msgspec.Struct, kw_only=True, rename="kebab"):
    """Reference to another task."""

    task_reference: str


class ArtifactReference(msgspec.Struct, kw_only=True, rename="kebab"):
    """Reference to a task artifact."""

    artifact_reference: str


# Create a custom validator
class OptimizationValidator:
    """A validator that can validate optimization schemas."""

    def __call__(self, value):
        """Validate optimization value."""
        if value is None:
            return None
        if isinstance(value, dict):
            if "index-search" in value:
                try:
                    return msgspec.convert(value, IndexSearchOptimization)
                except msgspec.ValidationError:
                    pass
            if "skip-unless-changed" in value:
                try:
                    return msgspec.convert(value, SkipUnlessChangedOptimization)
                except msgspec.ValidationError:
                    pass
        # Simple validation for dict types
        if isinstance(value, dict):
            if "index-search" in value and isinstance(value["index-search"], list):
                return value
            if "skip-unless-changed" in value and isinstance(
                value["skip-unless-changed"], list
            ):
                return value
        raise ValueError(f"Invalid optimization value: {value}")


class TaskRefValidator:
    """A validator that can validate task references."""

    def __call__(self, value):
        """Validate task reference value."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            if "task-reference" in value:
                try:
                    return msgspec.convert(value, TaskReference)
                except msgspec.ValidationError:
                    pass
            if "artifact-reference" in value:
                try:
                    return msgspec.convert(value, ArtifactReference)
                except msgspec.ValidationError:
                    pass
        # Simple validation for dict types
        if isinstance(value, dict):
            if "task-reference" in value and isinstance(value["task-reference"], str):
                return value
            if "artifact-reference" in value and isinstance(
                value["artifact-reference"], str
            ):
                return value
        raise ValueError(f"Invalid task reference value: {value}")


# Keep the same names for backward compatibility
OptimizationSchema = OptimizationValidator()
taskref_or_string = TaskRefValidator()
