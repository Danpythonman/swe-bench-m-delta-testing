import logging
from pathlib import Path
from typing import Final, override

import docker

from sbmdt.base import Evaluator
from sbmdt.env import DOCKERFILES_BASE
from sbmdt.utils import apply_change, read_from_container

__all__ = ['AlibabaEvaluator']

log = logging.getLogger(__name__)

KARMA_FILE: Final[str] = '/testbed/scripts/test/karma.js'


class AlibabaEvaluator(Evaluator):
    instance_id: str
    dockerfile_path: Path

    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        self.dockerfile_path = DOCKERFILES_BASE / instance_id / 'Dockerfile'
        self.image = None
        self.container = None

    @override
    def setup(self):
        client = docker.from_env()
        self.image, _ = client.images.build(
            path=str(self.dockerfile_path.parent.resolve()),
            tag=f'sbmdt-{self.instance_id}:latest',
        )
        self.container = client.containers.run(
            self.image,
            command='/bin/bash',
            name=f'sbmdt-{self.instance_id}',
            stdin_open=True,
            tty=True,
            detach=True,
        )

        # 1. Install package
        exit_code, output = self.container.exec_run(
            'npm install karma-junit-reporter --save-dev',
            workdir='/testbed',
            stream=False,
        )
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())

        # 2. Add junit to reporters
        apply_change(
            container=self.container,
            file=KARMA_FILE,
            find="reporters: ['spec', 'coverage']",
            replace="reporters: ['spec', 'coverage', 'junit']",
            assertion="reporters: ['spec', 'coverage', 'junit']",
        )

        # 3. Add junitReporter config
        apply_change(
            container=self.container,
            file=KARMA_FILE,
            find="hostname: 'localhost'",
            replace="""junitReporter: {
                    outputDir: 'test-results',
                    outputFile: 'results.xml',
                    useBrowserName: false,
                },
                hostname: 'localhost'""",
            assertion='junitReporter:',
        )

        # 4. Add plugin
        apply_change(
            container=self.container,
            file=KARMA_FILE,
            find="'karma-coverage',",
            replace="'karma-coverage',\n            'karma-junit-reporter',",
            assertion="'karma-junit-reporter',",
        )

        log.info('All changes applied successfully.')

    @override
    def evaluate(self):
        if self.container is None:
            raise Exception('no container')

        exit_code, output = self.container.exec_run(
            'npm test',
            environment={'TRAVIS': 'true'},
            workdir='/testbed',
            stream=False,
        )
        log.info('done running')
        assert isinstance(output, bytes)

        log.info(exit_code)
        log.info(output.decode())
        results = read_from_container(
            self.container, '/testbed/scripts/test/test-results/results.xml'
        )
        log.info(results)

    @override
    def pre_cleanup(self):
        pass

    @override
    def post_cleanup(self):
        pass
