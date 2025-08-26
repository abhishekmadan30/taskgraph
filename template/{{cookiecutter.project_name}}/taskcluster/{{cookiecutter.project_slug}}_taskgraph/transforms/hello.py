from taskgraph.transforms.base import TransformSequence
from taskgraph.util.schema import Required, Schema

HELLO_SCHEMA = Schema(
    {
        Required("noun"): str,
    }
)

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
