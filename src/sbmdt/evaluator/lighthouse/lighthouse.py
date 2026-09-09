"""
Evaluator implementation for Lighthouse repository instances.

Builds a Docker image from the instance's Dockerfile, configures Mocha to
emit JUnit XML output via ``mocha-junit-reporter``, runs the three test
suites (CLI, core, viewer) independently, and retrieves the combined
results.
"""

from __future__ import annotations

import logging
from typing import Final, override

import docker.errors

from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.lighthouse.mocha_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.utils import apply_change_literal, read_from_container

__all__ = [
    'LighthouseEvaluator',
]

log = logging.getLogger(__name__)

PACKAGE_JSON_FILE: Final[str] = '/testbed/package.json'
RUN_MOCHA_SCRIPT: Final[str] = '/testbed/lighthouse-core/scripts/run-mocha.sh'
RESULTS_DIR: Final[str] = '/testbed/test-results'
# Each suite is run independently (rather than via run-mocha.sh's default
# ``&&``-chained invocation) so that one suite's test failures don't prevent
# the remaining suites from running.
SUITES: Final[list[str]] = [
    'lighthouse-cli',
    'lighthouse-core',
    'lighthouse-viewer',
]


class LighthouseEvaluator(Evaluator):
    """Evaluator for Lighthouse benchmark instances.

    Builds a Docker image for the given instance, installs and configures
    ``mocha-junit-reporter`` to produce a JUnit XML file per test suite,
    executes each suite's Mocha run independently, and reads the resulting
    XML files from the container.
    """

    @override
    def setup(self) -> None:
        """Install the JUnit reporter and enable it in ``run-mocha.sh``.

        Steps performed:
        1. Install ``mocha-junit-reporter`` in the container, pinned to its
           last 1.x release for compatibility with this project's old
           Mocha version.
        2. Install the ``lighthouse-cli`` sub-package's own dependencies;
           its tests fail with a "needs to be compiled" error otherwise,
           unlike the ``lighthouse-core``/``lighthouse-viewer`` suites.
        3. Create the results output directory.
        4. Patch ``run-mocha.sh`` so each Mocha invocation writes its own
           JUnit XML file, named after the suite directory it tested.

        Raises:
            Exception: If the container has not been started (i.e.,
                :meth:`Evaluator.provision` was not called first), or if
                the instance is from Lighthouse's post-rewrite era (not yet
                supported; see the module docstring caveat below).
        """

        if self.container is None:
            raise Exception('no container')

        # Lighthouse was rewritten around v10 to use ESM modules, yarn, and
        # a JS-based test runner instead of the npm/bash-script setup this
        # evaluator targets. Fail clearly rather than limping through a
        # confusing chain of npm/tsc errors on unsupported instances.
        package_json = read_from_container(self.container, PACKAGE_JSON_FILE)
        if '"type": "module"' in package_json:
            raise Exception(
                f'{self.instance_id} appears to be from the post-rewrite '
                'ESM/yarn era of Lighthouse, which this evaluator does '
                'not support (only the older npm/bash-script-based test '
                'setup is supported).'
            )

        # 1. Install package
        exit_code, output = self.container.exec_run(
            'npm install mocha-junit-reporter@1 --save-dev --legacy-peer-deps',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        if exit_code != 0:
            raise Exception(
                f'Failed to install mocha-junit-reporter for '
                f'{self.instance_id}: {output.decode()}'
            )

        # 2. Install lighthouse-cli's own dependencies. install-cli is not
        # the script name in every commit's package.json: two separate
        # instances failed with "Missing script: install-cli", and npm's
        # own error both times suggested install-all as one of the
        # existing scripts -- direct evidence for that alternative name,
        # not a guess. Beyond those two names, the rest of setup() below
        # is already pinned to one specific commit's exact quirks
        # (typescript@2.0.3, a silent prepublish hook failure), so a
        # package.json with neither script most likely needs handling
        # this evaluator has never been given evidence for. Failing with
        # the scripts actually available keeps that distinction visible.
        node_snippet = (
            "console.log(JSON.stringify("
            "require('./package.json').scripts || {}))"
        )
        exit_code, scripts_output = self.container.exec_run(
            ['node', '-e', node_snippet],
            workdir='/testbed',
        )
        assert isinstance(scripts_output, bytes)
        if b'"install-cli"' in scripts_output:
            install_script = 'install-cli'
        elif b'"install-all"' in scripts_output:
            install_script = 'install-all'
        else:
            raise Exception(
                f'{self.instance_id} has neither "install-cli" nor '
                f'"install-all" in package.json, so this evaluator '
                f'cannot install lighthouse-cli\'s dependencies the way '
                f'it does for the commits it was written against. '
                f'Scripts available: {scripts_output.decode()!r}'
            )

        exit_code, output = self.container.exec_run(
            f'npm run {install_script}',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        if exit_code != 0:
            raise Exception(
                f'Failed to install lighthouse-cli dependencies for '
                f'{self.instance_id} (via "{install_script}"): '
                f'{output.decode()}'
            )

        # lighthouse-cli/package.json declares loose ranges
        # (typescript@^2.0.3, @types/node@^6.0.45); npm install resolves
        # these to the newest matching patch release, but a later
        # @types/node 6.x patch uses reference-directive syntax this old
        # TypeScript can't parse. Pin both to their exact original
        # versions to avoid that drift.
        exit_code, output = self.container.exec_run(
            'npm install typescript@2.0.3 @types/node@6.0.45 --save-exact',
            workdir='/testbed/lighthouse-cli',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        if exit_code != 0:
            raise Exception(
                f'Failed to pin lighthouse-cli build dependencies for '
                f'{self.instance_id}: {output.decode()}'
            )

        # install-cli's prepublish hook is supposed to build the CLI
        # automatically, but fails silently due to an npm lifecycle
        # working-directory quirk on this old npm version, so the build is
        # triggered explicitly here instead. The install-all layout pairs
        # with build-all the same way install-cli pairs with build-cli --
        # confirmed directly: lighthouse-5688 got past install-all only to
        # hit "Missing script: build-cli", and npm's own suggestion was
        # build-all.
        build_script = (
            'build-all' if install_script == 'install-all' else 'build-cli'
        )
        exit_code, output = self.container.exec_run(
            f'npm run {build_script}',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        if exit_code != 0:
            raise Exception(
                f'Failed to build lighthouse-cli for {self.instance_id} '
                f'(via "{build_script}"): '
                f'{output.decode()}'
            )

        # 3. Create results directory
        self.container.exec_run(f'mkdir -p {RESULTS_DIR}', workdir='/testbed')

        # run-mocha.sh's location is not fixed: lighthouse-5688 (the
        # install-all/build-all layout) has no
        # lighthouse-core/scripts/run-mocha.sh at all. Finding it directly
        # rather than assuming the path avoids repeating that mistake for
        # every future layout difference.
        _, find_output = self.container.exec_run(
            ['find', '/testbed', '-name', 'run-mocha.sh'],
        )
        assert isinstance(find_output, bytes)
        candidates = [
            c for c in find_output.decode().splitlines() if c.strip()
        ]
        if RUN_MOCHA_SCRIPT in candidates:
            run_mocha_script = RUN_MOCHA_SCRIPT
        elif candidates:
            run_mocha_script = candidates[0]
        else:
            raise Exception(
                f'No run-mocha.sh found under /testbed for '
                f'{self.instance_id}'
            )
        if run_mocha_script != RUN_MOCHA_SCRIPT:
            log.info(
                f'run-mocha.sh for {self.instance_id} is at '
                f'{run_mocha_script!r}, not the default {RUN_MOCHA_SCRIPT!r}'
            )
        self._run_mocha_script = run_mocha_script

        # 4. Add the JUnit reporter to each Mocha invocation
        apply_change_literal(
            container=self.container,
            file=self._run_mocha_script,
            find='--timeout 60000;',
            replace=(
                '--timeout 60000 --reporter mocha-junit-reporter'
                f' --reporter-options mochaFile={RESULTS_DIR}/$1.xml;'
            ),
            assertion=(
                'mocha-junit-reporter --reporter-options'
                f' mochaFile={RESULTS_DIR}/$1.xml'
            ),
        )

        log.info('All changes applied successfully.')

    @override
    def evaluate(self) -> list[TestResult]:
        """Run each Mocha suite and retrieve the combined JUnit XML results.

        Returns:
            A list of :class:`TestResult` parsed from all three suites'
            JUnit XML output.

        Raises:
            Exception: If the container has not been started (i.e., ``setup``
                was not called first).
        """

        if self.container is None:
            raise Exception('no container')

        # Run each suite independently (joined with `;`, not `&&`) so that
        # one suite's failures don't prevent the remaining suites from
        # running. ``npm run`` normally prepends node_modules/.bin to PATH;
        # since we invoke the script directly rather than through npm, that
        # prefix is added explicitly (prepended to the container's existing
        # PATH, not replacing it, since node itself may live somewhere
        # non-standard) so ``mocha`` resolves.
        commands = [
            'export PATH="/testbed/node_modules/.bin:$PATH"',
            f'bash {self._run_mocha_script} --cli',
            f'bash {self._run_mocha_script} --core',
            f'bash {self._run_mocha_script} --viewer',
        ]
        exit_code, output = self.container.exec_run(
            ['bash', '-c', '; '.join(commands)],
            workdir='/testbed',
            stream=False,
        )
        log.info('done running')
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        results: list[TestResult] = []
        for suite in SUITES:
            try:
                xml = read_from_container(
                    self.container, f'{RESULTS_DIR}/{suite}.xml'
                )
            except docker.errors.NotFound:
                log.warning(f'No results file found for suite {suite}')
                continue

            results.extend(
                results_xml_to_test_results(
                    self.instance_id,
                    self.patch_type,
                    self.agent_name,
                    xml,
                    self.timestamp,
                )
            )

        return results

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass
