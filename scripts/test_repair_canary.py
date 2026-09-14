"""Unit checks for the isolated AWS repair packager."""

import unittest
from unittest.mock import patch

from botocore.exceptions import EndpointConnectionError

from scripts.repair_canary import aws_retry, minimal_dockerfile


class MinimalDockerfileTests(unittest.TestCase):
    def test_preserves_base_image_and_testbed_workdir(self):
        original = b"""FROM example/image:latest

RUN apt-get update
RUN git clone https://example.invalid/SWE-bench /SWE-bench
WORKDIR /SWE-bench
"""
        self.assertEqual(
            minimal_dockerfile(original),
            b'FROM example/image:latest\n\nWORKDIR /testbed\n',
        )

    def test_requires_base_image(self):
        with self.assertRaisesRegex(ValueError, 'no FROM'):
            minimal_dockerfile(b'RUN true\n')


class AwsRetryTests(unittest.TestCase):
    @patch('scripts.repair_canary.time.sleep')
    def test_retries_transient_endpoint_errors(self, sleep):
        outcomes = [
            EndpointConnectionError(endpoint_url='https://example.invalid'),
            EndpointConnectionError(endpoint_url='https://example.invalid'),
            'ok',
        ]

        def call():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        self.assertEqual(aws_retry(call), 'ok')
        self.assertEqual(sleep.call_count, 2)


if __name__ == '__main__':
    unittest.main()
