"""Per-instance metadata for SWE-bench Verified instances.

Each Verified instance directory under ``dockerfiles/`` carries two JSON
files written by ``scripts/import_swebench.py``:

``instance.json``
    What the evaluator needs to know about the instance: repository,
    version, and base commit. Read at evaluation time.

``reference.json``
    The benchmark's published FAIL_TO_PASS and PASS_TO_PASS lists. These
    are held out: nothing under ``sbmdt.evaluator`` reads them, so the
    harness derives its own split independently, and
    ``scripts/compare_official_split.py`` is the only consumer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from sbmdt.env import DOCKERFILES_BASE

__all__ = [
    'INSTANCE_FILENAME',
    'REFERENCE_FILENAME',
    'InstanceMetadata',
    'ReferenceSplit',
]

INSTANCE_FILENAME: Final[str] = 'instance.json'
REFERENCE_FILENAME: Final[str] = 'reference.json'


def _require_str(obj: dict[str, Any], key: str) -> str:
    """Return ``obj[key]``, checking that it is a non-empty string.

    Raises:
        ValueError: If the key is missing, empty, or not a string.
    """
    value = obj.get(key)
    if not value or not isinstance(value, str):
        raise ValueError(f'{key} missing or not a non-empty str')
    return value


def _require_str_list(obj: dict[str, Any], key: str) -> list[str]:
    """Return ``obj[key]``, checking that it is a list of strings.

    Raises:
        ValueError: If the key is missing or not a list of strings.
    """
    value = obj.get(key)
    if not isinstance(value, list):
        raise ValueError(f'{key} missing or not a list')
    items: list[str] = []
    for item in value:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, str):
            raise ValueError(f'{key} contains a non-str item')
        items.append(item)
    return items


@dataclass(kw_only=True, frozen=True)
class InstanceMetadata:
    """What the evaluator knows about a SWE-bench Verified instance.

    Attributes:
        instance_id: The instance ID, e.g. ``'django__django-11099'``.
        repo: The GitHub repository, e.g. ``'django/django'``.
        version: The repository version the benchmark assigns the
            instance, which determines the prebuilt environment.
        base_commit: The commit the gold patch was written against.
    """

    instance_id: str
    repo: str
    version: str
    base_commit: str

    def write(self, base: Path = DOCKERFILES_BASE) -> Path:
        """Write this metadata to the instance's ``instance.json``.

        Args:
            base: Directory of per-instance folders.

        Returns:
            The path written.
        """
        path = base / self.instance_id / INSTANCE_FILENAME
        path.write_text(json.dumps(asdict(self), indent=2) + '\n')
        return path

    @staticmethod
    def load(
        instance_id: str, base: Path = DOCKERFILES_BASE
    ) -> InstanceMetadata:
        """Read an instance's ``instance.json``.

        Args:
            instance_id: The instance to look up.
            base: Directory of per-instance folders.

        Returns:
            The parsed metadata.

        Raises:
            FileNotFoundError: If the instance has no ``instance.json``.
            ValueError: If the file is missing a field.
        """
        obj = json.loads((base / instance_id / INSTANCE_FILENAME).read_text())
        return InstanceMetadata(
            instance_id=_require_str(obj, 'instance_id'),
            repo=_require_str(obj, 'repo'),
            version=_require_str(obj, 'version'),
            base_commit=_require_str(obj, 'base_commit'),
        )


@dataclass(kw_only=True, frozen=True)
class ReferenceSplit:
    """The benchmark's published test split for one instance.

    Attributes:
        instance_id: The instance the split belongs to.
        fail_to_pass: Tests the benchmark expects to fail before the gold
            patch and pass after it.
        pass_to_pass: Tests the benchmark expects to pass both before and
            after.
    """

    instance_id: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]

    def write(self, base: Path = DOCKERFILES_BASE) -> Path:
        """Write this split to the instance's ``reference.json``.

        Args:
            base: Directory of per-instance folders.

        Returns:
            The path written.
        """
        path = base / self.instance_id / REFERENCE_FILENAME
        path.write_text(json.dumps(asdict(self), indent=2) + '\n')
        return path

    @staticmethod
    def load(
        instance_id: str, base: Path = DOCKERFILES_BASE
    ) -> ReferenceSplit:
        """Read an instance's ``reference.json``.

        Args:
            instance_id: The instance to look up.
            base: Directory of per-instance folders.

        Returns:
            The parsed split.

        Raises:
            FileNotFoundError: If the instance has no ``reference.json``.
            ValueError: If the file is missing a field.
        """
        obj = json.loads((base / instance_id / REFERENCE_FILENAME).read_text())
        return ReferenceSplit(
            instance_id=_require_str(obj, 'instance_id'),
            fail_to_pass=_require_str_list(obj, 'fail_to_pass'),
            pass_to_pass=_require_str_list(obj, 'pass_to_pass'),
        )
