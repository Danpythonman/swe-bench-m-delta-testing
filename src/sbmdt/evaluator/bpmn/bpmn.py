"""
Evaluator implementation for bpmn-js repository instances.

bpmn-js uses Karma as its test runner, configured at
``test/config/karma.unit.js``.  This evaluator:

1. Installs ``karma-junit-reporter`` in the container.
2. Patches the Karma config to emit JUnit XML output and to make
   ChromeHeadless reliably capture inside Docker.
3. Ensures a real Chrome/Chromium binary is present (SWE-bench images
   often set ``PUPPETEER_SKIP_DOWNLOAD``, and Ubuntu's ``chromium-browser``
   package is a non-functional snap stub).
4. Runs ``npm test`` with ``TEST_BROWSERS=ChromeHeadless`` (PhantomJS is
   the config default but cannot parse this project's compiled bundle).
   On Node ≥ 17 also sets ``NODE_OPTIONS=--openssl-legacy-provider``;
   on Node < 17 that flag is omitted because it is rejected by
   ``NODE_OPTIONS`` and aborts before Karma starts.
5. Reads the resulting XML from the container and returns parsed results.

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

KARMA_CONFIG_FILE: Final[str] = '/testbed/test/config/karma.unit.js'
RESULTS_XML: Final[str] = '/testbed/test-results/results.xml'

# Flags needed for headless Chrome as root in Docker. --no-sandbox is
# already upstream for ChromeHeadless_Linux; --disable-dev-shm-usage was
# tried alone and was not enough for capture; --no-proxy-server and an
# ephemeral debugging port address the remaining "launches but never
# captures" failure mode we still see on AWS.
_CHROME_DOCKER_FLAGS: Final[str] = (
    "[\n"
    "          '--no-sandbox',\n"
    "          '--disable-setuid-sandbox',\n"
    "          '--disable-dev-shm-usage',\n"
    "          '--disable-gpu',\n"
    "          '--no-proxy-server',\n"
    "          '--remote-debugging-port=0'\n"
    "        ]"
)


class BpmnEvaluator(Evaluator):
    """Evaluator for bpmn-io/bpmn-js benchmark instances."""

    @override
    def setup(self) -> None:
        """Install the JUnit reporter and patch Karma for Docker Chrome."""

        if self.container is None:
            raise Exception('no container')

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

        self._ensure_chrome_binary()
        self._patch_chrome_bin_assignment()

        apply_change_regex(
            container=self.container,
            file=KARMA_CONFIG_FILE,
            find=r'(reporters:\s*.+?)(\s*,\s*\n)',
            replace=lambda m: f"{m.group(1)}.concat('junit'){m.group(2)}",
            assertion="concat('junit')",
        )

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
                # Force IPv4 so Chrome does not open http://localhost:9876
                # on ::1 while Karma listens on 127.0.0.1.
                "\n    hostname: '127.0.0.1',"
                "\n    listenAddress: '127.0.0.1',"
            ),
            assertion='junitReporter:',
        )

        self._ensure_chrome_docker_launcher()

        log.info('BpmnEvaluator setup complete for %s', self.instance_id)

    def _ensure_chrome_binary(self) -> None:
        """Make sure a runnable Chrome/Chromium binary exists for Karma."""
        assert self.container is not None

        check_script = (
            "const fs=require('fs');"
            "let p='';"
            "try{p=require('puppeteer').executablePath()}catch(e){}"
            "console.log(p&&fs.existsSync(p)?'ok:'+p:'missing:'+p)"
        )
        exit_code, output = self.container.exec_run(
            ['node', '-e', check_script],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        status = output.decode().strip()
        log.info('puppeteer chrome check: %s (exit_code=%s)', status, exit_code)
        if status.startswith('ok:'):
            return

        # Prefer an already-installed system Chrome (openlayers images and
        # some bpmn images already have one).
        exit_code, output = self.container.exec_run(
            [
                'bash',
                '-lc',
                'command -v google-chrome-stable || '
                'command -v google-chrome || '
                'command -v chromium || true',
            ],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        system_chrome = output.decode().strip().splitlines()
        if system_chrome and system_chrome[0]:
            log.info('Found system Chrome at %s', system_chrome[0])
            return

        # SWE-bench images set PUPPETEER_SKIP_DOWNLOAD — clear it.
        for cmd in (
            'env -u PUPPETEER_SKIP_DOWNLOAD -u PUPPETEER_SKIP_CHROMIUM_DOWNLOAD '
            'node node_modules/puppeteer/install.js',
            'env -u PUPPETEER_SKIP_DOWNLOAD -u PUPPETEER_SKIP_CHROMIUM_DOWNLOAD '
            'npx --yes puppeteer browsers install chrome',
        ):
            log.info('Attempting Chromium download via: %s', cmd)
            exit_code, output = self.container.exec_run(
                ['bash', '-lc', cmd],
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(output, bytes)
            log.info('chromium download exit_code=%s', exit_code)
            log.info(output.decode()[-4000:])
            exit_code, output = self.container.exec_run(
                ['node', '-e', check_script],
                workdir='/testbed',
                stream=False,
            )
            assert isinstance(output, bytes)
            status = output.decode().strip()
            log.info('puppeteer chrome check after download: %s', status)
            if status.startswith('ok:'):
                return

        # Do NOT apt-install chromium-browser: on Ubuntu 22.04 it is a snap
        # stub that prints "requires the chromium snap" and exits.
        log.info('Falling back to Google Chrome .deb install')
        exit_code, output = self.container.exec_run(
            [
                'bash',
                '-lc',
                'set -euo pipefail; '
                'export DEBIAN_FRONTEND=noninteractive; '
                'apt-get update -qq; '
                'apt-get install -y -qq wget ca-certificates; '
                'wget -q -O /tmp/chrome.deb '
                'https://dl.google.com/linux/direct/'
                'google-chrome-stable_current_amd64.deb; '
                'apt-get install -y -qq /tmp/chrome.deb || '
                '(dpkg -i /tmp/chrome.deb; apt-get install -y -f -qq); '
                'test -x /usr/bin/google-chrome-stable',
            ],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        log.info('google-chrome install exit_code=%s', exit_code)
        log.info(output.decode()[-2000:])
        if exit_code != 0:
            raise Exception(
                'No Chrome/Chromium binary available for Karma: '
                f'{output.decode()[-1000:]}'
            )

    def _patch_chrome_bin_assignment(self) -> None:
        """Stop karma.unit.js from pointing CHROME_BIN at a missing path."""
        assert self.container is not None
        content = read_from_container(self.container, KARMA_CONFIG_FILE)
        needle = (
            "process.env.CHROME_BIN = require('puppeteer').executablePath();"
        )
        if needle not in content:
            return
        replacement = (
            "(function() {"
            "  var fs = require('fs');"
            "  var p = null;"
            "  try { p = require('puppeteer').executablePath(); } catch (e) {}"
            "  if (p && fs.existsSync(p)) { process.env.CHROME_BIN = p; }"
            "})();"
        )
        write_to_container(
            self.container,
            KARMA_CONFIG_FILE,
            content.replace(needle, replacement, 1),
        )
        log.info('Patched unconditional puppeteer CHROME_BIN assignment')

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
        """Build env vars for ``npm test``."""
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
            # Keep local-batch-work's selection: the config maps
            # ChromeHeadless → ChromeHeadless_Linux on Linux.
            'TEST_BROWSERS': 'ChromeHeadless',
            'NO_PROXY': 'localhost,127.0.0.1,::1',
            'no_proxy': 'localhost,127.0.0.1,::1',
        }

        locate_script = (
            "const fs=require('fs');"
            "const candidates=[];"
            "try{candidates.push(require('puppeteer').executablePath())}catch(e){}"
            "candidates.push("
            "'/usr/bin/google-chrome-stable','/usr/bin/google-chrome',"
            "'/usr/bin/chromium');"
            "for (const p of candidates){"
            "  if(p&&fs.existsSync(p)){console.log(p);process.exit(0)}"
            "}"
            "process.exit(1)"
        )
        exit_code, output = self.container.exec_run(
            ['node', '-e', locate_script],
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)
        chrome_bin = output.decode().strip()
        if exit_code == 0 and chrome_bin:
            # Same pattern as openlayers: wrap so --no-sandbox is always on,
            # including paths that launch Chrome through puppeteer itself.
            shim = '/tmp/chrome-no-sandbox'
            write_to_container(
                self.container,
                shim,
                f'#!/bin/bash\nexec {chrome_bin} --no-sandbox --disable-gpu "$@"\n',
            )
            self.container.exec_run(['chmod', '+x', shim], stream=False)
            env['CHROME_BIN'] = shim
            env['PUPPETEER_EXECUTABLE_PATH'] = shim
            log.info('Using CHROME_BIN shim -> %s', chrome_bin)
        else:
            log.warning('Could not locate a Chrome binary to set CHROME_BIN')

        try:
            major = int(node_version.split('.', 1)[0])
        except ValueError:
            major = 0
            log.warning('Could not parse node version %r', node_version)

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
        """Run ``npm test`` and retrieve the JUnit XML results."""

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

        # junitReporter's outputDir is relative to karma's basePath, which
        # is not always /testbed. Prefer the configured path when present
        # and otherwise take whatever karma actually wrote (same pattern
        # as openlayers/alibaba/carbon).
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
        written = [
            p for p in results_find.decode().splitlines() if p.strip()
        ]
        if RESULTS_XML in written:
            results_file = RESULTS_XML
        elif written:
            results_file = written[0]
            log.info(
                'results.xml for %s is at %r, not the expected %r',
                self.instance_id,
                results_file,
                RESULTS_XML,
            )
        else:
            raise Exception(
                f'karma wrote no results.xml for {self.instance_id}; '
                f'expected it at {RESULTS_XML!r}'
            )

        results_xml = read_from_container(self.container, results_file)

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
