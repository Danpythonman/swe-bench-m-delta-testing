"""
Evaluator implementation for OpenLayers repository instances.

Builds a Docker image from the instance's Dockerfile, configures the Karma
test runner to emit JUnit XML output and run headless in a container,
executes the Mocha-based test suite (skipping the separate pixel-diff
rendering suite, which SWE-bench instances' gold patches never touch), and
retrieves the results.
"""

from __future__ import annotations

import logging
from typing import Final, override

from sbmdt.evaluator.alibaba.karma_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.utils import (
    apply_change_literal,
    apply_change_regex,
    read_from_container,
)

__all__ = [
    'OpenlayersEvaluator',
]

log = logging.getLogger(__name__)

DEFAULT_KARMA_CONFIG_FILE: Final[str] = '/testbed/test/karma.config.js'


class OpenlayersEvaluator(Evaluator):
    """Evaluator for OpenLayers benchmark instances.

    Builds a Docker image for the given instance, patches the Karma
    configuration to work headlessly in a container and produce JUnit XML
    output, executes the Karma/Mocha test suite, and reads the resulting
    XML from the container. Only ``npm run karma`` is run, not the
    ``test-rendering`` half of ``npm test`` (a separate pixel-diff visual
    regression suite): no observed instance's gold patch touches anything
    under ``rendering/``, only ``test/spec/``.
    """

    @override
    def setup(self) -> None:
        """Install the JUnit reporter and patch Karma's config to run
        headlessly in a container.

        Steps performed:
        1. Install ``karma-junit-reporter`` in the container.
        2. Add the JUnit reporter and its output config to ``reporters``.
        3. Add an explicit ``plugins`` allowlist. Karma auto-loads every
           ``karma-*`` package present in ``node_modules`` by default, and
           ``karma-firefox-launcher``'s own load-time environment probe
           throws in this container (it shells out to the WSL-only
           ``wslpath``), which Karma treats as a fatal error before any
           test runs -- even though Firefox is never used here.
        4. Replace the ``Chrome`` browser with a custom launcher that adds
           ``--no-sandbox``, since Chrome refuses to start as root
           (the container's default user) without it.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        # The commit checked out inside the container varies per instance
        # (SWE-bench resolves it at container runtime, not build time), and
        # karma's config file has not lived at a single stable path across
        # openlayers' history. Discovering it here, instead of assuming
        # /testbed/test/karma.config.js, is what makes setup() work across
        # different commits rather than only the ones that happen to match
        # the current default layout.
        exit_code, find_output = self.container.exec_run(
            [
                'find',
                '/testbed',
                '-maxdepth',
                '6',
                '(',
                '-iname',
                'karma.config.*js',
                '-o',
                '-iname',
                'karma.conf.*js',
                ')',
                '-not',
                '-path',
                '*/node_modules/*',
            ],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(find_output, bytes)
        candidates = [
            c
            for c in find_output.decode().splitlines()
            if c.strip() and 'rendering' not in c
        ]
        if DEFAULT_KARMA_CONFIG_FILE in candidates:
            karma_config_file = DEFAULT_KARMA_CONFIG_FILE
        elif candidates:
            karma_config_file = candidates[0]
        else:
            # Diagnostic broad search, so a failure here leaves
            # something to look at instead of only "not found": what
            # test-related files this checkout actually has, if any.
            _, diag_output = self.container.exec_run(
                ['find', '/testbed', '-iname', '*karma*'],
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(diag_output, bytes)
            raise Exception(
                f'No karma config found under /testbed for '
                f'{self.instance_id} (searched for karma.config.*js and '
                f'karma.conf.*js up to 6 levels deep, excluding '
                f'node_modules and rendering). Files matching *karma*'
                f'anywhere under /testbed: {diag_output.decode()!r}'
            )
        if karma_config_file != DEFAULT_KARMA_CONFIG_FILE:
            log.info(
                f'karma config for {self.instance_id} is at '
                f'{karma_config_file!r}, not the default '
                f'{DEFAULT_KARMA_CONFIG_FILE!r}'
            )
        self._karma_config_file = karma_config_file
        self._results_file = (
            karma_config_file.rsplit('/', 1)[0] + '/test-results/results.xml'
        )

        exit_code, output = self.container.exec_run(
            'npm install karma-junit-reporter --save-dev',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        log.info(exit_code)
        log.info(output.decode())
        if exit_code != 0:
            raise Exception(
                f'Failed to install karma-junit-reporter for '
                f'{self.instance_id}: {output.decode()}'
            )

        def _add_junit_reporter(m):
            inner = m.group(1).rstrip()
            sep = ', ' if inner and not inner.endswith(',') else ' '
            return (
                f"reporters: [{inner}{sep}'junit'],\n"
                '    junitReporter: {\n'
                "      outputDir: 'test-results',\n"
                "      outputFile: 'results.xml',\n"
                '      useBrowserName: false,\n'
                '    },'
            )

        apply_change_regex(
            container=self.container,
            file=self._karma_config_file,
            find=r"reporters:\s*\[([^\]]*)\],",
            replace=_add_junit_reporter,
            assertion='junitReporter: {',
        )

        apply_change_literal(
            container=self.container,
            file=self._karma_config_file,
            find='webpackMiddleware: {',
            replace=(
                'plugins: [\n'
                "      'karma-mocha',\n"
                "      'karma-chrome-launcher',\n"
                "      'karma-webpack',\n"
                "      'karma-sourcemap-loader',\n"
                "      'karma-coverage-istanbul-reporter',\n"
                "      'karma-junit-reporter',\n"
                '    ],\n'
                '    webpackMiddleware: {'
            ),
            assertion="plugins: [\n      'karma-mocha',",
        )

        apply_change_regex(
            container=self.container,
            file=self._karma_config_file,
            find=r"browsers:\s*\[[^\]]*\],",
            replace=(
                "browsers: ['ChromeNoSandbox'],\n"
                '    customLaunchers: {\n'
                '      ChromeNoSandbox: {\n'
                "        base: 'Chrome',\n"
                "        flags: ['--no-sandbox', '--disable-gpu'],\n"
                '      },\n'
                '    },'
            ),
            assertion="browsers: ['ChromeNoSandbox'],",
        )

        log.info('All changes applied successfully.')

    @override
    def evaluate(self) -> list[TestResult]:
        """Run the Karma/Mocha suite and retrieve the JUnit XML results.

        Returns:
            A list of :class:`TestResult` parsed from the JUnit XML output.

        Raises:
            Exception: If the container has not been started (i.e.,
                ``setup`` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            'xvfb-run -a npm run karma -- --single-run --log-level error',
            environment={
                # webpack 4's MD4-based hashing is incompatible with the
                # default OpenSSL 3 provider bundled with this container's
                # Node version.
                'NODE_OPTIONS': '--openssl-legacy-provider',
            },
            workdir='/testbed',
            stream=False,
        )
        log.info('done running')
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        # junitReporter's outputDir is relative to karma's basePath, which
        # each config sets for itself, so the results file does not
        # reliably sit beside the config. Prefer the derived path when it
        # is really there and otherwise take what karma actually wrote.
        _, results_find = self.container.exec_run(
            [
                'find',
                '/testbed',
                '-name',
                'results.xml',
                '-not',
                '-path',
                '*/node_modules/*',
            ],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(results_find, bytes)
        written = [p for p in results_find.decode().splitlines() if p.strip()]
        if self._results_file in written:
            results_file = self._results_file
        elif written:
            results_file = written[0]
            log.info(
                f'results.xml for {self.instance_id} is at {results_file!r}, '
                f'not the expected {self._results_file!r}'
            )
        else:
            raise Exception(
                f'karma wrote no results.xml for {self.instance_id}; '
                f'expected it at {self._results_file!r}'
            )

        results = read_from_container(self.container, results_file)

        return results_xml_to_test_results(
            self.instance_id,
            self.patch_type,
            self.agent_name,
            results,
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
