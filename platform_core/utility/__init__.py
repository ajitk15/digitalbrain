"""Surfaces built on top of the platform rather than part of it.

What lives here consumes the core the way an outside caller does - through
`workbench.access`, the published graph, the browser's own answering path - and
adds no rule of its own. Nothing in `platform_core` imports from this package;
the dependency runs one way, so a module here can be removed without the
platform noticing.

Named `utility` rather than `tools` on purpose: `agent_runtime/tools.py` already
owns that word for the scoped tools a model may call, and two different
meanings of "tools" in one codebase is how someone ends up reading the wrong
file.
"""
