"""Sync adapters: one per provider, all read-only.

An adapter fetches what is new or changed since its cursor and hands raw bytes and
a Location to the Ingestor. It never writes to the server: selects are read-only,
bodies are fetched with BODY.PEEK, and Graph is only ever sent GET requests. The
guard tests in tests/test_guards.py hold every file in this package to that.
"""
