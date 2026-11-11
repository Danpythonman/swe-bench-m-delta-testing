"""Which benchmark an instance belongs to.

The project evaluates instances from two benchmarks: SWE-bench Multimodal
(JavaScript repositories, the original scope) and SWE-bench Verified
(Python repositories). The two never share a repository, so an instance's
benchmark follows from its ID prefix alone. That keeps the benchmark out
of :class:`~sbmdt.evaluator.base.TestResult` and the S3 key formats, so
results written before Verified support existed stay readable unchanged.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    'Benchmark',
    'SWE_BENCH_VERIFIED_PREFIXES',
    'benchmark_of',
]


class Benchmark(StrEnum):
    """A benchmark whose instances this project evaluates.

    Attributes:
        SWE_BENCH_M: SWE-bench Multimodal (JavaScript repositories).
        SWE_BENCH_VERIFIED: SWE-bench Verified (Python repositories).
    """

    SWE_BENCH_M = 'swe-bench-m'
    SWE_BENCH_VERIFIED = 'swe-bench-verified'


# Instance ID prefixes (``owner__repo``) of every SWE-bench Verified
# repository. None of them is a prefix of a SWE-bench M instance ID.
SWE_BENCH_VERIFIED_PREFIXES: Final[tuple[str, ...]] = (
    'astropy__astropy',
    'django__django',
    'matplotlib__matplotlib',
    'mwaskom__seaborn',
    'pallets__flask',
    'psf__requests',
    'pydata__xarray',
    'pylint-dev__pylint',
    'pytest-dev__pytest',
    'scikit-learn__scikit-learn',
    'sphinx-doc__sphinx',
    'sympy__sympy',
)


def benchmark_of(instance_id: str) -> Benchmark:
    """Return the benchmark ``instance_id`` belongs to.

    Args:
        instance_id: A benchmark instance ID, e.g. ``'django__django-11099'``.

    Returns:
        :attr:`Benchmark.SWE_BENCH_VERIFIED` for a Verified repository,
        otherwise :attr:`Benchmark.SWE_BENCH_M`.
    """
    repo, _, _ = instance_id.rpartition('-')
    if repo in SWE_BENCH_VERIFIED_PREFIXES:
        return Benchmark.SWE_BENCH_VERIFIED
    return Benchmark.SWE_BENCH_M
