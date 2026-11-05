"""The alibaba evaluator puts the Mocha abort shim first in karma's files.

A late async error was ending whole alibaba runs part-way (next-4182 at
117 of 1565 tests, next-2984 at 42 of 1484), in before_patch runs as
well as agent runs. The fixture below is quoted from alibaba-fusion/next
scripts/test/karma.js, the files list the edit has to find.
"""

import unittest

from sbmdt.evaluator.alibaba.alibaba import ABORT_SHIM_FILE, add_abort_shim

KARMA = """    const options = {
        frameworks: ['mocha'],
        browsers: ['Chrome'],
        customLaunchers: {
            ChromeTravis: {
                base: 'ChromeHeadless',
                flags: ['--no-sandbox'],
            },
        },
        reporters: ['spec'],
        preprocessors: {
            [specPath]: ['webpack', 'sourcemap'],
        },
        files: [
            path.join(__dirname, 'animation-polyfill.js'),
            require.resolve('babel-polyfill/dist/polyfill.js'),
            require.resolve('console-polyfill/index.js'),
            require.resolve('es5-shim/es5-shim.js'),
            require.resolve('es5-shim/es5-sham.js'),
            require.resolve('html5shiv/dist/html5shiv.js'),
            specPath,
        ],
"""


class AddAbortShimTest(unittest.TestCase):
    def test_shim_is_the_first_file(self):
        out = add_abort_shim(KARMA)
        files = out[out.index('files: ['):]
        first = files.splitlines()[1].strip()
        self.assertEqual(first, f"'{ABORT_SHIM_FILE}',")
        # everything that was there is still there, in order
        self.assertIn("path.join(__dirname, 'animation-polyfill.js'),", files)
        self.assertLess(files.index(ABORT_SHIM_FILE), files.index('specPath'))

    def test_idempotent(self):
        once = add_abort_shim(KARMA)
        self.assertEqual(add_abort_shim(once), once)
        self.assertEqual(once.count(ABORT_SHIM_FILE), 1)

    def test_no_files_list_is_reported(self):
        with self.assertRaises(ValueError):
            add_abort_shim('module.exports = function (config) {};')

    def test_shim_defuses_abort(self):
        from sbmdt.evaluator.alibaba import alibaba
        self.assertIn('runner.abort = function', alibaba._ABORT_SHIM)


if __name__ == '__main__':
    unittest.main()
