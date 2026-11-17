"""Coordinator: routes storage operations to shards and tracks storage nodes.

Self-contained on purpose (no imports from `common/`): this component will be
rewritten in Go, and its behaviour is defined by the HTTP contract alone.
"""
