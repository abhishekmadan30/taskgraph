# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""
Transforms used to create tasks based on the kind dependencies, filtering on
common attributes like the ``build-type``.

These transforms are useful when follow-up tasks are needed for some
indeterminate subset of existing tasks. For example, running a signing task
after each build task, whatever builds may exist.
"""

from copy import deepcopy
from textwrap import dedent
from typing import Any, Dict, List, Optional, Union

import msgspec

from taskgraph.transforms.base import TransformSequence
from taskgraph.util.attributes import attrmatch
from taskgraph.util.dependencies import GROUP_BY_MAP, get_dependencies
from taskgraph.util.schema import validate_schema
from taskgraph.util.set_name import SET_NAME_MAP


# Define FetchEntry for the fetches field
class FetchEntry(msgspec.Struct, kw_only=True, omit_defaults=True):
    """A fetch entry for an artifact."""

    artifact: str
    dest: Optional[str] = None


class FromDepsConfig(msgspec.Struct, kw_only=True, omit_defaults=True, rename="kebab"):
    """
    Configuration for from-deps transforms.

    Attributes:
        kinds: Limit dependencies to specified kinds (defaults to all kinds in
              `kind-dependencies`). The first kind in the list is the "primary" kind.
              The dependency of this kind will be used to derive the label
              and copy attributes (if `copy-attributes` is True).
        set_name: UPDATE ME AND DOCS. Can be a string from SET_NAME_MAP, False, None,
                 or a dict with a SET_NAME_MAP key.
        with_attributes: Limit dependencies to tasks whose attributes match
                        using :func:`~taskgraph.util.attributes.attrmatch`.
        group_by: Group cross-kind dependencies using the given group-by
                 function. One task will be created for each group. If not
                 specified, the 'single' function will be used which creates
                 a new task for each individual dependency.
        copy_attributes: If True, copy attributes from the dependency matching the
                        first kind in the `kinds` list (whether specified explicitly
                        or taken from `kind-dependencies`).
        unique_kinds: If true (the default), there must be only a single unique task
                     for each kind in a dependency group. Setting this to false
                     disables that requirement.
        fetches: If present, a `fetches` entry will be added for each task
                dependency. Attributes of the upstream task may be used as
                substitution values in the `artifact` or `dest` values of the
                `fetches` entry.
    """

    kinds: Optional[List[str]] = None
    set_name: Optional[Union[str, bool, Dict[str, Any]]] = None
    with_attributes: Optional[Dict[str, Union[List[Any], str]]] = None
    group_by: Optional[Union[str, Dict[str, Any]]] = None
    copy_attributes: Optional[bool] = None
    unique_kinds: Optional[bool] = None
    fetches: Optional[Dict[str, List[Union[str, Dict[str, str]]]]] = None

    def __post_init__(self):
        # Validate set_name
        if self.set_name is not None and self.set_name is not False:
            if isinstance(self.set_name, str) and self.set_name not in SET_NAME_MAP:
                raise msgspec.ValidationError(f"Invalid set-name: {self.set_name}")
            elif isinstance(self.set_name, dict):
                keys = list(self.set_name.keys())
                if len(keys) != 1 or keys[0] not in SET_NAME_MAP:
                    raise msgspec.ValidationError(
                        f"Invalid set-name dict: {self.set_name}"
                    )

        # Validate group_by
        if self.group_by is not None:
            if isinstance(self.group_by, str) and self.group_by not in GROUP_BY_MAP:
                raise msgspec.ValidationError(f"Invalid group-by: {self.group_by}")
            elif isinstance(self.group_by, dict):
                keys = list(self.group_by.keys())
                if len(keys) != 1 or keys[0] not in GROUP_BY_MAP:
                    raise msgspec.ValidationError(
                        f"Invalid group-by dict: {self.group_by}"
                    )


#: Schema for from_deps transforms
class FromDepsSchema(msgspec.Struct, kw_only=True, omit_defaults=True, rename="kebab"):
    """Schema for from_deps transforms."""

    from_deps: FromDepsConfig
    # Allow extra fields
    _extra: Optional[Dict[str, Any]] = msgspec.field(default=None, name="")


# Backward compatibility
FROM_DEPS_SCHEMA = FromDepsSchema

transforms = TransformSequence()
transforms.add_validate(FromDepsSchema)


@transforms.add
def from_deps(config, tasks):
    for task in tasks:
        # Setup and error handling.
        from_deps = task.pop("from-deps")
        kind_deps = config.config.get("kind-dependencies", [])
        kinds = from_deps.get("kinds", kind_deps)

        invalid = set(kinds) - set(kind_deps)
        if invalid:
            invalid = "\n".join(sorted(invalid))
            raise Exception(
                dedent(
                    f"""
                    The `from-deps.kinds` key contains the following kinds
                    that are not defined in `kind-dependencies`:
                    {invalid}
                """.lstrip()
                )
            )

        if not kinds:
            raise Exception(
                dedent(
                    """
                The `from_deps` transforms require at least one kind defined
                in `kind-dependencies`!
                """.lstrip()
                )
            )

        # Resolve desired dependencies.
        with_attributes = from_deps.get("with-attributes")
        deps = [
            task
            for task in config.kind_dependencies_tasks.values()
            if task.kind in kinds
            if not with_attributes or attrmatch(task.attributes, **with_attributes)
        ]

        # Resolve groups.
        group_by = from_deps.get("group-by", "single")
        groups = set()

        if isinstance(group_by, dict):
            assert len(group_by) == 1
            group_by, arg = group_by.popitem()
            func = GROUP_BY_MAP[group_by]
            if func.schema:
                validate_schema(
                    func.schema, arg, f"Invalid group-by {group_by} argument"
                )
            groups = func(config, deps, arg)
        else:
            func = GROUP_BY_MAP[group_by]
            groups = func(config, deps)

        # Split the task, one per group.
        set_name = from_deps.get("set-name", "strip-kind")
        copy_attributes = from_deps.get("copy-attributes", False)
        unique_kinds = from_deps.get("unique-kinds", True)
        fetches = from_deps.get("fetches", [])
        for group in groups:
            # Verify there is only one task per kind in each group.
            group_kinds = {t.kind for t in group}
            if unique_kinds and len(group_kinds) < len(group):
                raise Exception(
                    "The from_deps transforms only allow a single task per kind in a group!"
                )

            new_task = deepcopy(task)
            new_task.setdefault("dependencies", {})
            new_task["dependencies"].update(
                {dep.kind if unique_kinds else dep.label: dep.label for dep in group}
            )

            # Set name and copy attributes from the primary kind.
            for kind in kinds:
                if kind in group_kinds:
                    primary_kind = kind
                    break
            else:
                raise Exception("Could not detect primary kind!")

            new_task.setdefault("attributes", {})["primary-kind-dependency"] = (
                primary_kind
            )

            primary_dep = [dep for dep in group if dep.kind == primary_kind][0]
            new_task["attributes"]["primary-dependency-label"] = primary_dep.label

            if set_name:
                func = SET_NAME_MAP[set_name]
                new_task["name"] = func(config, deps, primary_dep, primary_kind)

            if copy_attributes:
                attrs = new_task.setdefault("attributes", {})
                new_task["attributes"] = primary_dep.attributes.copy()
                new_task["attributes"].update(attrs)

            if fetches:
                task_fetches = new_task.setdefault("fetches", {})

                for dep_task in get_dependencies(config, new_task):
                    # Nothing to do if this kind has no fetches listed
                    if dep_task.kind not in fetches:
                        continue

                    fetches_from_dep = []
                    for kind, kind_fetches in fetches.items():
                        if kind != dep_task.kind:
                            continue

                        for fetch in kind_fetches:
                            entry = fetch.copy()
                            entry["artifact"] = entry["artifact"].format(
                                **dep_task.attributes
                            )
                            if "dest" in entry:
                                entry["dest"] = entry["dest"].format(
                                    **dep_task.attributes
                                )
                            fetches_from_dep.append(entry)

                    task_fetches[dep_task.label] = fetches_from_dep

            yield new_task
