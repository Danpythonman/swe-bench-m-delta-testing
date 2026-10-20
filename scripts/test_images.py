"""Unit checks for base-image parsing and digest pinning."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sbmdt.images import (
    DOCKER_HUB,
    ECR_DOCKER_HUB,
    BaseImage,
    dockerfile_header,
    parse_base_image,
    registry,
)
from scripts.pin_base_images import pin

REPO = 'swebench/sweb.eval.x86_64.django_1776_django-11099'
DIGEST = 'sha256:' + 'a' * 64
NEW_DIGEST = 'sha256:' + 'b' * 64
BODY = '\nWORKDIR /install\nRUN apt-get update\n\nWORKDIR /testbed\n'


class ParseTests(unittest.TestCase):
    def test_old_ecr_form(self):
        image = parse_base_image(f'\nFROM {ECR_DOCKER_HUB}/{REPO}:latest\n')
        self.assertEqual(image, BaseImage(REPO, 'latest', None))

    def test_docker_hub_form(self):
        image = parse_base_image(f'FROM docker.io/{REPO}:latest\n')
        self.assertEqual(image, BaseImage(REPO, 'latest', None))

    def test_pinned_form_round_trips(self):
        image = BaseImage(REPO, None, DIGEST)
        self.assertEqual(parse_base_image(dockerfile_header(image)), image)

    def test_header(self):
        self.assertEqual(
            dockerfile_header(BaseImage(REPO, None, DIGEST)),
            f'ARG REGISTRY=docker.io\nFROM ${{REGISTRY}}/{REPO}@{DIGEST}\n',
        )

    def test_reference_prefers_digest(self):
        image = BaseImage(REPO, 'latest', DIGEST)
        self.assertEqual(
            image.reference(ECR_DOCKER_HUB),
            f'{ECR_DOCKER_HUB}/{REPO}@{DIGEST}',
        )

    def test_no_swebench_image(self):
        with self.assertRaises(ValueError):
            parse_base_image('FROM python:3.12\n')

    def test_registry_defaults_to_docker_hub(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(registry(), DOCKER_HUB)
        with patch.dict(os.environ, {'SBMDT_REGISTRY': ECR_DOCKER_HUB}):
            self.assertEqual(registry(), ECR_DOCKER_HUB)


class PinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dockerfile = Path(self.tmp.name) / 'Dockerfile'

    def tearDown(self):
        self.tmp.cleanup()

    @patch('scripts.pin_base_images.docker_hub_digest', return_value=DIGEST)
    def test_pins_and_keeps_the_rest(self, _):
        # SWE-bench M Dockerfiles start with a blank line.
        self.dockerfile.write_text(
            f'\nFROM {ECR_DOCKER_HUB}/{REPO}:latest\n' + BODY
        )
        self.assertEqual(pin(self.dockerfile, False, False), 'pinned')
        self.assertEqual(
            self.dockerfile.read_text(),
            dockerfile_header(BaseImage(REPO, None, DIGEST)) + BODY,
        )

    @patch('scripts.pin_base_images.docker_hub_digest')
    def test_leaves_pinned_alone(self, lookup):
        text = dockerfile_header(BaseImage(REPO, None, DIGEST)) + BODY
        self.dockerfile.write_text(text)
        self.assertEqual(pin(self.dockerfile, False, False), 'already pinned')
        lookup.assert_not_called()
        self.assertEqual(self.dockerfile.read_text(), text)

    @patch(
        'scripts.pin_base_images.docker_hub_digest', return_value=NEW_DIGEST
    )
    def test_repin_replaces_digest(self, _):
        self.dockerfile.write_text(
            dockerfile_header(BaseImage(REPO, None, DIGEST)) + BODY
        )
        self.assertEqual(pin(self.dockerfile, True, False), 'repinned')
        self.assertIn(NEW_DIGEST, self.dockerfile.read_text())
        self.assertTrue(self.dockerfile.read_text().endswith(BODY))

    @patch('scripts.pin_base_images.docker_hub_digest')
    def test_check_changes_nothing(self, lookup):
        text = f'FROM {ECR_DOCKER_HUB}/{REPO}:latest\n' + BODY
        self.dockerfile.write_text(text)
        self.assertEqual(pin(self.dockerfile, False, True), 'unpinned')
        lookup.assert_not_called()
        self.assertEqual(self.dockerfile.read_text(), text)


if __name__ == '__main__':
    unittest.main()
