from __future__ import annotations

import io
import logging
import re
import tarfile
from collections.abc import Callable
from warnings import deprecated

from docker.models.containers import Container

log = logging.getLogger(__name__)

__all__ = [
    'read_from_container',
    'write_to_container',
    'apply_change_literal',
    'apply_change_regex',
    'apply_change',
]


def read_from_container(container: Container, file: str) -> str:
    """
    Read a file from inside a Docker container and return its contents as a
    string.

    Args:
        container: The running Docker container to read from.
        file: Absolute path to the file inside the container.

    Returns:
        Contents of the file
    """
    # get_archive returns a stream of bytes chunks and metadata (we discard the
    # metadata)
    bits, _ = container.get_archive(file)

    # Join all the chunks into one bytes object and wrap it in a file-like
    # object so tarfile can read it (tarfile expects a file-like object, not
    # raw bytes)
    buf = io.BytesIO(b''.join(bits))

    with tarfile.open(fileobj=buf) as tar:
        # List all files in the archive. We only expect one since we
        # requested a single file from Docker
        members = tar.getmembers()
        target_file = members[0]

        # extractfile() gives us a file-like object to read from
        # (like open() but for files inside the archive)
        extracted = tar.extractfile(target_file)
        if extracted is None:
            raise ValueError(
                f'{file} is a directory or symlink, not a regular file'
            )

        # Extract bytes and convert bytes to string
        raw_bytes = extracted.read()
        return raw_bytes.decode()


def write_to_container(container: Container, file: str, content: str) -> None:
    """
    Write a string to a file inside a Docker container.

    Args:
        container: The running Docker container to write to.
        file: Absolute path to the file inside the container.
        content: The string content to write to the file.
    """
    # Convert the string to bytes since tar works with raw bytes
    encoded: bytes = content.encode()

    # Create an in-memory buffer to build the tar archive into
    buf = io.BytesIO()

    with tarfile.open(fileobj=buf, mode='w') as tar:
        # TarInfo holds the metadata for the file we're adding to the archive
        # (like name, size, permissions, etc.)
        #
        # We only need the filename (not the full path) as the name inside the
        # tar
        info = tarfile.TarInfo(name=file.split('/')[-1])
        info.size = len(encoded)  # tar needs to know the file size upfront

        # Add the file to the archive: metadata (info) + actual content
        tar.addfile(info, io.BytesIO(encoded))

    # After writing the tar, the buffer's cursor is at the end. Here we reset
    # it to the start so put_archive can read it from the beginning.
    buf.seek(0)

    # Upload the tar to the container, extracting it into the file's parent
    # directory
    #
    # `rsplit("/", 1)[0]` gets the directory path, e.g.
    #     "/etc/myapp/karma.txt" -> "/etc/myapp"
    container.put_archive(path=file.rsplit('/', 1)[0], data=buf)


def apply_change_literal(
    container: Container,
    file: str,
    find: str,
    replace: str,
    assertion: str,
) -> None:
    """Replace the first literal occurrence of ``find`` with ``replace``.

    Reads ``file`` from ``container``; if ``assertion`` is already present,
    does nothing. Otherwise replaces the first occurrence of the literal string
    ``find`` with ``replace``, writes the file back, and verifies ``assertion``
    is present.

    Args:
        container: The running Docker container holding the file.
        file: Absolute path to the file inside the container.
        find: Exact literal string to locate.
        replace: String substituted for the first occurrence of ``find``.
        assertion: String that must be present in the file after the edit.

    Raises:
        Exception: If ``find`` is not present in the original content.
        Exception: If ``assertion`` is not present after writing.
    """
    content = read_from_container(container, file)

    # Idempotency: skip if the change is already present.
    if assertion in content:
        log.info(f'Skipping, already present: {repr(assertion)}')
        return

    if find not in content:
        raise Exception(f'Could not find target string: {repr(find)}')
    content = content.replace(find, replace, 1)

    write_to_container(container, file, content)

    updated = read_from_container(container, file)
    if assertion not in updated:
        raise Exception(
            f'Assertion failed: expected to find: {repr(assertion)}'
        )


def apply_change_regex(
    container: Container,
    file: str,
    find: str,
    replace: str | Callable[[re.Match[str]], str],
    assertion: str,
    *,
    flags: int = 0,
) -> None:
    """Replace the first regex match of ``find`` with ``replace``.

    Reads ``file`` from ``container``; if ``assertion`` is already present,
    does nothing. Otherwise substitutes the first match of the regex ``find``
    with ``replace``, writes the file back, and verifies ``assertion`` is
    present.

    Args:
        container: The running Docker container holding the file.
        file: Absolute path to the file inside the container.
        find: Regex pattern to locate.
        replace: Replacement string (supports backreferences such as ``\\1``)
            or a callable taking the match and returning the replacement.
        assertion: String that must be present in the file after the edit.
        flags: Regex flags passed to :func:`re.subn`.

    Raises:
        Exception: If ``find`` matches nothing in the original content.
        Exception: If ``assertion`` is not present after writing.
    """
    content = read_from_container(container, file)

    # Idempotency: skip if the change is already present.
    if assertion in content:
        log.info(f'Skipping, already present: {repr(assertion)}')
        return

    new_content, n = re.subn(find, replace, content, count=1, flags=flags)
    if n == 0:
        raise Exception(f'Could not find pattern: {repr(find)}')
    content = new_content

    write_to_container(container, file, content)

    updated = read_from_container(container, file)
    if assertion not in updated:
        raise Exception(
            f'Assertion failed: expected to find: {repr(assertion)}'
        )


@deprecated('use functions apply_change_literal() or apply_change_regex()')
def apply_change(
    container: Container,
    file: str,
    find: str,
    replace: str | Callable[[re.Match[str]], str],
    assertion: str,
    *,
    regex: bool = False,
    flags: int = 0,
) -> None:
    """Apply a find-and-replace to a file in a container, then verify it.

    Dispatches to :func:`apply_change_literal` or :func:`apply_change_regex`
    based on ``regex``.

    Args:
        container: The running Docker container holding the file.
        file: Absolute path to the file inside the container.
        find: Literal string (``regex=False``) or regex pattern
            (``regex=True``) to locate.
        replace: Replacement. A string in both modes; in regex mode it may
            also be a callable taking the match and returning the replacement,
            and string replacements support backreferences (e.g. ``\\1``).
        assertion: String that must be present in the file after the edit.
        regex: If True, treat ``find`` as a regex; otherwise literal.
        flags: Regex flags, used only when ``regex=True``.

    Raises:
        Exception: Propagated from the dispatched function if ``find`` is not
            found or ``assertion`` is absent after writing.
    """
    if regex:
        apply_change_regex(
            container, file, find, replace, assertion, flags=flags
        )
    else:
        if callable(replace):
            raise TypeError('literal mode requires a string replacement')
        apply_change_literal(container, file, find, replace, assertion)
