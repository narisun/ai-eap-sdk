"""Helpers for writing deterministic LocalRuntimeAdapter responses in tests."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml


@contextmanager
def canned_responses(entries: list[dict[str, str]]) -> Iterator[Path]:
    """Yield a temp dir containing a `responses.yaml`; chdir into it for the duration."""
    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "responses.yaml"
        p.write_text(yaml.safe_dump({"responses": entries}))
        os.chdir(td)
        try:
            yield Path(td)
        finally:
            os.chdir(cwd)
