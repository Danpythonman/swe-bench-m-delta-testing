"""
Evaluator implementation for bpmn-js repository instances.

bpmn-js uses Karma as its test runner, configured at
``test/config/karma.unit.js``.  This evaluator:

1. Installs ``karma-junit-reporter`` in the container.
2. Patches the Karma config to emit JUnit XML output and to make
   ChromeHeadless reliably capture inside Docker (IPv4 hostname, no
   proxy, ephemeral remote-debugging port).
3. Runs ``npm test`` with ``TEST_BROWSERS=ChromeHeadless``.  On Node ≥ 17
   also sets ``NODE_OPTIONS=--openssl-legacy-provider`` (required because
   the pinned webpack version uses a legacy OpenSSL hash that Node ≥ 17
   disables by default).  On Node < 17 that flag is omitted: it is not a
   recognized ``NODE_OPTIONS`` value and aborts the process immediately.
4. Reads the resulting XML from the container and returns parsed results.

All bpmn-js instances share the same project layout and test infrastructure,
so a single evaluator class handles every ``bpmn-io__bpmn-js-*`` instance ID.
"""

from __future__ import annotations

import logging
import re
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.bpmn.karma_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.utils import (
    apply_change_regex,
    read_from_container,
    write_to_container,
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

# Chrome flags required for headless capture as root inside Docker.
# --no-proxy-server avoids HTTP(S)_PROXY hijacking localhost:9876;
# --remote-debugging-port=0 avoids fights over the launcher's default 9222.
_CHROME_DOCKER_FLAGS: Final[str] = (
    "[\n"
    "          '--no-sandbox',\n"
    "          '--disable-setuid-sandbox',\n"
    "          '--disable-dev-shm-usage',\n"
    "          '--no-proxy-server',\n"
    "          '--remote-debugging-port=0'\n"
    "        ]"
)


class BpmnEvaluator(Evaluator):
    """Evaluator for bpmn-io/bpmn-js benchmark instances.

    Builds a Docker image for the given instance, installs and configures
    ``karma-junit-reporter`` to produce JUnit XML output, executes
    ``npm test`` under ChromeHeadless, and parses the resulting XML into
    :class:`TestResult` objects.
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
        4. Harden ``ChromeHeadless_Linux`` for Docker (proxy / debugging
           port) and force Karma onto IPv4 loopback so Chrome can capture.

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
                # Force IPv4 so Chrome does not try ::1 while Karma listens
                # on 127.0.0.1 (classic Docker capture timeout).
                "\n    hostname: '127.0.0.1',"
                "\n    listenAddress: '127.0.0.1',"
            ),
            assertion='junitReporter:',
        )

        # ------------------------------------------------------------------
        # 4. Ensure a Docker-safe ChromeHeadless_Linux custom launcher.
        #
        # Older bpmn-js karma configs already define ChromeHeadless_Linux
        # (sometimes with debug: true). Newer ones only use bare
        # ChromeHeadless. In both cases we need --no-sandbox,
        # --no-proxy-server, and an ephemeral remote-debugging port so
        # Chrome can capture inside Docker as root.
        # ------------------------------------------------------------------
        self._ensure_chrome_docker_launcher()

        log.info('BpmnEvaluator setup complete for %s', self.instance_id)

    def _ensure_chrome_docker_launcher(self) -> None:
        """Install or rewrite ``ChromeHeadless_Linux`` with Docker-safe flags."""
        assert self.container is not None

        content = read_from_container(self.container, KARMA_CONFIG_FILE)
        if '--no-proxy-server' in content:
            log.info('Chrome Docker flags already present; skipping launcher patch')
            return

        launcher_block = (
            'ChromeHeadless_Linux: {\n'
            "        base: 'ChromeHeadless',\n"
            f'        flags: {_CHROME_DOCKER_FLAGS}\n'
            '      }'
        )

        if 'ChromeHeadless_Linux' in content:
            apply_change_regex(
                container=self.container,
                file=KARMA_CONFIG_FILE,
                find=(
                    r'ChromeHeadless_Linux:\s*\{'
                    r'[\s\S]*?'
                    r'\n\s*\}'
                ),
                replace=launcher_block,
                assertion='--no-proxy-server',
            )
            return

        # Newer karma.unit.js has no customLaunchers — inject one next to
        # the browsers setting so TEST_BROWSERS=ChromeHeadless_Linux works.
        custom_launchers = (
            'customLaunchers: {\n'
            f'      {launcher_block}\n'
            '    },\n    '
        )
        if re.search(r'\bbrowsers\b\s*,', content):
            apply_change_regex(
                container=self.container,
                file=KARMA_CONFIG_FILE,
                find=r'(\bbrowsers\b\s*,)',
                replace=custom_launchers + r'\1',
                assertion='--no-proxy-server',
            )
            return

        # Fallback: append before karma.set(config) / end of exported function.
        if 'karma.set(config)' in content:
            updated = content.replace(
                'karma.set(config)',
                'config.customLaunchers = {'
                + launcher_block
                + '};\n  karma.set(config)',
                1,
            )
            write_to_container(self.container, KARMA_CONFIG_FILE, updated)
            verify = read_from_container(self.container, KARMA_CONFIG_FILE)
            if '--no-proxy-server' not in verify:
                raise Exception('Failed to inject ChromeHeadless_Linux launcher')
            return

        raise Exception(
            f'Could not install ChromeHeadless_Linux launcher in '
            f'{KARMA_CONFIG_FILE}'
        )

    def _npm_test_environment(self) -> dict[str, str]:
        """Build env vars for ``npm test``, including Node-version-aware
        OpenSSL legacy-provider handling and ChromeHeadless selection.
        """
        assert self.container is not None

        exit_code, output = self.container.exec_run(
            ['node', '-p', 'process.versions.node'],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        node_version = output.decode().strip()
        log.info('container node version=%s (exit_code=%s)', node_version, exit_code)

        env: dict[str, str] = {
            # Use our Docker-hardened custom launcher (installed in setup).
            'TEST_BROWSERS': 'ChromeHeadless_Linux',
            # Belt-and-suspenders with --no-proxy-server: keep loopback local.
            'NO_PROXY': 'localhost,127.0.0.1,::1',
            'no_proxy': 'localhost,127.0.0.1,::1',
        }

        try:
            major = int(node_version.split('.', 1)[0])
        except ValueError:
            major = 0
            log.warning(
                'Could not parse node version %r; skipping openssl-legacy-provider',
                node_version,
            )

        # Node < 17 rejects --openssl-legacy-provider in NODE_OPTIONS with
        # "is not allowed in NODE_OPTIONS" and never starts Karma.
        if major >= 17:
            env['NODE_OPTIONS'] = '--openssl-legacy-provider'
            log.info('Setting NODE_OPTIONS=--openssl-legacy-provider (node>=17)')
        else:
            log.info(
                'Skipping NODE_OPTIONS=--openssl-legacy-provider (node=%s)',
                node_version,
            )

        return env

    @override
    def evaluate(self) -> list[TestResult]:
        """Run ``npm test`` and retrieve the JUnit XML results.

        Selects ChromeHeadless and, on Node ≥ 17, enables the OpenSSL
        legacy provider. Reads the XML written to
        ``/testbed/test-results/results.xml`` by ``karma-junit-reporter``
        and parses it into :class:`TestResult` objects.

        Returns:
            A list of :class:`TestResult` parsed from the JUnit XML output.

        Raises:
            Exception: If the container has not been started.
        """

        if self.container is None:
            raise Exception('no container')

        environment = self._npm_test_environment()

        exit_code, output = self.container.exec_run(
            'npm test',
            environment=environment,
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
