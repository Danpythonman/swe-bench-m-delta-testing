"""
Evaluator implementation for Carbon Design System repository instances.

Builds a Docker image from the instance's Dockerfile, runs the Jest test
suite (which already has ``jest-junit`` configured as a reporter in
``jest.config.js``), and retrieves the results.
"""

from __future__ import annotations

import logging
from typing import Final, override

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.grommet.jest_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.utils import (
    apply_change_literal,
    read_from_container,
    write_to_container,
)

__all__ = [
    'CarbonEvaluator',
]

log = logging.getLogger(__name__)

RESULTS_DIR: Final[str] = 'test-results'
RESULTS_FILE: Final[str] = 'results.xml'
SERVER_PATH: Final[str] = '/testbed/sbmdt-aat-server.js'
SERVER_LOG: Final[str] = '/tmp/sbmdt-aat-server.log'
NEWLINE: Final[str] = chr(10)
HEAP_FLAG: Final[str] = '--max-old-space-size=4096'


class CarbonEvaluator(Evaluator):
    """Evaluator for Carbon Design System benchmark instances.

    Carbon's ``jest.config.js`` already declares ``jest-junit`` in Jest's
    ``reporters`` array, so no patching is needed. This evaluator only
    ensures the output directory exists, then runs ``npm test`` with the
    ``JEST_JUNIT_OUTPUT_DIR``/``JEST_JUNIT_OUTPUT_NAME`` environment
    variables pointing to the desired results file.
    """

    @override
    def setup(self) -> None:
        """Create the directory where jest-junit will write its XML output.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            f'mkdir -p /testbed/{RESULTS_DIR}',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        if exit_code != 0:
            raise Exception(
                f'Failed to create test-results directory for '
                f'{self.instance_id}: {output.decode()}'
            )

        self._aat_configs = self._find_aat_configs()
        if self._aat_configs:
            self._install_local_rule_server()

    def _find_aat_configs(self) -> list[str]:
        """Config files that point AAT at a rule archive, if any.

        Located rather than hardcoded: the path differs between carbon
        versions, and which instances run the accessibility suite at all
        is not something an instance id predicts.
        """

        if self.container is None:
            return []
        _, out = self.container.exec_run(
            [
                'bash',
                '-c',
                "{ find /testbed -maxdepth 3 -name 'aat.js'; "
                "  find /testbed -maxdepth 3 -name '.aat.js'; } "
                "2>/dev/null | grep -v node_modules || true",
            ],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(out, bytes)
        return [p for p in out.decode().splitlines() if p.strip()]

    def _install_local_rule_server(self) -> None:
        """Serve AAT's own bundled rule engine over loopback.

        AAT's configured IBM archive endpoint now returns HTML, which
        crashes JSON parsing after jest has run but before jest-junit
        flushes, losing the whole run. The package ships the same
        awe-node engine in node_modules, so point AAT's documented
        customRuleServer/rulePack at a local copy instead of disabling
        the accessibility assertions.
        """

        if self.container is None:
            return

        for cfg in self._aat_configs:
            try:
                apply_change_literal(
                    container=self.container,
                    file=cfg,
                    find='module.exports = {',
                    replace=(
                        'module.exports = {'
                        + NEWLINE
                        + '  customRuleServer: true,'
                        + NEWLINE
                        + "  rulePack: 'http://127.0.0.1:18123/',"
                    ),
                    assertion='customRuleServer: true',
                )
            except Exception as exc:
                # A config whose shape we do not recognise is left alone;
                # the run then fails the way it did before rather than in
                # some new way caused by a half-applied edit.
                log.warning(f'could not point {cfg!r} at the local rule '
                            f'server: {exc}')

        # require.resolve() searches upward from the *script's* directory.
        # A server living in /tmp therefore looks in /tmp/node_modules and
        # /node_modules, never /testbed/node_modules, and exits before it
        # listens. Resolve from /testbed explicitly.
        write_to_container(
            self.container,
            SERVER_PATH,
            (
                "const http = require('http');"
                + NEWLINE
                + "const fs = require('fs');"
                + NEWLINE
                + 'const engine = require.resolve('
                + NEWLINE
                + "  '@ibma/aat/lib/engine/awe-node.js',"
                + NEWLINE
                + "  { paths: ['/testbed/node_modules', '/testbed'] }"
                + NEWLINE
                + ');'
                + NEWLINE
                + 'http.createServer((req, res) => {'
                + NEWLINE
                + "  if (req.url !== '/awe-node.js') {"
                + NEWLINE
                + '    res.statusCode = 404; res.end(); return;'
                + NEWLINE
                + '  }'
                + NEWLINE
                + "  res.setHeader('Content-Type', "
                + "'application/javascript');"
                + NEWLINE
                + '  fs.createReadStream(engine).pipe(res);'
                + NEWLINE
                + "}).listen(18123, '127.0.0.1');"
                + NEWLINE
            ),
        )

    @override
    def evaluate(self) -> list[TestResult]:
        """Run ``npm test`` and retrieve the JUnit XML results.

        Uses the ``JEST_JUNIT_OUTPUT_DIR``/``JEST_JUNIT_OUTPUT_NAME``
        environment variables to direct output to a known location.
        jest-junit v10 (the version installed here) does not support the
        older single-path ``JEST_JUNIT_OUTPUT`` variable.

        Returns:
            A list of :class:`TestResult` parsed from the JUnit XML output.

        Raises:
            Exception: If the container has not been started.
        """

        if self.container is None:
            raise Exception('no container')

        test_command = 'npm test'
        if getattr(self, '_aat_configs', None):
            # Start the rule server in the same shell as the tests, so
            # it outlives them and dies with them, and wait for the
            # port instead of racing jest to it.
            test_command = (
                'node ' + SERVER_PATH +
                ' >' + SERVER_LOG + ' 2>&1 & '
                'aat_pid=$!; '
                'trap \'kill "$aat_pid" 2>/dev/null || true\' EXIT; '
                'for _ in $(seq 1 50); do '
                '  (exec 3<>/dev/tcp/127.0.0.1/18123) 2>/dev/null '
                '  && break; sleep 0.2; '
                'done; '
                'npm test'
            )

        # carbon-9136 and carbon-8912 both failed npm test with
        # "/testbed/node_modules/.bin/cross-env: Permission denied" even
        # after `chmod -R +x node_modules/.bin` (exit 0): .bin entries are
        # often symlinks, and chmod on the link does not always restore
        # the target. Chmod the targets too, then run npm test in the
        # same shell so nothing can undo the bits in between.
        exit_code, output = self.container.exec_run(
            [
                'bash',
                '-c',
                'chmod -R a+x node_modules/.bin 2>/dev/null || true; '
                'find node_modules/.bin -type l -print0 '
                '| xargs -0 -r -I{} sh -c '
                '\'t=$(readlink -f "{}"); '
                '[ -n "$t" ] && chmod a+x "$t" 2>/dev/null || true\'; '
                'find node_modules -path "*/cross-env*/bin/*" '
                '-type f -exec chmod a+x {} + 2>/dev/null || true; '
                + test_command,
            ],
            environment={
                'JEST_JUNIT_OUTPUT_DIR': RESULTS_DIR,
                'JEST_JUNIT_OUTPUT_NAME': RESULTS_FILE,
                'BABEL_ENV': 'test',
                # carbon-5156 died with "Ineffective mark-compacts near
                # heap limit" partway through the suite, after roughly
                # forty files had already passed, and so wrote no
                # results.xml at all -- the same symptom as the AAT
                # failures but a different cause. The GC trace names the
                # ceiling: 1339 MB used of 1456 MB, which is the old
                # node in these images defaulting to a ~1.5 GB old
                # space. Carbon is a monorepo and its jest run simply
                # needs more than that.
                #
                # 4 GB is a cap, not a reservation: nothing else in the
                # suite comes near it, so this cannot cost the other
                # instances anything, and it stays well under the
                # worker's 8 GB even with jest's two processes.
                'NODE_OPTIONS': HEAP_FLAG,
            },
            workdir='/testbed',
            stream=False,
        )
        log.info('done running npm test')
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        # JEST_JUNIT_OUTPUT_DIR is resolved by jest-junit relative to the
        # jest process's own working directory, not necessarily /testbed.
        # Three instances (carbon-9136, carbon-5156, carbon-8912) failed
        # with results.xml missing at exactly that assumed path -- likely
        # because their npm test script cds into a package subdirectory
        # (this is a monorepo: packages/react, packages/web-components,
        # etc.) before running jest. Finding whatever jest-junit actually
        # wrote, the same way already fixed for openlayers, lighthouse
        # and alibaba-fusion, instead of assuming /testbed itself.
        default_results_file = f'/testbed/{RESULTS_DIR}/{RESULTS_FILE}'
        _, results_find = self.container.exec_run(
            [
                'find',
                '/testbed',
                '-name',
                RESULTS_FILE,
                '-not',
                '-path',
                '*/node_modules/*',
            ],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(results_find, bytes)
        written = [p for p in results_find.decode().splitlines() if p.strip()]
        if default_results_file in written:
            results_file = default_results_file
        elif written:
            results_file = written[0]
            log.info(
                f'results.xml for {self.instance_id} is at '
                f'{results_file!r}, not the expected '
                f'{default_results_file!r}'
            )
        else:
            raise Exception(
                f'jest-junit wrote no results.xml for {self.instance_id}; '
                f'expected it at {default_results_file!r}'
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
