#!/usr/bin/env python
'''Playground for local fetch & diff workflow.

To run:
    source ./common.sh
    ./dpxdt/test.py
'''

import copy
import glob
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading

# Local Libraries
import gflags
FLAGS = gflags.FLAGS
import yaml

# Local modules
from dpxdt import runworker
from dpxdt import server
from dpxdt.client import capture_worker
from dpxdt.client import fetch_worker
from dpxdt.client import pdiff_worker
from dpxdt.client import process_worker
from dpxdt.client import timer_worker
from dpxdt.client import workers

FLAGS.phantomjs_binary = 'phantomjs'
FLAGS.phantomjs_script = 'dpxdt/client/capture.js'
FLAGS.phantomjs_timeout = 20


MODES = ['test', 'update']


class OneTestWorkflowItem(workers.WorkflowItem):
    '''Runs an individual capture & pdiff (or update) based on a config.'''

    def run(self, test_config, ref_dir, tmp_dir, mode, heartbeat=None):
        '''Build a CaptureAndDiffWorkflowItem for a test.
        
        Args:
            test_config: See test.yaml for structure of test_config.
        Returns: A CaptureAndDiffWorkflowItem
        '''
        assert 'name' in test_config
        assert 'url' in test_config

        name = test_config['name']

        test_dir = tempfile.mkdtemp(dir=tmp_dir)
        log_file = os.path.join(test_dir, 'log.txt')
        output_path = os.path.join(test_dir, 'screenshot.png')

        capture_config = copy.deepcopy(test_config['config'])
        capture_config['targetUrl'] = test_config['url']
        config_file = os.path.join(test_dir, 'config.json')
        json.dump(capture_config, open(config_file, 'w'), indent=2)

        ref_path = os.path.join(ref_dir, '%s.png' % name)
        if mode == 'test':
            assert os.path.exists(ref_path), ('Reference image %s does not exist. '
                'Try running in update mode.' % ref_path)
        elif mode == 'update':
            output_path = ref_path
            ref_path = None
        else:
            raise ValueError('Invalid mode %s' % mode)

        class NamedHeartbeat(workers.WorkflowItem):
            def run(self, message):
                yield heartbeat('%s: %s' % (name, message))

        yield CaptureAndDiffWorkflowItem(
                name, log_file, config_file, output_path, ref_path,
                heartbeat=NamedHeartbeat)


class CaptureAndDiffWorkflowItem(workers.WorkflowItem):

    def run(self, name, log_file, config_file, output_path, ref_path, heartbeat=None):
        yield heartbeat('Running webpage capture process')

        capture_failed = True
        failure_reason = None

        try:
            returncode = yield capture_worker.CaptureWorkflow(log_file, config_file, output_path)
        except (process_worker.TimeoutError, OSError), e:
            failure_reason = str(e)
        else:
            capture_failed = returncode != 0
            failure_reason = 'returncode=%s' % returncode

        if capture_failed:
            raise capture_worker.CaptureFailedError(
                FLAGS.capture_task_max_attempts,
                failure_reason)

        if ref_path is None:
            yield heartbeat('Updated %s' % output_path)
            return  # update mode
        
        # TODO: consolidate this code w/ DoPdiffQueueWorkflow.run
        ref_resized_path = os.path.join(os.path.dirname(output_path), 'ref_resized')
        diff_path = os.path.join(os.path.dirname(output_path), 'diff.png')
        max_attempts = FLAGS.pdiff_task_max_attempts

        yield heartbeat('Resizing reference image')
        returncode = yield pdiff_worker.ResizeWorkflow(
            log_file, ref_path, output_path, ref_resized_path)
        if returncode != 0:
            raise pdiff_worker.PdiffFailedError(
                max_attempts,
                'Could not resize reference image to size of new image')

        yield heartbeat('Running perceptual diff process')
        returncode = yield pdiff_worker.PdiffWorkflow(
            log_file, ref_resized_path, output_path, diff_path)

        # ImageMagick returns 1 if the images are different and 0 if
        # they are the same, so the return code is a bad judge of
        # successfully running the diff command. Instead we need to check
        # the output text.
        diff_failed = True

        # Check for a successful run or a known failure.
        distortion = None
        if os.path.isfile(log_file):
            log_data = open(log_file).read()
            if 'all: 0 (0)' in log_data:
                diff_path = None
                diff_failed = False
            elif 'image widths or heights differ' in log_data:
                # Give up immediately
                max_attempts = 1
            else:
                # Try to find the image magic normalized root square
                # mean and grab the first one.
                r = pdiff_worker.DIFF_REGEX.findall(log_data)
                if len(r) > 0:
                    diff_failed = False
                    distortion = r[0]

        if diff_failed:
            raise pdiff_worker.PdiffFailedError(
                max_attempts,
                'Comparison failed. returncode=%r' % returncode)

        else:
            if distortion:
                print '%s failed' % name
                print '  %s distortion' % distortion
                print '  See %s' % diff_path
            else:
                print '%s passed (no diff)' % name

        # TODO: delete temp files


class RunTestsWorkflowItem(workers.WorkflowItem):
    '''Load test YAML files and add them to the work queue.'''

    def run(self, config_dir, mode):
        configs = glob.glob(os.path.join(config_dir, '*.yaml'))
        if not configs:
            raise ValueError('No yaml files found in %s' % config_dir)

        for config_file in configs:
            config = yaml.load(open(config_file))
            assert 'tests' in config

            tmp_dir = tempfile.mkdtemp()

            setup = config.get('setup')
            if setup:
                setup_file = os.path.join(tmp_dir, 'setup.sh')
                log_file = os.path.join(tmp_dir, 'setup.log')
                logging.info('Executing setup step: %s', setup_file)
                open(setup_file, 'w').write(setup)
                with open(log_file, 'a') as output_file:
                    subprocess.Popen(['bash', setup_file],
                        stderr=subprocess.STDOUT,
                        stdout=output_file,
                        close_fds=True)

            for test in config['tests']:
                yield OneTestWorkflowItem(test, config_dir, tmp_dir, mode, heartbeat=workers.PrintWorkflow)


def usage(short=False):
    sys.stderr.write('Usage: %s [update|test] <testdir>\n' % sys.argv[0])
    if not short:
        sys.stderr.write('%s\n' % FLAGS)


def main(argv):
    try:
        argv = FLAGS(argv)
    except gflags.FlagsError, e:
        sys.stderr.write('%s\n' % e)
        usage()
        sys.exit(1)

    if len(argv) < 3:
        sys.stderr.write('Too few arguments\n')
        usage(short=True)
        sys.exit(1)

    mode = argv[1]
    assert mode in MODES, 'Invalid mode: %s (expected %r)' % (mode, MODES)

    config_dir = argv[2]
    assert os.path.isdir(config_dir), 'Expected directory, got %s' % config_dir

    if FLAGS.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    coordinator = workers.get_coordinator()
    timer_worker.register(coordinator)

    item = RunTestsWorkflowItem(config_dir, mode)
    item.root = True
    coordinator.input_queue.put(item, mode)

    coordinator.start()
    coordinator.wait_one()

    # TODO: return appropriate exit code


if __name__ == '__main__':
    main(sys.argv)
