# Copyright 2026 Marimo. All rights reserved.
"""The host API and its session integration for `marimo edit`."""

OPERATIONS = [
    "notebook.create",
    "notebook.update",
    "notebook.delete",
    "notebook.open",
    "catalog.read",
    "notebook.read",
    "notebook.edit",
    "notebook.save",
    "runtime.start",
    "runtime.stop",
    "runtime.restart",
    "runtime.run",
    "runtime.input",
    "runtime.execute",
    "runtime.interrupt",
    "runtime.cancel",
]
"""Supported operations advertised in the `host` object."""
