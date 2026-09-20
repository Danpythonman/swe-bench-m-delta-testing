"""
Evaluator implementation for bpmn-js repository instances.

bpmn-js uses Karma as its test runner, configured at
``test/config/karma.unit.js``.  This evaluator:

1. Installs ``karma-junit-reporter`` in the container.
2. Patches the Karma config to emit JUnit XML output.
3. Runs ``npm test``, setting ``NODE_OPTIONS=--openssl-legacy-provider``
   when the image's Node still accepts it (the pinned webpack uses a legacy
   OpenSSL hash that Node ≥ 17 disables by default; newer Node builds reject
   the flag itself, so it is probed rather than assumed).
4. Reads the resulting XML from the container and returns parsed results.

All bpmn-js instances share the same project layout and test infrastructure,
so a single evaluator class handles every ``bpmn-io__bpmn-js-*`` instance ID.
"""

from __future__ import annotations

import logging
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.bpmn.karma_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.utils import (
    apply_change_regex,
    read_from_container,
)

__all__ = [
    'BpmnEvaluator',
]

log = logging.getLogger(__name__)

# Path to the Karma unit-test config inside the container.
# Consistent across all bpmn-js instances.
KARMA_CONFIG_FILE: Final[str] = '/testbed/test/config/karma.unit.js'

# Absolute path where karma-junit-reporter writes its output.
# karma.unit.js sets basePath = '../../' (relative to test/config/),
# which resolves to /testbed, so outputDir 'test-results' lands here.
RESULTS_XML: Final[str] = '/testbed/test-results/results.xml'

# Re-enables the legacy OpenSSL provider for webpack's MD4 hashing.
# Not every Node build still accepts it in NODE_OPTIONS - see
# BpmnEvaluator._node_environment.
OPENSSL_LEGACY_FLAG: Final[str] = '--openssl-legacy-provider'


class BpmnEvaluator(Evaluator):
    """Evaluator for bpmn-io/bpmn-js benchmark instances.

    Builds a Docker image for the given instance, installs and configures
    ``karma-junit-reporter`` to produce JUnit XML output, executes
    ``npm test``, and parses the resulting XML into :class:`TestResult`
    objects.

    The ``NODE_OPTIONS=--openssl-legacy-provider`` environment variable is
    injected at test-run time to work around the
    ``ERR_OSSL_EVP_UNSUPPORTED`` error that arises when the webpack version
    pinned by the repository attempts to use a legacy MD4 hash on Node ≥ 17,
    but only where that Node build still accepts the flag - see
    :meth:`_node_environment`.
    """

    @override
    def setup(self) -> None:
        """Install the JUnit reporter and patch Karma's unit-test config.

        Steps performed:

        1. Install ``karma-junit-reporter`` via npm inside the container.
        2. Add ``'junit'`` to the ``reporters`` array in
           ``test/config/karma.unit.js``.
        3. Insert a ``junitReporter`` config block inside the existing
           ``karma.set({...})`` call.

        The bpmn-js karma config has no explicit ``plugins`` array so we rely
        on karma-junit-reporter's auto-discovery (it registers itself as a
        karma plugin via its ``package.json`` ``keywords``).

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first).
            Exception: If ``karma-junit-reporter`` installation fails.
        """

        if self.container is None:
            raise Exception('no container')

        # ------------------------------------------------------------------
        # 1. Install karma-junit-reporter.
        #    --legacy-peer-deps is required because karma-webpack@3 declares
        #    a peer dep on webpack 2/3 but webpack 4 is installed.
        # ------------------------------------------------------------------
        exit_code, output = self.container.exec_run(
            'npm install karma-junit-reporter --save-dev --legacy-peer-deps',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info('npm install exit_code=%s', exit_code)
        log.info(output.decode())

        if exit_code != 0:
            raise Exception(
                f'Failed to install karma-junit-reporter for '
                f'{self.instance_id}: {output.decode()}'
            )

        # ------------------------------------------------------------------
        # 2. Add 'junit' to the reporters array.
        #
        # The bpmn-js karma config uses a dynamic reporters line:
        #   reporters: [ 'progress' ].concat(coverage ? 'coverage' : []),
        #
        # We append to whatever array expression is already there by
        # matching the whole reporters line and tacking on .concat('junit').
        # ------------------------------------------------------------------
        apply_change_regex(
            container=self.container,
            file=KARMA_CONFIG_FILE,
            find=r'(reporters:\s*.+?)(\s*,\s*\n)',
            replace=lambda m: f"{m.group(1)}.concat('junit'){m.group(2)}",
            assertion="concat('junit')",
        )

        # ------------------------------------------------------------------
        # 3. Inject junitReporter config block inside karma.set({...}).
        #
        # Anchor on `singleRun: true` which is present in every bpmn-js
        # karma config. Inserting after it keeps us firmly inside the
        # karma.set({...}) object literal, avoiding the syntax error that
        # occurs when the block lands after the closing `});`.
        # ------------------------------------------------------------------
        apply_change_regex(
            container=self.container,
            file=KARMA_CONFIG_FILE,
            find=r'(singleRun:\s*true,?)',
            replace=(
                r'\1'
                '\n\n    junitReporter: {'
                "\n      outputDir: 'test-results',"
                "\n      outputFile: 'results.xml',"
                '\n      useBrowserName: false,'
                '\n    },'
            ),
            assertion='junitReporter:',
        )

        log.info('BpmnEvaluator setup complete for %s', self.instance_id)

    def _node_environment(self) -> dict[str, str]:
        """Environment for ``npm test``, with the OpenSSL flag if usable.

        The webpack version bpmn-js pins hashes with MD4, which Node >= 17
        disables unless ``--openssl-legacy-provider`` re-enables it. That
        flag used to be set unconditionally, but the base images are tagged
        ``:latest`` and their Node has since moved: newer builds reject the
        flag outright with ``--openssl-legacy-provider is not allowed in
        NODE_OPTIONS`` and exit 9 before running a single test, so the
        suite never writes its JUnit XML and the whole instance fails with
        a misleading "results.xml not found".

        Probing costs one short exec and keeps both worlds working: where
        the flag is still accepted nothing changes, and where it is not the
        suite at least runs and reports.

        Returns:
            The environment mapping to pass to ``npm test``.
        """
        if self.container is None:
            raise Exception('no container')

        probe_code, _ = self.container.exec_run(
            "node -e ''",
            environment={'NODE_OPTIONS': OPENSSL_LEGACY_FLAG},
            workdir='/testbed',
            stream=False,
        )
        if probe_code == 0:
            return {'NODE_OPTIONS': OPENSSL_LEGACY_FLAG}

        log.warning(
            "node in this image rejects %s in NODE_OPTIONS; running %s "
            'without it. If webpack then fails on an unsupported MD4 '
            'digest, the image needs a Node matching the pinned webpack.',
            OPENSSL_LEGACY_FLAG,
            self.instance_id,
        )
        return {}

    @override
    def evaluate(self) -> list[TestResult]:
        """Run ``npm test`` and retrieve the JUnit XML results.

        Runs the Karma test suite, suppressing the OpenSSL incompatibility
        between Node ≥ 17 and the webpack version pinned by bpmn-js where
        the image's Node still supports the flag (see
        :meth:`_node_environment`). Reads the XML written to
        ``/testbed/test-results/results.xml`` by ``karma-junit-reporter``
        and parses it into :class:`TestResult` objects.

        Returns:
            A list of :class:`TestResult` parsed from the JUnit XML output.

        Raises:
            Exception: If the container has not been started.
        """

        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            'npm test',
            environment=self._node_environment(),
            workdir='/testbed',
            stream=False,
        )
        log.info(
            'npm test finished for %s, exit_code=%s',
            self.instance_id,
            exit_code,
        )
        assert isinstance(output, bytes)

        log.info(output.decode())

        results_xml = read_from_container(
            self.container,
            RESULTS_XML,
        )

        return results_xml_to_test_results(
            self.instance_id,
            self.patch_type,
            self.agent_name,
            results_xml,
            self.timestamp,
        )

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass
