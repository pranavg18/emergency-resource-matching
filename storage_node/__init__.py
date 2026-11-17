"""Storage node: one versioned key-value store backed by a SQLite file.

Self-contained on purpose (no imports from `common/`): this component will be
rewritten in Go, and its behaviour is defined by the HTTP contract alone.
"""
