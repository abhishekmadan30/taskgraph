import msgspec

from taskgraph.transforms.base import TransformSequence
from taskgraph.util.schema import Schema


class HelloSchema(msgspec.Struct, kw_only=True):
    noun: str  # Required field


HELLO_SCHEMA = Schema(HelloSchema)

transforms = TransformSequence()
transforms.add_validate(HELLO_SCHEMA)


@transforms.add
def add_noun(config, tasks):
    for task in tasks:
        noun = task.pop("noun").capitalize()
        task["description"] = f"Prints 'Hello {noun}'"

        env = task.setdefault("worker", {}).setdefault("env", {})
        env["NOUN"] = noun

        yield task
