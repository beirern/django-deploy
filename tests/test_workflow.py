"""Exercise the actual inline workflow scripts without cloud credentials."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load(
    (ROOT / '.github/workflows/deploy.yml').read_text(), Loader=yaml.BaseLoader
)
STEPS = {
    step.get('name'): step
    for job in WORKFLOW['jobs'].values()
    for step in job['steps']
}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app = self.base / 'application'
        self.app.mkdir()
        (self.app / 'docker-compose.yml').write_text(
            'services:\n  web:\n    image: ${IMAGE:?required}\n'
        )
        self.runner = self.base / 'runner'
        self.runner.mkdir()
        self.remote_temp = self.base / 'remote-temp'
        self.remote_temp.mkdir()
        self.destination = self.base / 'deployed'
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.log = self.base / 'docker.jsonl'
        self.env = {
            **os.environ,
            'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
            'RUNNER_TEMP': str(self.runner),
            'TMPDIR': str(self.remote_temp),
            'DEPLOY_DIRECTORY': str(self.destination),
            'COMPOSE_FILES': 'docker-compose.yml',
            'DEPLOYMENT_FILES': 'docker-compose.yml',
            'WAIT_TIMEOUT': '90',
            'APP_ENV': 'SECRET_KEY=example-only\nDEBUG=False\n',
            'ECR_REGISTRY': '123456789012.dkr.ecr.us-west-1.amazonaws.com',
            'ECR_REPOSITORY': 'example-app',
            'VERSION': '1.2.3',
            'SERVER_HOST': 'example.invalid',
            'SERVER_USER': 'ubuntu',
            'SSH_PORT': '22',
            'DOCKER_LOG': str(self.log),
            'FAIL_DOCKER': '',
        }
        self.command('aws', "import sys\nsys.stdout.write('fake-registry-password\\n')\n")
        self.command('docker', '''
import json
import os
from pathlib import Path
import sys
args = sys.argv[1:]
with open(os.environ['DOCKER_LOG'], 'a') as stream:
    stream.write(json.dumps({'args': args, 'image': os.environ.get('IMAGE')}) + '\\n')
if args[0] == 'login':
    assert sys.stdin.read().strip() == 'fake-registry-password'
    directory = Path(os.environ['DOCKER_CONFIG'])
    directory.mkdir()
    (directory / 'config.json').write_text('temporary credentials')
if os.environ['FAIL_DOCKER'] and os.environ['FAIL_DOCKER'] in args:
    sys.exit(37)
''')
        self.command('ssh', '''
import subprocess
import sys
assert 'StrictHostKeyChecking=yes' in sys.argv
assert 'BatchMode=yes' in sys.argv
assert sys.argv[-2] == 'ubuntu@example.invalid'
sys.exit(subprocess.run(['bash', '-c', sys.argv[-1]]).returncode)
''')

    def command(self, name, code):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\n{code}')
        path.chmod(0o755)

    def run_step(self, name):
        return subprocess.run(
            ['bash', '-e', '-o', 'pipefail', '-c', STEPS[name]['run']],
            cwd=self.app, env=self.env, capture_output=True, text=True,
        )

    def prepare(self):
        result = self.run_step('Prepare deployment bundle')
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.runner / 'django-deploy/bundle'

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_full_transport_preserves_env_and_waits_without_building(self):
        marker = self.base / 'must-not-exist'
        self.env['APP_ENV'] = f'SECRET="$(touch {marker})"\nOTHER=`touch {marker}`\n'
        self.env['COMPOSE_FILES'] += '\ndocker-compose.prod.yml\n'
        self.env['DEPLOYMENT_FILES'] += '\ndocker-compose.prod.yml\nbin\n'
        (self.app / 'docker-compose.prod.yml').write_text('services: {}\n')
        (self.app / 'bin').mkdir()
        executable = self.app / 'bin/prod.sh'
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
        self.prepare()
        result = self.run_step('Deploy and wait for readiness')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        dotenv = self.destination / '.env'
        self.assertEqual(dotenv.read_text(), self.env['APP_ENV'])
        self.assertEqual(dotenv.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.destination / 'bin/prod.sh').stat().st_mode & 0o111)
        self.assertEqual(list(self.remote_temp.iterdir()), [])
        calls = self.calls()
        expected_image = self.env['ECR_REGISTRY'] + '/example-app:1.2.3'
        self.assertTrue(all(call['image'] == expected_image for call in calls))
        self.assertEqual(calls[-1]['args'], [
            'compose', '-f', 'docker-compose.yml', '-f', 'docker-compose.prod.yml',
            'up', '--detach', '--no-build', '--wait', '--wait-timeout', '90',
        ])
        self.assertNotIn('example-only', result.stdout + result.stderr)

    def test_pull_failure_prevents_start_and_cleans_remote_credentials(self):
        self.prepare()
        self.env['FAIL_DOCKER'] = 'pull'
        result = self.run_step('Deploy and wait for readiness')
        self.assertEqual(result.returncode, 37)
        self.assertFalse(any('up' in call['args'] for call in self.calls()))
        self.assertEqual(list(self.remote_temp.iterdir()), [])

    def test_readiness_failure_is_not_reported_as_success(self):
        self.prepare()
        self.env['FAIL_DOCKER'] = 'up'
        result = self.run_step('Deploy and wait for readiness')
        self.assertEqual(result.returncode, 37)
        self.assertNotIn('Deployment is ready', result.stdout)
        self.assertEqual(list(self.remote_temp.iterdir()), [])

    def test_runner_cleanup_removes_secret_bundle(self):
        self.prepare()
        result = self.run_step('Remove temporary credentials and deployment bundle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.runner.iterdir()), [])

    def test_invalid_paths_are_rejected(self):
        for field, value in [
            ('DEPLOY_DIRECTORY', '/'),
            ('DEPLOY_DIRECTORY', '/home'),
            ('DEPLOY_DIRECTORY', '~/app'),
            ('DEPLOY_DIRECTORY', '/home/ubuntu/../app'),
            ('DEPLOY_DIRECTORY', '/home/ubuntu/app;touch_bad'),
            ('DEPLOYMENT_FILES', '../outside'),
            ('DEPLOYMENT_FILES', '.env'),
            ('DEPLOYMENT_FILES', '/etc/passwd'),
        ]:
            with self.subTest(field=field, value=value):
                old = self.env[field]
                self.env[field] = value
                result = self.run_step('Prepare deployment bundle')
                self.assertNotEqual(result.returncode, 0)
                self.env[field] = old
                # Each attempt needs its own fresh bundle directory.
                self.run_step('Remove temporary credentials and deployment bundle')

    def test_missing_compose_and_empty_env_are_rejected(self):
        for field, value in [
            ('COMPOSE_FILES', 'missing.yml'), ('APP_ENV', ''),
            ('DEPLOYMENT_FILES', ''), ('WAIT_TIMEOUT', '0'), ('WAIT_TIMEOUT', '601'),
        ]:
            with self.subTest(field=field):
                old = self.env[field]
                self.env[field] = value
                self.assertNotEqual(self.run_step('Prepare deployment bundle').returncode, 0)
                self.env[field] = old
                self.run_step('Remove temporary credentials and deployment bundle')

    def test_symlink_and_nested_hidden_files_are_rejected(self):
        assets = self.app / 'assets'
        assets.mkdir()
        link = assets / 'outside'
        link.symlink_to('/etc/passwd')
        self.env['DEPLOYMENT_FILES'] += '\nassets'
        self.assertNotEqual(self.run_step('Prepare deployment bundle').returncode, 0)
        self.run_step('Remove temporary credentials and deployment bundle')
        link.unlink()
        (assets / '.env').write_text('never bundle this')
        self.assertNotEqual(self.run_step('Prepare deployment bundle').returncode, 0)

    def test_shell_steps_parse(self):
        for name, step in STEPS.items():
            if 'run' in step:
                with self.subTest(step=name):
                    result = subprocess.run(
                        ['bash', '-n'], input=step['run'], text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_template_contract_and_release_configuration(self):
        caller = yaml.load((ROOT / 'templates/caller.yml').read_text(), Loader=yaml.BaseLoader)
        contract = WORKFLOW['on']['workflow_call']
        job = caller['jobs']['deploy']
        self.assertLessEqual(set(job['with']), set(contract['inputs']))
        self.assertEqual(set(job['secrets']), set(contract['secrets']))
        config = json.loads((ROOT / 'templates/.releaserc.json').read_text())
        self.assertEqual(config['branches'], ['main'])
        self.assertEqual(config['plugins'][0][1]['releaseRules'][-1], {'release': 'patch'})


if __name__ == '__main__':
    unittest.main()
