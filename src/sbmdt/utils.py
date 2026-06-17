import io
import tarfile

from docker.models.containers import Container

__all__ = [
    'read_from_container',
    'write_to_container',
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


def apply_change(
    container: Container, file: str, find: str, replace: str, assertion: str
) -> None:
    content = read_from_container(container, file)
    if find not in content:
        raise Exception(f'Could not find target string: {repr(find)}')

    content = content.replace(find, replace, 1)
    write_to_container(container, file, content)

    updated = read_from_container(container, file)

    if assertion not in updated:
        raise Exception(
            f'Assertion failed: expected to find: {repr(assertion)}'
        )
