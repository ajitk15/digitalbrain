"""Every tool an agent may be given: a closed registry, like `services.FEATURES`.

A run is handed the tools whose `purposes` include its scope's purpose and no
others. The provider runtimes build what each model is shown from these specs
(`ToolSpec.wire`), and `run_tool` is the only thing that executes one, so the
list a model sees and the list that can run cannot drift apart. A tool is added
here, with its purposes, or it does not exist.

Every tool is read-only. Writing - a pull request, a file in a folder, a source,
a verdict, a published version - happens in code after a person approves it,
never as a tool (`documentation/engineering/agentic-ai-plan.md`).
"""

from .code_tools import CODE_TOOLS
from .tools import FETCH_SOURCE, SEARCH_KNOWLEDGE

TOOLS = {spec.name: spec for spec in (SEARCH_KNOWLEDGE, FETCH_SOURCE, *CODE_TOOLS)}


def for_scope(scope):
    """The tools a run in this scope is given, in registry order."""
    return [spec for spec in TOOLS.values() if scope.purpose in spec.purposes]
