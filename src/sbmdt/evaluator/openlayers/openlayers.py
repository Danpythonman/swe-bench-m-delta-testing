"""
Evaluator implementation for OpenLayers repository instances.

Builds a Docker image from the instance's Dockerfile, configures the Karma
test runner to emit JUnit XML output and run headless in a container,
executes the Mocha-based test suite (skipping the separate pixel-diff
rendering suite, which SWE-bench instances' gold patches never touch), and
retrieves the results.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from typing import Any, Final, cast, override

from sbmdt.env import DOCKERFILES_BASE
from sbmdt.evaluator.alibaba.karma_junit_parser import (
    results_xml_to_test_results,
)
from sbmdt.evaluator.base import Evaluator, TestResult
from sbmdt.evaluator.paths import repo_relative
from sbmdt.patches import TEST_PATCH_DIFF_FILENAME, read_diff
from sbmdt.utils import (
    apply_change_regex,
    read_from_container,
    write_to_container,
)

__all__ = [
    'OpenlayersEvaluator',
]

log = logging.getLogger(__name__)

DEFAULT_KARMA_CONFIG_FILE: Final[str] = '/testbed/test/karma.config.js'


RENDERING_CASES_DIR: Final[str] = 'test/rendering/cases/'
NODE_RESULTS_FILE: Final[str] = '/tmp/sbmdt-node-results.json'

# Older checkouts keep the cases under rendering/cases/ at the top level.
_RENDERING_CASE_PATH: Final[re.Pattern[str]] = re.compile(
    r'^(?:test/)?rendering/cases/([^/]+)/'
)

# One line per case from test/rendering/test.js, e.g.
#   case ./cases/icon-opacity/main.js': ok
#   case ./cases/icon-opacity/main.js (note)': mismatch 0.0123
_RENDERING_RESULT: Final[re.Pattern[str]] = re.compile(
    r"case \./cases/(?P<case>[^/\s']+)/main\.js(?: \([^)]*\))?': "
    r'(?P<outcome>ok|mismatch)'
)


def touched_rendering_cases(test_patch: str) -> list[str]:
    """Return the rendering cases a test patch adds or changes.

    Args:
        test_patch: The instance's test patch.

    Returns:
        Case names in diff order, without duplicates.
    """
    cases: list[str] = []
    # Read the diff headers rather than the hunks: a case is often changed
    # only through its expected.png, a binary section with no +++ line.
    for path in re.findall(r'^diff --git a/\S+ b/(\S+)', test_patch, re.M):
        match = _RENDERING_CASE_PATH.match(path)
        if match and match[1] not in cases:
            cases.append(match[1])
    return cases


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
            'npm install karma-junit-reporter regenerator-runtime --save-dev',
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

        # source-map >= 0.7 loads mappings.wasm via SourceMapConsumer
        # initialize(); without that call karma dies mid-run with
        # UnhandledRejection and never flushes junit results.xml
        # (openlayers-11545). Pinning 0.6.x keeps the sync API that
        # webpack 4 / karma expect; --save-exact (not --no-save) so the
        # lockfile / nested resolvers actually see 0.6.x. evaluate() also
        # preloads an initialize() shim in case a nested 0.7+ still wins.
        exit_code, output = self.container.exec_run(
            'npm install source-map@0.6.1 --save-exact',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        log.info(exit_code)
        log.info(output.decode())
        if exit_code != 0:
            raise Exception(
                f'Failed to pin source-map for {self.instance_id}: '
                f'{output.decode()}'
            )

        # npm often strips NODE_OPTIONS, so --require from evaluate() may
        # never run. Prepend the wasm init into the karma config itself so
        # it loads in the same process as karma-server (openlayers-11545).
        source_map_init = '/tmp/sbmdt-source-map-init.js'
        write_to_container(
            self.container,
            source_map_init,
            (
                'try {\n'
                "  var path = require('path');\n"
                "  var sm = require('source-map');\n"
                '  if (sm.SourceMapConsumer && '
                'sm.SourceMapConsumer.initialize) {\n'
                '    var wasm = path.join(\n'
                '      path.dirname(require.resolve(\n'
                "        'source-map/package.json')),\n"
                "      'lib', 'mappings.wasm');\n"
                '    sm.SourceMapConsumer.initialize('
                "{ 'lib/mappings.wasm': wasm });\n"
                '  }\n'
                '} catch (e) {}\n'
            ),
        )
        _, karma_raw = self.container.exec_run(
            ['cat', self._karma_config_file],
        )
        assert isinstance(karma_raw, bytes)
        karma_text = karma_raw.decode()
        init_require = (
            "require('regenerator-runtime/runtime');\n"
            f'require({source_map_init!r});\n'
        )
        if init_require not in karma_text:
            write_to_container(
                self.container,
                self._karma_config_file,
                init_require + karma_text,
            )

        # Requiring the runtime above fixes the Node-side Karma process, but
        # Babel-generated async helpers execute in Chrome. Include the same
        # module as the first browser file so it defines regeneratorRuntime
        # before any independently bundled spec executes (openlayers-13212).
        apply_change_regex(
            container=self.container,
            file=self._karma_config_file,
            find=r'files\s*:\s*\[',
            replace=(
                'files: [\n'
                '      {\n'
                '        pattern: require.resolve('
                "'regenerator-runtime/runtime.js'),\n"
                '        watched: false,\n'
                '      },'
            ),
            assertion="require.resolve('regenerator-runtime/runtime.js')",
        )

        # The checkout's Mocha timeout (2500ms) is tuned for a developer
        # machine. On the 2-vCPU EC2 workers some specs overran it and left
        # map <div>s behind; test-extensions.js then throws "Found extra
        # <div> elements in the body" from a global hook, which ends the
        # Karma run early (openlayers-12172 stopped at 2,609 of 3,276 and
        # never reached the ol.style.Icon specs the patch is about).
        try:
            apply_change_regex(
                container=self.container,
                file=self._karma_config_file,
                find=r'(mocha\s*:\s*\{[^}]*?)timeout\s*:\s*\d+',
                replace=r'\1timeout: 60000',
                assertion='timeout: 60000',
            )
        except Exception as exc:
            log.info(
                f'Skipping Mocha timeout increase for '
                f'{self.instance_id}: {exc}'
            )

        # Force webpack off source maps. Nested source-map@0.7+ still wins
        # over the 0.6.1 pin / initialize shim (openlayers-11545), and karma
        # then dies on mappings.wasm before junit can flush.
        try:
            apply_change_regex(
                container=self.container,
                file=self._karma_config_file,
                find=r"devtool\s*:\s*['\"][^'\"]*['\"]",
                replace='devtool: false',
                assertion='devtool: false',
            )
        except Exception as exc:
            log.info(
                f'Skipping webpack devtool disable for '
                f'{self.instance_id}: {exc}'
            )
        _, karma_for_loader = self.container.exec_run(
            ['cat', self._karma_config_file],
        )
        assert isinstance(karma_for_loader, bytes)
        loader_text = karma_for_loader.decode()
        if 'source-map-loader' in loader_text:
            write_to_container(
                self.container,
                self._karma_config_file,
                re.sub(
                    r"['\"]source-map-loader['\"]\s*,?",
                    '',
                    loader_text,
                    count=1,
                ),
            )
        # Replace any nested source-map@>=0.7 with the pinned 0.6.1 tree.
        exit_code, output = self.container.exec_run(
            [
                'bash',
                '-c',
                'root=$(node -p \'require("path").dirname('
                'require.resolve("source-map/package.json"))\'); '
                'find node_modules -path "*/source-map/package.json" '
                '-print0 | while IFS= read -r -d "" p; do '
                '  ver=$(node -p "require(process.argv[1]).version" "$p"); '
                '  case "$ver" in 0.7*|0.8*|0.9*|1.*) '
                '    d=$(dirname "$p"); rm -rf "$d"; cp -a "$root" "$d";; '
                '  esac; '
                'done',
            ],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        log.info(exit_code)
        log.info(output.decode())

        def _add_junit_reporter(m):
            inner = m.group(1).rstrip()
            sep = ', ' if inner and not inner.endswith(',') else ' '
            # Long suites (openlayers-14945 ~2500 tests) can trip karma's
            # default disconnect/no-activity timeouts; when Chrome is
            # abandoned mid-run, karma-junit-reporter never flushes
            # results.xml. Raise the ceilings next to the reporter config.
            return (
                f"reporters: [{inner}{sep}'junit'],\n"
                '    junitReporter: {\n'
                "      outputDir: 'test-results',\n"
                "      outputFile: 'results.xml',\n"
                '      useBrowserName: false,\n'
                '    },\n'
                '    browserDisconnectTimeout: 20000,\n'
                '    browserDisconnectTolerance: 3,\n'
                '    browserNoActivityTimeout: 120000,\n'
                '    captureTimeout: 120000,'
            )

        apply_change_regex(
            container=self.container,
            file=self._karma_config_file,
            find=r'reporters:\s*\[([^\]]*)\],',
            replace=_add_junit_reporter,
            assertion='junitReporter: {',
        )

        # The old fixed plugin list assumed every instance's package.json
        # carries the same karma-* packages this evaluator happened to be
        # written against. It does not: different commits have different
        # karma plugins installed, and an allowlist naming one that is not
        # there makes karma refuse to start ("Cannot find plugin"), while
        # omitting one that provides a framework/reporter the config
        # actually uses does the same ("No provider for ..."). Discovering
        # what is really in node_modules keeps this working across commits
        # instead of only the one layout the list was written from.
        # -maxdepth 1 keeps this to direct node_modules entries; a scoped
        # package (@scope/karma-*) would sort under its scope directory
        # and needs its own lookup, which the -o branch below adds.
        exit_code, ls_output = self.container.exec_run(
            "sh -c \"find node_modules -maxdepth 1 -iname 'karma-*' "
            "-o -maxdepth 2 -path 'node_modules/@*/karma-*'\"",
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(ls_output, bytes)
        installed = {
            p.rsplit('/', 1)[-1]
            for p in ls_output.decode().splitlines()
            if p.strip()
        }
        # karma-junit-reporter was installed above; the log lines already
        # printed prove that succeeded, so add it explicitly instead of
        # re-listing node_modules to confirm.
        installed.add('karma-junit-reporter')
        # karma-firefox-launcher is excluded on purpose (see the setup()
        # docstring): its load-time probe throws in this container.
        installed.discard('karma-firefox-launcher')
        log.info(
            f'karma plugins discovered for {self.instance_id} '
            f'(exit_code={exit_code}): {sorted(installed)}'
        )
        plugin_lines = ''.join(f"      '{p}',\n" for p in sorted(installed))

        # webpackMiddleware is not guaranteed present -- it is only there
        # if this commit's config configures webpack's dev middleware, and
        # not every commit does. Older configs (openlayers-7554) also name
        # the karma config argument `karma` rather than `config`, so the
        # set() call is `karma.set({` -- matching only `config.set` left
        # those instances unable to inject the plugins allowlist.
        # config/karma.set({ is the one thing every karma config has (it's
        # the config file's whole purpose), so it is the anchor of last
        # resort when webpackMiddleware is absent.
        _, config_content = self.container.exec_run(
            ['cat', self._karma_config_file],
        )
        assert isinstance(config_content, bytes)
        has_webpack_middleware = bool(
            re.search(r'webpackMiddleware\s*:\s*\{', config_content.decode())
        )

        if has_webpack_middleware:
            apply_change_regex(
                container=self.container,
                file=self._karma_config_file,
                find=r'webpackMiddleware\s*:\s*\{',
                replace=(
                    'plugins: [\n'
                    f'{plugin_lines}'
                    '    ],\n'
                    '    webpackMiddleware: {'
                ),
                assertion='plugins: [\n',
            )
        else:
            apply_change_regex(
                container=self.container,
                file=self._karma_config_file,
                find=r'(?:config|karma)\.set\(\s*\{',
                replace=lambda m: (
                    m.group(0) + '\n    plugins: [\n' + plugin_lines + '    ],'
                ),
                assertion='plugins: [\n',
            )

        # This override's base: 'Chrome' comes from karma-chrome-launcher.
        # A commit that launches Chrome itself via puppeteer inside the
        # config (see evaluate()'s shim) has no reason to depend on that
        # plugin and may not have it installed, in which case overriding
        # browsers: to require it breaks a config that was already working
        # ("Cannot load browser 'ChromeNoSandbox': it is not registered!").
        # The puppeteer path already gets --no-sandbox from the binary
        # shim, so skipping this override there loses nothing.
        if 'karma-chrome-launcher' in installed:
            # A config that launches Chrome itself for other purposes (see
            # evaluate()'s shim) may already declare its own
            # customLaunchers: {. A JS object literal with the key
            # written twice does not error -- the later one silently wins
            # -- so inserting a second customLaunchers: { block here would
            # get the whole block discarded, ChromeNoSandbox included,
            # while apply_change_regex's assertion still passes because
            # the text is genuinely there, just inert. Adding the entry
            # inside the existing block when there is one avoids creating
            # that duplicate key.
            _, existing_launchers = self.container.exec_run(
                ['cat', self._karma_config_file],
            )
            assert isinstance(existing_launchers, bytes)
            has_custom_launchers = bool(
                re.search(
                    r'customLaunchers\s*:\s*\{', existing_launchers.decode()
                )
            )

            headless_launcher = re.search(
                r"flags:\s*\[\s*'--headless[^']*'", existing_launchers.decode()
            )
            if headless_launcher:
                # Newer checkouts (openlayers-15683) define their own
                # headless launcher. Swapping in a headed SwiftShader
                # Chrome under xvfb made Chrome 151 disconnect after 16 of
                # 2,515 tests, so keep their launcher and only add the
                # --no-sandbox root needs.
                apply_change_regex(
                    container=self.container,
                    file=self._karma_config_file,
                    find=r"flags:\s*\[(\s*'--headless[^']*')",
                    replace=r"flags: [\1, '--no-sandbox'",
                    assertion="'--no-sandbox'",
                )
            elif has_custom_launchers:
                apply_change_regex(
                    container=self.container,
                    file=self._karma_config_file,
                    find=r'browsers:\s*\[[^\]]*\]\s*,?',
                    replace="browsers: ['ChromeNoSandbox'],",
                    assertion="browsers: ['ChromeNoSandbox'],",
                )
                apply_change_regex(
                    container=self.container,
                    file=self._karma_config_file,
                    find=r'customLaunchers\s*:\s*\{',
                    replace=(
                        'customLaunchers: {\n'
                        '      ChromeNoSandbox: {\n'
                        "        base: 'Chrome',\n"
                        "        flags: ['--no-sandbox', '--enable-webgl', "
                        "'--ignore-gpu-blocklist', "
                        "'--use-angle=swiftshader'],\n"
                        '      },'
                    ),
                    assertion='ChromeNoSandbox: {',
                )
            else:
                apply_change_regex(
                    container=self.container,
                    file=self._karma_config_file,
                    # Older configs (openlayers-7554) omit the trailing
                    # comma after browsers: ['Chrome'] because it is the
                    # last property inside karma.set({...}).
                    find=r'browsers:\s*\[[^\]]*\]\s*,?',
                    replace=(
                        "browsers: ['ChromeNoSandbox'],\n"
                        '    customLaunchers: {\n'
                        '      ChromeNoSandbox: {\n'
                        "        base: 'Chrome',\n"
                        "        flags: ['--no-sandbox', '--enable-webgl', "
                        "'--ignore-gpu-blocklist', "
                        "'--use-angle=swiftshader'],\n"
                        '      },\n'
                        '    },'
                    ),
                    assertion="browsers: ['ChromeNoSandbox'],",
                )
        else:
            log.info(
                f'karma-chrome-launcher not installed for '
                f'{self.instance_id}; leaving browsers: as-is and relying '
                f'on the PUPPETEER_EXECUTABLE_PATH shim for --no-sandbox'
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

        # karma-chrome-launcher (which the browsers: patch targets) finds
        # Chrome on its own without any env var here, so some Chrome binary
        # is already reachable in this container. Some commits' karma
        # config resolves the browser through puppeteer instead, which
        # uses a separate mechanism (PUPPETEER_EXECUTABLE_PATH, or its own
        # download cache) and does not see whatever karma-chrome-launcher
        # found. Locating the same binary and exporting it under
        # puppeteer's variable lets both resolution paths agree instead of
        # puppeteer only working when it happened to download its own copy.
        _, which_output = self.container.exec_run(
            [
                'sh',
                '-c',
                'command -v google-chrome-stable || command -v '
                'google-chrome || command -v chromium || true',
            ],
        )
        assert isinstance(which_output, bytes)
        chrome_path = which_output.decode().strip().splitlines()
        # Pre-init source-map's wasm mapping when a 0.7+ copy is still on
        # the resolve path after the 0.6.1 pin (nested webpack deps).
        source_map_init = '/tmp/sbmdt-source-map-init.js'
        write_to_container(
            self.container,
            source_map_init,
            (
                'try {\n'
                "  var path = require('path');\n"
                "  var sm = require('source-map');\n"
                '  if (sm.SourceMapConsumer && '
                'sm.SourceMapConsumer.initialize) {\n'
                '    var wasm = path.join(\n'
                '      path.dirname(require.resolve(\n'
                "        'source-map/package.json')),\n"
                "      'lib', 'mappings.wasm');\n"
                '    sm.SourceMapConsumer.initialize('
                "{ 'lib/mappings.wasm': wasm });\n"
                '  }\n'
                '} catch (e) {}\n'
            ),
        )
        environment = {
            # webpack 4's MD4-based hashing is incompatible with the
            # default OpenSSL 3 provider bundled with this container's
            # Node version. Also preload the source-map wasm init above.
            'NODE_OPTIONS': (
                f'--openssl-legacy-provider --require {source_map_init}'
            ),
        }
        if chrome_path:
            # Some commits launch Chrome themselves inside karma.config.cjs
            # (via puppeteer directly) rather than through karma-chrome-
            # launcher's browsers: array, so they never see the --no-sandbox
            # flag the browsers: patch adds -- Chrome then refuses to start
            # as root, which is the only user available in this container.
            # Wrapping the real binary in a shim that always adds the flag,
            # and pointing PUPPETEER_EXECUTABLE_PATH at the shim instead of
            # the binary itself, reaches both launch paths without needing
            # to know which one a given commit uses.
            shim_path = '/tmp/chrome-no-sandbox'
            shim_script = (
                f'#!/bin/sh\n'
                f'exec {chrome_path[0]} --no-sandbox --disable-gpu "$@"\n'
            )
            write_to_container(self.container, shim_path, shim_script)
            self.container.exec_run(['chmod', '+x', shim_path])
            environment['PUPPETEER_EXECUTABLE_PATH'] = shim_path
            # karma-chrome-launcher reads CHROME_BIN instead. Configs whose
            # browsers: list falls back to plain 'Chrome' outside CircleCI
            # (openlayers-9083) launch it as root without the flag, and
            # Chrome refuses to start, so those get CHROME_BIN pointed at a
            # no-sandbox shim. Only those: newer configs launch their own
            # Chrome with WebGL flags, and overriding its binary made it
            # disconnect after 16 of 2,515 tests (openlayers-15683).
            _, karma_config = self.container.exec_run(
                ['cat', self._karma_config_file]
            )
            assert isinstance(karma_config, bytes)
            if re.search(rb"browsers:\s*\[\s*'Chrome'\s*\]", karma_config):
                karma_shim_path = '/tmp/chrome-no-sandbox-karma'
                write_to_container(
                    self.container,
                    karma_shim_path,
                    f'#!/bin/sh\nexec {chrome_path[0]} --no-sandbox "$@"\n',
                )
                self.container.exec_run(['chmod', '+x', karma_shim_path])
                environment['CHROME_BIN'] = karma_shim_path
        else:
            log.info(
                f'no system Chrome found for {self.instance_id}; leaving '
                f'PUPPETEER_EXECUTABLE_PATH unset'
            )

        exit_code, output = self.container.exec_run(
            'xvfb-run -a npm run karma -- --single-run --log-level error',
            environment=environment,
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

        return [
            *results_xml_to_test_results(
                self.instance_id,
                self.patch_type,
                self.agent_name,
                results,
                self.timestamp,
            ),
            *self._run_node_tests(environment),
            *self._run_rendering_cases(environment),
        ]

    def _run_node_tests(self, environment: dict[str, str]) -> list[TestResult]:
        """Run the Node-side Mocha suite when the checkout has one.

        Newer checkouts split pure-function specs (ol/coordinate.js,
        ol/extent.js, ...) out of Karma into ``test/node``, run by
        ``npm run test-node``. Without it those tests never ran
        (openlayers-13893's FAIL_TO_PASS tests live there).

        Args:
            environment: The environment the Karma run used.

        Returns:
            One result per Node test, or nothing when there is no
            ``test-node`` script or its report cannot be read.
        """
        assert self.container is not None
        _, scripts = self.container.exec_run(
            [
                'node',
                '-e',
                "process.stdout.write(require('./package.json').scripts"
                "['test-node'] || '')",
            ],
            workdir='/testbed',
        )
        assert isinstance(scripts, bytes)
        if not scripts.strip():
            return []
        exit_code, _ = self.container.exec_run(
            [
                'bash',
                '-c',
                'npm run --silent test-node -- --reporter json '
                f'> {NODE_RESULTS_FILE}',
            ],
            environment=environment,
            workdir='/testbed',
            stream=False,
        )
        log.info(f'test-node exit code: {exit_code}')
        try:
            raw = read_from_container(self.container, NODE_RESULTS_FILE)
        except Exception as exc:
            log.warning(f'No test-node report for {self.instance_id}: {exc}')
            return []
        # Tests may print to stdout around the reporter's JSON document.
        start = raw.find('{\n  "stats"')
        try:
            report = json.loads(raw[start:]) if start >= 0 else None
        except json.JSONDecodeError:
            report = None
        if not isinstance(report, dict):
            log.warning(f'Unreadable test-node report for {self.instance_id}')
            return []
        tests = cast(list[dict[str, Any]], report.get('tests', []))
        failures = cast(list[dict[str, Any]], report.get('failures', []))
        failed = {(t.get('file'), t.get('fullTitle')) for t in failures}
        return [
            TestResult(
                instance_id=self.instance_id,
                patch_type=self.patch_type,
                agent_name=self.agent_name,
                timestamp=self.timestamp,
                test_name=test['fullTitle'],
                passed=(test.get('file'), test['fullTitle']) not in failed,
                test_file=repo_relative(test.get('file')),
            )
            for test in tests
            if test.get('fullTitle')
        ]

    def _run_rendering_cases(
        self, environment: dict[str, str]
    ) -> list[TestResult]:
        """Run the rendering cases the test patch touches.

        Rendering cases are pixel comparisons outside Karma, run by
        ``npm run test-rendering``, which prints one line per case
        (``case ./cases/<name>/main.js': ok`` or ``...': mismatch <n>``).
        A fix to how something is drawn is often tested only by a new or
        updated case, so these are the FAIL_TO_PASS tests of such
        instances. Only touched cases run: the full suite takes long, and
        pixel-exact cases that depend on the GPU can fail here for reasons
        unrelated to the patch.

        Args:
            environment: The environment the Karma run used.

        Returns:
            One result per touched case, named ``rendering <name>``; empty
            when no case is touched or the runner reported none.
        """
        assert self.container is not None
        # The raw test patch, not test_patch_for(): that drops the binary
        # sections, and a case is often changed only through its
        # expected.png (the image itself comes from the image's assets).
        test_patch = DOCKERFILES_BASE / self.instance_id
        cases = touched_rendering_cases(
            read_diff(test_patch / TEST_PATCH_DIFF_FILENAME)
        )
        if not cases:
            return []
        pattern = '|'.join(f'/{re.escape(case)}/' for case in cases)
        command = (
            # --log-level info: passing cases are logged at info level only.
            # --timeout: the first page waits for webpack to bundle the
            # cases, which overran the 60s default on the 2-vCPU workers
            # (openlayers-13823 reported no case at all).
            'xvfb-run -a npm run test-rendering -- --force --log-level info '
            '--timeout 300000 '
            f'--match {shlex.quote(pattern)}'
        )
        log.info(f'Running rendering cases {cases}: {command}')
        exit_code, output = self.container.exec_run(
            ['bash', '-c', f'timeout 1800 {command}'],
            # CI makes the runner headless with --no-sandbox, as root needs.
            environment={**environment, 'CI': 'true'},
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        text = output.decode(errors='replace')
        log.info(f'test-rendering exit code: {exit_code}')
        log.info(text)

        outcomes: dict[str, bool] = {}
        for match in _RENDERING_RESULT.finditer(text):
            outcomes[match['case']] = match['outcome'] == 'ok'
        if not outcomes:
            log.warning(
                f'test-rendering reported no case for {self.instance_id}; '
                f'leaving rendering cases out'
            )
            return []
        # A selected case without a result line crashed its page or had no
        # screenshot to compare, which the runner reports as a failure.
        return [
            TestResult(
                instance_id=self.instance_id,
                patch_type=self.patch_type,
                agent_name=self.agent_name,
                timestamp=self.timestamp,
                test_name=f'rendering {case}',
                passed=outcomes.get(case, False),
                test_file=f'{RENDERING_CASES_DIR}{case}/main.js',
            )
            for case in cases
        ]

    @override
    def pre_cleanup(self) -> None:
        """Pre-cleanup hook. No-op for this evaluator."""
        pass

    @override
    def post_cleanup(self) -> None:
        """Post-cleanup hook. No-op for this evaluator."""
        pass
