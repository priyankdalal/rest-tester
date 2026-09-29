"""Shared execution foundation for Load Testing Studio and the Data Runner.

This package holds the concurrency-agnostic contracts described in
``platform architecture guide: load-testing-and-data-runner.md``:
immutable request/environment models, the error taxonomy, cancellation,
per-worker transport, batched events, bounded-memory metrics, and the SQLite
run store. Nothing in this package is Qt-aware; UI code must only ever
consume it through signals/callbacks, never call it from a widget directly.
"""

from __future__ import annotations
