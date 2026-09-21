"""
Evaluator implementation for Lighthouse repository instances.

Builds a Docker image from the instance's Dockerfile, configures Mocha to
emit JUnit XML output via ``mocha-junit-reporter``, runs the three test
suites (CLI, core, viewer) independently, and retrieves the combined
results.
"""

from __future__ import annotations

import json
import logging
import re
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
        package_data = json.loads(package_json)
        scripts = package_data.get('scripts', {})
        _, find_output = self.container.exec_run(
            ['find', '/testbed', '-name', 'run-mocha.sh'],
        )
        assert isinstance(find_output, bytes)
        mocha_scripts = [
            item for item in find_output.decode().splitlines() if item.strip()
        ]
        direct_suites = {
            suite: scripts.get(f'unit-{suite}')
            for suite in ('core', 'cli', 'viewer')
            if scripts.get(f'unit-{suite}')
        }
        modern_layout = (
            '"type": "module"' in package_json
            or not mocha_scripts
            or not ({'install-cli', 'install-all'} & scripts.keys())
        )
        if modern_layout and direct_suites:
            install = (
                'yarn install --ignore-scripts --non-interactive '
                '--network-timeout 300000'
                if self.container.exec_run(
                    ['test', '-f', '/testbed/yarn.lock']
                )[0]
                == 0
                else 'npm install --ignore-scripts --legacy-peer-deps'
            )
            exit_code, output = self.container.exec_run(
                install,
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(output, bytes)
            log.info(exit_code)
            log.info(output.decode())
            if exit_code != 0:
                raise Exception(
                    f'Failed to install direct-test dependencies for '
                    f'{self.instance_id}: {output.decode()}'
                )
            self._direct_suites = direct_suites
            custom_runner = '/testbed/core/test/scripts/run-mocha-tests.js'
            if self.container.exec_run(['test', '-f', custom_runner])[0] == 0:
                apply_change_literal(
                    container=self.container,
                    file=custom_runner,
                    find='const mocha = new Mocha({\n      rootHooks,',
                    replace=(
                        "const mocha = new Mocha({\n      reporter: 'json',"
                        '\n      rootHooks,'
                    ),
                    assertion="reporter: 'json'",
                )
                self._custom_mocha_runner = True
            self.container.exec_run(
                f'mkdir -p {RESULTS_DIR}', workdir='/testbed'
            )
            log.info(
                'Using direct Lighthouse unit scripts for %s: %s',
                self.instance_id,
                direct_suites,
            )
            return

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
            'console.log(JSON.stringify('
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
                f"cannot install lighthouse-cli's dependencies the way "
                f'it does for the commits it was written against. '
                f'Scripts available: {scripts_output.decode()!r}'
            )

        install_definition = scripts.get(install_script, '')
        if (
            install_script == 'install-cli'
            and 'yarn install' in install_definition
            and 'yarn build' in install_definition
        ):
            # Some legacy install-cli scripts immediately build with whatever
            # version a floating TypeScript range resolves to today. Install
            # without lifecycle scripts first; setup pins the checkout's
            # declared compiler below and performs the build exactly once.
            exit_code, output = self.container.exec_run(
                'yarn install --ignore-scripts --non-interactive',
                workdir='/testbed/lighthouse-cli',
                stream=False,
            )
        else:
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

        if (
            self.container.exec_run(
                ['test', '-f', '/testbed/chrome-launcher/package.json']
            )[0]
            == 0
        ):
            exit_code, output = self.container.exec_run(
                'yarn install --ignore-scripts --non-interactive',
                workdir='/testbed/chrome-launcher',
                stream=False,
            )
            assert isinstance(output, bytes)
            log.info(exit_code)
            log.info(output.decode())
            if exit_code != 0:
                raise Exception(
                    f'Failed to install chrome-launcher dependencies for '
                    f'{self.instance_id}: {output.decode()}'
                )

        # Only the multi-package layout has a lighthouse-cli manifest to
        # pin. The older single-package checkouts still have a
        # lighthouse-cli directory - lighthouse-4036's test patch writes
        # into lighthouse-cli/test/fixtures - but no package.json inside
        # it, and reading one that is not there aborted the whole run
        # with a docker 404 before any test executed. Guard it the same
        # way chrome-launcher above is guarded. With no manifest there is
        # no declared TypeScript range, so version_match stays unset and
        # the build below falls back to the compatibility pin that exists
        # for exactly this older layout.
        version_match = None
        has_cli_manifest = self.container.exec_run(
            ['test', '-f', '/testbed/lighthouse-cli/package.json']
        )[0] == 0
        if not has_cli_manifest:
            log.info(
                f'{self.instance_id} has no lighthouse-cli/package.json; '
                f'skipping the sub-package dependency pins'
            )
        else:
            # lighthouse-cli/package.json declares loose historical ranges.
            # Installing them today selects much newer releases within those
            # ranges, which do not necessarily compile these old checkouts.
            # Pin @types/node and TypeScript to the minimum version this
            # checkout's own manifest declares.
            #
            # The manifest has to be read *before* the pin, not after:
            # `npm install --save-exact` rewrites the very field we are
            # trying to read, so reading afterwards only ever reports back
            # whatever we just wrote.
            cli_package = json.loads(
                read_from_container(
                    self.container, '/testbed/lighthouse-cli/package.json'
                )
            )
            dependencies = {
                **cli_package.get('dependencies', {}),
                **cli_package.get('devDependencies', {}),
            }

            # @types/node used to be pinned flat at 6.0.45 for every
            # instance. Ten of these manifests ask for "^6.0.45", where
            # taking the floor is exactly right -- the caret is what
            # resolves to something far newer today. But six ask for
            # "6.0.66" *exactly*, and forcing those backwards is not a
            # conservative choice, it is a wrong one: 6.0.66 is the release
            # that added `isTTY` to WritableStream, and lighthouse-3692's
            # sentry-prompt.ts opens with `if (!process.stdout.isTTY ...)`.
            # Under 6.0.45 that is TS2339, tsc exits 2, and the instance
            # produced no results at all. Same floor rule as TypeScript
            # below, so a declared exact version is honoured and only a
            # manifest that declares nothing falls back to the baseline.
            types_node_match = re.search(
                r'\d+\.\d+\.\d+', dependencies.get('@types/node') or ''
            )
            types_node_version = (
                types_node_match.group() if types_node_match else '6.0.45'
            )
            exit_code, output = self.container.exec_run(
                f'npm install @types/node@{types_node_version} --save-exact',
                workdir='/testbed/lighthouse-cli',
                stream=False,
            )
            assert isinstance(output, bytes)

            log.info(exit_code)
            log.info(output.decode())

            if exit_code != 0:
                raise Exception(
                    f'Failed to pin lighthouse-cli @types/node@'
                    f'{types_node_version} for '
                    f'{self.instance_id}: {output.decode()}'
                )

            typescript_range = dependencies.get('typescript')
            version_match = re.search(
                r'\d+\.\d+\.\d+', typescript_range or ''
            )
            if version_match:
                typescript_version = version_match.group()
                exit_code, output = self.container.exec_run(
                    f'npm install typescript@{typescript_version} '
                    f'--save-exact',
                    workdir='/testbed/lighthouse-cli',
                    stream=False,
                )
                assert isinstance(output, bytes)
                log.info(exit_code)
                log.info(output.decode())
                if exit_code != 0:
                    raise Exception(
                        f'Failed to pin typescript@{typescript_version} for '
                        f'{self.instance_id}: {output.decode()}'
                    )

            # The two pins above are npm operations inside a tree yarn
            # built. npm does not edit a tree in place: it re-resolves the
            # whole thing from package.json and prunes whatever its own
            # resolution does not reach, which here means it deletes most
            # of what yarn installed -- "removed 186 packages" in the
            # lighthouse-3442 log. yargs-parser is one of the casualties,
            # so the run reaches the test suite and dies on
            #
            #     Error: Cannot find module 'yargs-parser'
            #
            # from lighthouse-cli/run.js, after both installs reported
            # success. Re-running yarn restores the tree it knows how to
            # build, and because --save-exact wrote the pins into
            # package.json first, yarn installs those exact versions
            # rather than undoing them.
            exit_code, output = self.container.exec_run(
                'yarn install --ignore-scripts --non-interactive',
                workdir='/testbed/lighthouse-cli',
                stream=False,
            )
            assert isinstance(output, bytes)
            log.info(exit_code)
            log.info(output.decode())
            if exit_code != 0:
                raise Exception(
                    f'Failed to restore lighthouse-cli dependencies after '
                    f'pinning for {self.instance_id}: {output.decode()}'
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
            build_log = output.decode()
            # Some manifests omit TypeScript. For those only, retain the
            # compatibility fallback established for the oldest layout.
            needs_ts_pin = not version_match and (
                'reference' in build_log.lower()
                or 'TS2304' in build_log
                or 'Cannot find type definition' in build_log
                or '@types/node' in build_log
            )
            if needs_ts_pin:
                log.info(
                    f'build-cli failed for {self.instance_id}; retrying '
                    'after pinning typescript@2.0.3'
                )
                pin_code, pin_out = self.container.exec_run(
                    'npm install typescript@2.0.3 --save-exact',
                    workdir='/testbed/lighthouse-cli',
                    stream=False,
                )
                assert isinstance(pin_out, bytes)
                log.info(pin_code)
                log.info(pin_out.decode())
                if pin_code != 0:
                    raise Exception(
                        f'Failed to pin typescript for {self.instance_id}: '
                        f'{pin_out.decode()}'
                    )
                exit_code, output = self.container.exec_run(
                    f'npm run {build_script}',
                    workdir='/testbed',
                    stream=False,
                )
                assert isinstance(output, bytes)
                log.info(exit_code)
                log.info(output.decode())
                build_log = output.decode()

            if exit_code != 0:
                raise Exception(
                    f'Failed to build lighthouse-cli for {self.instance_id} '
                    f'(via "{build_script}"): '
                    f'{build_log}'
                )

        if (
            str(package_data.get('version', '')).startswith('1.')
            and self.container.exec_run(
                ['test', '-f', '/testbed/gulpfile.js']
            )[0]
            == 0
        ):
            exit_code, output = self.container.exec_run(
                './node_modules/.bin/gulp',
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(output, bytes)
            log.info(exit_code)
            log.info(output.decode())
            if exit_code != 0:
                raise Exception(
                    f'Failed to generate Lighthouse test assets for '
                    f'{self.instance_id}: {output.decode()}'
                )
        elif str(package_data.get('version', '')).startswith('2.'):
            # Gulp 3 aborts inside V8 on the newer Node runtime in the
            # benchmark image. Reproduce its two small Handlebars compilation
            # tasks directly while preserving the generated module interface.
            compile_templates = r"""
const fs = require('fs');
const path = require('path');
const Handlebars = require('handlebars');
function compile(source, destination, type) {
  fs.mkdirSync(path.dirname(destination), {recursive: true});
  const lines = [
    "'use strict';",
    "const Handlebars = require('handlebars/runtime');",
    `exports.report = {${type}: {}};`,
  ];
  for (const file of fs.readdirSync(source).filter(f => f.endsWith('.html'))) {
        const name = path.basename(file, '.html');
    const input = fs.readFileSync(path.join(source, file), 'utf8');
    const template = Handlebars.precompile(input);
    const key = JSON.stringify(name);
    lines.push(
      `exports.report.${type}[${key}] = Handlebars.template(${template});`
    );
  }
  fs.writeFileSync(destination, lines.join('\n') + '\n');
}
compile(
  'lighthouse-core/report/templates',
  'lighthouse-core/report/templates/report-templates.js',
  'templates'
);
compile(
  'lighthouse-core/report/partials',
  'lighthouse-core/report/partials/templates/report-partials.js',
  'partials'
);
"""
            exit_code, output = self.container.exec_run(
                ['node', '-e', compile_templates],
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(output, bytes)
            log.info(exit_code)
            log.info(output.decode())
            if exit_code != 0:
                raise Exception(
                    f'Failed to compile Lighthouse 2.x report templates for '
                    f'{self.instance_id}: {output.decode()}'
                )

        # Lighthouse 2.x launches more than one Chrome target.  Its bundled
        # Chrome rejects that pattern in old ``--headless`` mode, while these
        # historical images do not always include a virtual display.
        xvfb_code, xvfb_output = self.container.exec_run(
            [
                'bash',
                '-lc',
                'command -v xvfb-run || '
                '(apt-get update -qq && apt-get install -y -qq xvfb)',
            ],
            workdir='/testbed',
        )
        assert isinstance(xvfb_output, bytes)
        log.info(xvfb_code)
        log.info(xvfb_output.decode())
        if xvfb_code != 0:
            raise Exception(
                f'Failed to install Xvfb for {self.instance_id}: '
                f'{xvfb_output.decode()}'
            )

        wrapper = '/tmp/sbmdt-chrome-wrapper'
        wrapper_script = (
            'chrome=$(command -v google-chrome-stable || '
            'command -v google-chrome || command -v chromium); '
            'test -n "$chrome"; '
            f'printf \'#!/bin/sh\\nchrome="%s"\\n'
            f'if command -v xvfb-run >/dev/null 2>&1; then '
            f'exec xvfb-run -a "$chrome" --no-sandbox --disable-gpu "$@"; '
            'else exec "$chrome" --no-sandbox --headless --disable-gpu '
            '"$@"; fi\\n\' '
            f'"$chrome" > {wrapper}; chmod +x {wrapper}'
        )
        wrapper_code, _ = self.container.exec_run(
            ['bash', '-lc', wrapper_script], workdir='/testbed'
        )
        if wrapper_code == 0:
            self._chrome_wrapper = wrapper

        # 3. Create results directory
        self.container.exec_run(f'mkdir -p {RESULTS_DIR}', workdir='/testbed')

        # run-mocha.sh's location is not fixed: lighthouse-5688 (the
        # install-all/build-all layout) has no
        # lighthouse-core/scripts/run-mocha.sh at all. Finding it directly
        # rather than assuming the path avoids repeating that mistake for
        # every future layout difference.
        candidates = mocha_scripts
        if RUN_MOCHA_SCRIPT in candidates:
            run_mocha_script = RUN_MOCHA_SCRIPT
        elif candidates:
            run_mocha_script = candidates[0]
        else:
            raise Exception(
                f'No run-mocha.sh found under /testbed for {self.instance_id}'
            )
        if run_mocha_script != RUN_MOCHA_SCRIPT:
            log.info(
                f'run-mocha.sh for {self.instance_id} is at '
                f'{run_mocha_script!r}, not the default {RUN_MOCHA_SCRIPT!r}'
            )
        self._run_mocha_script = run_mocha_script
        mocha_source = read_from_container(
            self.container, self._run_mocha_script
        )

        # 4. Add the JUnit reporter to each Mocha invocation.
        #
        # The anchor is the file-list expansion, not '--timeout 60000;'.
        # The timeout is one revision's spelling of this line rather than
        # anything the script guarantees: across the 18 lighthouse
        # instances that ship a run-mocha.sh, _runmocha is written three
        # different ways, and lighthouse-4301 predates the timeout
        # entirely --
        #
        #     mocha --reporter dot $2 $(find $1/test -name '*-test.js');
        #
        # so the literal was absent, setup raised "Could not find target
        # string", and the instance produced no results at all. The find
        # expression is in all 18 and exactly once in each, so it anchors
        # every spelling without becoming ambiguous.
        #
        # Appending after it keeps mocha's own argument order intact: the
        # reporter flags land between the file list and whatever trailed
        # it, which is where they already sat for the 17 instances this
        # step used to handle.
        mocha_files = "$(find $1/test -name '*-test.js')"
        reporter = (
            ' --reporter mocha-junit-reporter'
            f' --reporter-options mochaFile={RESULTS_DIR}/$1.xml'
        )

        # 4301 is also the one revision with no --timeout, which leaves
        # mocha on its 2s default. The other 17 raised it to 60s because
        # these suites launch a real Chrome and do not finish in two
        # seconds; a timeout that short would report failures that are
        # purely a clock. Both sides of the comparison get the same
        # value, so supplying it where the checkout omits it removes
        # noise rather than adding bias.
        timeout = '' if '--timeout' in mocha_source else ' --timeout 60000'

        apply_change_literal(
            container=self.container,
            file=self._run_mocha_script,
            find=mocha_files,
            replace=mocha_files + reporter + timeout,
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

        if hasattr(self, '_direct_suites'):
            return self._evaluate_direct_suites()

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
        if hasattr(self, '_chrome_wrapper'):
            commands.insert(
                1,
                f'export LIGHTHOUSE_CHROMIUM_PATH={self._chrome_wrapper}',
            )
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
        missing_suites = []
        for suite in SUITES:
            try:
                xml = read_from_container(
                    self.container, f'{RESULTS_DIR}/{suite}.xml'
                )
            except docker.errors.NotFound:
                log.warning(f'No results file found for suite {suite}')
                missing_suites.append(suite)
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

        if missing_suites:
            raise Exception(
                'Lighthouse test result files missing for: '
                + ', '.join(missing_suites)
            )
        return results

    def _evaluate_direct_suites(self) -> list[TestResult]:
        """Run checkout-native Jest/Mocha unit scripts and parse JSON."""
        assert self.container is not None
        results: list[TestResult] = []
        for suite, script in self._direct_suites.items():
            path = f'{RESULTS_DIR}/lighthouse-{suite}.json'
            is_jest = bool(re.search(r'(^|\s)jest(?:\s|$)', script))
            if is_jest:
                # Preserve Jest's worker pool. Large Lighthouse suites such as
                # lighthouse-12067 exceed the worker command timeout when
                # artificially serialized with --runInBand.
                command = f'{script} --json --outputFile={path}'
            elif hasattr(self, '_custom_mocha_runner'):
                command = script.replace('yarn mocha', 'yarn --silent mocha')
                command = f'{command} > {path}'
            else:
                command = re.sub(r'--reporter\s+\S+', '', script)
                command = command.replace('yarn mocha', 'yarn --silent mocha')
                command = f'{command} --reporter json > {path}'
            exports = 'export PATH=/testbed/node_modules/.bin:$PATH'
            if hasattr(self, '_chrome_wrapper'):
                exports += (
                    f'; export LIGHTHOUSE_CHROMIUM_PATH={self._chrome_wrapper}'
                )
            command = f'{exports}; {command}'
            exit_code, output = self.container.exec_run(
                ['bash', '-lc', command],
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(output, bytes)
            log.info('%s direct unit exit code: %s', suite, exit_code)
            log.info(output.decode())
            try:
                raw = read_from_container(self.container, path)
            except docker.errors.NotFound:
                log.warning('No direct JSON result found for %s', suite)
                continue
            # Test output can contain braces before or after Mocha's JSON
            # reporter payload. Decode each possible object boundary and keep
            # the actual test report instead of assuming the first/last brace
            # encloses one valid JSON document.
            reports = []
            decoder = json.JSONDecoder()
            for match in re.finditer(r'{', raw):
                try:
                    candidate, _ = decoder.raw_decode(raw[match.start() :])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and (
                    'testResults' in candidate
                    or (
                        isinstance(candidate.get('tests'), list)
                        and isinstance(candidate.get('failures'), list)
                    )
                ):
                    reports.append(candidate)
            if not reports:
                log.warning(
                    'Invalid direct JSON result for %s: %r', suite, raw[:2000]
                )
                continue
            if is_jest:
                report = reports[0]
                for file_result in report.get('testResults', []):
                    for test in file_result.get('assertionResults', []):
                        name = ' '.join(
                            [*test.get('ancestorTitles', []), test['title']]
                        )
                        results.append(
                            TestResult(
                                instance_id=self.instance_id,
                                patch_type=self.patch_type,
                                agent_name=self.agent_name,
                                timestamp=self.timestamp,
                                test_name=name,
                                passed=test.get('status') == 'passed',
                            )
                        )
            else:
                for report in reports:
                    failed = {
                        test.get('fullTitle') or test.get('title', '')
                        for test in report.get('failures', [])
                    }
                    for test in report.get('tests', []):
                        name = test.get('fullTitle') or test.get('title', '')
                        results.append(
                            TestResult(
                                instance_id=self.instance_id,
                                patch_type=self.patch_type,
                                agent_name=self.agent_name,
                                timestamp=self.timestamp,
                                test_name=name,
                                passed=name not in failed,
                            )
                        )
        if not results:
            raise Exception(
                f'Direct Lighthouse unit scripts produced no results for '
                f'{self.instance_id}'
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
