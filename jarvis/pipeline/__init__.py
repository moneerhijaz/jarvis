"""PLANv5 — the dataflow pipeline runtime (clean slate).

A request runs as a graph of small tasks: tasks wait on the tasks they depend on,
independent tasks run in parallel, a task can recurse into its own sub-pipeline, and their
results converge into one answer. See alpha/PLANv5_PIPELINE.md for the full spec.
"""
