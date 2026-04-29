# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
from typing import Optional

from taskgraph.parameters import extend_parameters_schema
from taskgraph.util.schema import Schema


def get_defaults(repo_root):
    return {
        "pull_request_number": None,
    }


class CustomParametersSchema(Schema, kw_only=True, rename=None):
    pull_request_number: Optional[int]

    def __post_init__(self):
        super().__post_init__()
        if self.pull_request_number is not None and self.pull_request_number < 1:
            raise ValueError(
                f"pull_request_number must be >= 1, got {self.pull_request_number}"
            )


extend_parameters_schema(CustomParametersSchema, defaults_fn=get_defaults)


def decision_parameters(graph_config, parameters):
    if parameters["tasks_for"] == "github-release":
        parameters["target_tasks_method"] = "release"

    pr_number = os.environ.get("TASKGRAPH_PULL_REQUEST_NUMBER", None)
    parameters["pull_request_number"] = None if pr_number is None else int(pr_number)
