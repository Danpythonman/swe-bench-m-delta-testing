"""quarto runs only the tests an instance's test patch touches."""

import unittest

from sbmdt.evaluator.quarto.quarto import SMOKE_ALL, tests_for_patch


def patch(*paths):
    return ''.join(f'diff --git a/{p} b/{p}\n+x\n' for p in paths)


class QuartoTestSelection(unittest.TestCase):

    def test_a_test_file_is_run_directly(self):
        self.assertEqual(
            tests_for_patch(patch('tests/docs/callouts.qmd',
                                  'tests/smoke/render/render-callout.test.ts')),
            (['smoke/render/render-callout.test.ts'], None))

    def test_a_smoke_all_document_becomes_the_smoke_all_glob(self):
        doc = 'tests/docs/smoke-all/2023/01/23/reveal-config-quote-4063.qmd'
        self.assertEqual(tests_for_patch(patch(doc)),
                         ([SMOKE_ALL], doc[len('tests/'):]))

    def test_several_documents_are_one_brace_glob(self):
        files, glob = tests_for_patch(patch(
            'tests/docs/smoke-all/a.qmd', 'tests/docs/smoke-all/b.ipynb'))
        self.assertEqual(files, [SMOKE_ALL])
        self.assertEqual(glob,
                         '{docs/smoke-all/a.qmd,docs/smoke-all/b.ipynb}')

    def test_nothing_selectable_means_the_whole_suite(self):
        self.assertEqual(tests_for_patch(patch('src/core/x.ts')), ([], None))


if __name__ == '__main__':
    unittest.main()
