"""Studio core backend: SQLite state, event log, human gates, client memory.

stdlib only. The event log is the source of truth for run state; the gates and
memory tables are projections/queryable stores written in the same transaction.
"""
