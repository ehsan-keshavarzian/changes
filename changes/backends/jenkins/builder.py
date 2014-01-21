from __future__ import absolute_import, division

import json
import logging
import re
import requests
import time

from cStringIO import StringIO
from datetime import datetime
from hashlib import sha1
from flask import current_app

from changes.backends.base import BaseBackend, UnrecoverableException
from changes.config import db
from changes.constants import Result, Status
from changes.db.utils import create_or_update, get_or_create
from changes.events import publish_logchunk_update
from changes.jobs.sync_job_step import sync_job_step
from changes.models import (
    AggregateTestSuite, TestResult, TestResultManager, TestSuite,
    LogSource, LogChunk, Node, JobPhase, JobStep
)
from changes.handlers.xunit import XunitHandler

LOG_CHUNK_SIZE = 4096

RESULT_MAP = {
    'SUCCESS': Result.passed,
    'ABORTED': Result.aborted,
    'FAILURE': Result.failed,
    'REGRESSION': Result.failed,
    'UNSTABLE': Result.failed,
}

QUEUE_ID_XPATH = '/queue/item[action/parameter/name="CHANGES_BID" and action/parameter/value="{job_id}"]/id'
BUILD_ID_XPATH = '/freeStyleProject/build[action/parameter/name="CHANGES_BID" and action/parameter/value="{job_id}"]/number'

XUNIT_FILENAMES = ('junit.xml', 'xunit.xml', 'nosetests.xml')

ID_XML_RE = re.compile(r'<id>(\d+)</id>')
NUMBER_XML_RE = re.compile(r'<number>(\d+)</number>')


def chunked(iterator, chunk_size):
    """
    Given an iterator, chunk it up into ~chunk_size, but be aware of newline
    termination as an intended goal.
    """
    result = ''
    for chunk in iterator:
        result += chunk
        while len(result) >= chunk_size:
            newline_pos = result.rfind('\n', 0, chunk_size)
            if newline_pos == -1:
                newline_pos = chunk_size
            else:
                newline_pos += 1
            yield result[:newline_pos]
            result = result[newline_pos:]
    if result:
        yield result


class NotFound(Exception):
    pass


class JenkinsBuilder(BaseBackend):
    provider = 'jenkins'

    def __init__(self, base_url=None, job_name=None, token=None, *args, **kwargs):
        super(JenkinsBuilder, self).__init__(*args, **kwargs)
        self.base_url = base_url or self.app.config['JENKINS_URL']
        self.token = token or self.app.config['JENKINS_TOKEN']
        self.logger = logging.getLogger('jenkins')
        self.job_name = job_name
        # disabled by default as it's expensive
        self.sync_log_artifacts = self.app.config.get('JENKINS_SYNC_LOG_ARTIFACTS', False)
        self.sync_xunit_artifacts = self.app.config.get('JENKINS_SYNC_XUNIT_ARTIFACTS', True)

    def _get_raw_response(self, path, method='GET', params=None,
                          allow_redirects=False, **kwargs):
        url = '{}/{}'.format(self.base_url, path.lstrip('/'))

        if params is None:
            params = {}

        params.setdefault('token', self.token)

        self.logger.info('Fetching %r', url)
        resp = getattr(requests, method.lower())(url, params=params, **kwargs)

        if resp.status_code == 404:
            raise NotFound
        elif not (200 <= resp.status_code < 300):
            raise Exception('Invalid response. Status code was %s' % resp.status_code)

        return resp.text

    def _get_json_response(self, path, *args, **kwargs):
        path = '{}/api/json/'.format(path.strip('/'))

        data = self._get_raw_response(path, *args, **kwargs)
        if not data:
            return

        try:
            return json.loads(data)
        except ValueError:
            raise Exception('Invalid JSON data')

    _get_response = _get_json_response

    def _parse_parameters(self, json):
        params = {}
        for action in json['actions']:
            params.update(
                (p['name'], p.get('value'))
                for p in action.get('parameters', [])
            )
        return params

    def _sync_job_from_queue(self, job):
        # TODO(dcramer): when we hit a NotFound in the queue, maybe we should
        # attempt to scrape the list of jobs for a matching CHANGES_BID, as this
        # doesnt explicitly mean that the job doesnt exist
        try:
            item = self._get_response('/queue/item/{}'.format(
                job.data['item_id']))
        except NotFound:
            job.status = Status.finished
            job.result = Result.unknown
            db.session.add(job)
            return

        if item.get('executable'):
            build_no = item['executable']['number']
            job.data['queued'] = False
            job.data['build_no'] = build_no

        if item['blocked']:
            job.status = Status.queued
            db.session.add(job)
        elif item.get('cancelled') and not job.data.get('build_no'):
            job.status = Status.finished
            job.result = Result.aborted
            db.session.add(job)
        elif item.get('executable'):
            self._sync_job_from_active(job)

    def _create_job_step(self, phase, job_name, build_no, **kwargs):
        defaults = {
            'data': {
                'job_name': job_name,
                'build_no': build_no,
            },
        }
        defaults.update(kwargs)

        step, created = get_or_create(JobStep, where={
            'job': phase.job,
            'project': phase.project,
            'phase': phase,
            'label': '{0} #{1}'.format(job_name, build_no),
        }, defaults=defaults)
        if not created:
            db.session.add(step)

        return step

    def _sync_job_from_active(self, job):
        if job.status not in (Status.in_progress, Status.finished):
            job.status = Status.in_progress
            db.session.add(job)

        phase, created = get_or_create(JobPhase, where={
            'job': job,
            'label': job.data['job_name'],
            'project': job.project,
        }, defaults={
            'status': Status.in_progress,
        })

        step = self._create_job_step(
            phase=phase,
            job_name=job.data['job_name'],
            build_no=job.data['build_no'],
            status=Status.in_progress,
        )
        db.session.commit()

        sync_job_step.delay_if_needed(
            step_id=step.id.hex,
            task_id=step.id.hex,
            parent_task_id=job.id.hex,
        )

    def _sync_artifact_as_xunit(self, jobstep, job_name, build_no, artifact):
        url = '{base}/job/{job}/{build}/artifact/{artifact}'.format(
            base=self.base_url, job=job_name,
            build=build_no, artifact=artifact['relativePath'],
        )

        resp = requests.get(url, stream=True)

        # TODO(dcramer): requests doesnt seem to provide a non-binary file-like
        # API, so we're stuffing it into StringIO
        handler = XunitHandler(jobstep.job)
        handler.process(StringIO(resp.content))

    def _sync_artifact_as_log(self, jobstep, job_name, build_no, artifact):
        job = jobstep.job
        logsource, created = get_or_create(LogSource, where={
            'name': artifact['displayPath'],
            'job': job,
            'step': jobstep,
        }, defaults={
            'project': job.project,
            'date_created': job.date_started,
        })

        url = '{base}/job/{job}/{build}/artifact/{artifact}'.format(
            base=self.base_url, job=job_name,
            build=build_no, artifact=artifact['relativePath'],
        )

        offset = 0
        resp = requests.get(url, stream=True)
        iterator = resp.iter_content()
        for chunk in chunked(iterator, LOG_CHUNK_SIZE):
            chunk_size = len(chunk)
            chunk, _ = create_or_update(LogChunk, where={
                'source': logsource,
                'offset': offset,
            }, values={
                'job': job,
                'project': job.project,
                'size': chunk_size,
                'text': chunk,
            })
            offset += chunk_size

            publish_logchunk_update(chunk)

    def _sync_console_log(self, jobstep):
        job = jobstep.job
        return self._sync_log(
            jobstep=jobstep,
            name='console',
            job_name=job.data['job_name'],
            build_no=job.data['build_no'],
        )

    def _sync_log(self, jobstep, name, job_name, build_no):
        job = jobstep.job
        # TODO(dcramer): this doesnt handle concurrency
        logsource, created = get_or_create(LogSource, where={
            'name': name,
            'job': job,
        }, defaults={
            'step': jobstep,
            'project': jobstep.project,
            'date_created': jobstep.date_started,
        })
        if created:
            offset = 0
        else:
            offset = jobstep.data.get('log_offset', 0)

        url = '{base}/job/{job}/{build}/logText/progressiveHtml/'.format(
            base=self.base_url,
            job=job_name,
            build=build_no,
        )

        resp = requests.get(url, params={'start': offset}, stream=True)
        log_length = int(resp.headers['X-Text-Size'])
        # When you request an offset that doesnt exist in the build log, Jenkins
        # will instead return the entire log. Jenkins also seems to provide us
        # with X-Text-Size which indicates the total size of the log
        if offset > log_length:
            return

        iterator = resp.iter_content()
        # XXX: requests doesnt seem to guarantee chunk_size, so we force it
        # with our own helper
        for chunk in chunked(iterator, LOG_CHUNK_SIZE):
            chunk_size = len(chunk)
            chunk, _ = create_or_update(LogChunk, where={
                'source': logsource,
                'offset': offset,
            }, values={
                'job': job,
                'project': job.project,
                'size': chunk_size,
                'text': chunk,
            })
            offset += chunk_size

            publish_logchunk_update(chunk)

        # We **must** track the log offset externally as Jenkins embeds encoded
        # links and we cant accurately predict the next `start` param.
        jobstep.data['log_offset'] = log_length
        db.session.add(jobstep)

        # Jenkins will suggest to us that there is more data when the job has
        # yet to complete
        return True if resp.headers.get('X-More-Data') == 'true' else None

    def _process_test_report(self, job, test_report):
        test_list = []

        if not test_report:
            return test_list

        for suite_data in test_report['suites']:
            suite_name = suite_data.get('name', 'default')

            # TODO(dcramer): this is not specific to Jenkins and should be
            # abstracted
            suite, _ = get_or_create(TestSuite, where={
                'job': job,
                'name_sha': sha1(suite_name).hexdigest(),
            }, defaults={
                'name': suite_name,
                'project': job.project,
            })

            agg, created = get_or_create(AggregateTestSuite, where={
                'project': job.project,
                'name_sha': suite.name_sha,
            }, defaults={
                'name': suite.name,
                'first_job_id': job.id,
            })

            # if not created:
            #     db.session.query(AggregateTestSuite).filter(
            #         AggregateTestSuite.id == agg.id,
            #         AggregateTestSuite.last_job_id == agg.last_job_id,
            #     ).update({
            #         AggregateTestSuite.last_job_id: build.id,
            #     }, synchronize_session=False)

            for case in suite_data['cases']:
                message = []
                if case['errorDetails']:
                    message.append('Error\n-----')
                    message.append(case['errorDetails'] + '\n')
                if case['errorStackTrace']:
                    message.append('Stacktrace\n----------')
                    message.append(case['errorStackTrace'] + '\n')
                if case['skippedMessage']:
                    message.append(case['skippedMessage'] + '\n')

                if case['status'] in ('PASSED', 'FIXED'):
                    result = Result.passed
                elif case['status'] in ('FAILED', 'REGRESSION'):
                    result = Result.failed
                elif case['status'] == 'SKIPPED':
                    result = Result.skipped
                else:
                    raise ValueError('Invalid test result: %s' % (case['status'],))

                test_result = TestResult(
                    job=job,
                    suite=suite,
                    name=case['name'],
                    package=case['className'] or None,
                    duration=int(case['duration'] * 1000),
                    message='\n'.join(message).strip(),
                    result=result,
                )
                test_list.append(test_result)
        return test_list

    def _sync_test_results(self, step, job_name, build_no):
        try:
            test_report = self._get_response('/job/{}/{}/testReport/'.format(
                job_name, build_no))
        except NotFound:
            return

        test_list = self._process_test_report(step.job, test_report)

        manager = TestResultManager(step.job)
        with db.session.begin_nested():
            manager.save(test_list)

    def _find_job(self, job_name, job_id):
        """
        Given a job identifier, we attempt to poll the various endpoints
        for a limited amount of time, trying to match up either a queued item
        or a running job that has the CHANGES_BID parameter.

        This is nescesary because Jenkins does not give us any identifying
        information when we create a job initially.

        The job_id parameter should be the corresponding value to look for in
        the CHANGES_BID parameter.

        The result is a mapping with the following keys:

        - queued: is it currently present in the queue
        - item_id: the queued item ID, if available
        - build_no: the build number, if available
        """
        # Check the queue first to ensure that we don't miss a transition
        # from queue -> active jobs
        item = self._find_job_in_queue(job_name, job_id)
        if item:
            return item
        return self._find_job_in_active(job_name, job_id)

    def _find_job_in_queue(self, job_name, job_id):
        xpath = QUEUE_ID_XPATH.format(
            job_id=job_id,
        )
        try:
            response = self._get_raw_response('/queue/api/xml/', params={
                'xpath': xpath,
            })
        except NotFound:
            return

        # TODO: it's possible this isnt queued when this gets run
        return {
            'job_name': job_name,
            'queued': True,
            'item_id': ID_XML_RE.search(response).group(1),
            'build_no': None,
        }

    def _find_job_in_active(self, job_name, job_id):
        xpath = QUEUE_ID_XPATH.format(
            job_id=job_id,
        )
        try:
            response = self._get_raw_response('/job/{job_name}/api/xml/'.format(
                job_name=job_name,
            ), params={
                'depth': 1,
                'xpath': xpath,
            })
        except NotFound:
            return

        return {
            'job_name': job_name,
            'queued': False,
            'item_id': None,
            'build_no': NUMBER_XML_RE.search(response).group(1),
        }

    def sync_job(self, job):
        if job.data['queued']:
            self._sync_job_from_queue(job)
        else:
            self._sync_job_from_active(job)

    def sync_step(self, step):
        job_name = step.data['job_name']
        build_no = step.data['build_no']

        try:
            item = self._get_response('/job/{}/{}'.format(
                job_name, build_no))
        except NotFound:
            raise UnrecoverableException

        # TODO(dcramer): we're doing a lot of work here when we might
        # not need to due to it being sync'd previously
        node, _ = get_or_create(Node, where={
            'label': item['builtOn'],
        })

        step.node = node
        step.label = item['fullDisplayName']
        step.date_started = datetime.utcfromtimestamp(
            item['timestamp'] / 1000)

        if item['building']:
            step.status = Status.in_progress
        else:
            step.status = Status.finished
            step.result = RESULT_MAP[item['result']]
            # values['duration'] = item['duration'] or None
            step.date_finished = datetime.utcfromtimestamp(
                (item['timestamp'] + item['duration']) / 1000)

        # step.data.update({
        #     'backend': {
        #         'uri': item['url'],
        #         'label': item['fullDisplayName'],
        #     }
        # })
        db.session.add(step)

        # TODO(dcramer): we shoudl abstract this into a sync_phase
        phase = step.phase

        phase.status = step.status
        phase.result = step.result
        phase.date_started = step.date_started
        phase.date_finished = step.date_finished

        db.session.add(phase)

        db.session.commit()

        # sync artifacts
        for artifact in item.get('artifacts', ()):
            self.sync_artifact(
                jobstep=step,
                job_name=job_name,
                build_no=build_no,
                artifact=artifact,
            )
            db.session.commit()

        # sync test results
        try:
            with db.session.begin_nested():
                self._sync_test_results(
                    step=step,
                    job_name=job_name,
                    build_no=build_no,
                )
        except Exception:
            self.logger.exception(
                'Failed to sync test results for %s #%s', job_name, build_no)
        else:
            db.session.commit()

        # sync console log
        try:
            start = time.time()
            result = True
            while result:
                if time.time() - start > 30:
                    raise Exception('Took too long to sync log')

                result = self._sync_log(
                    jobstep=step,
                    name=step.label,
                    job_name=job_name,
                    build_no=build_no,
                )

        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                'Unable to sync console log for job step %r',
                step.id.hex)

    def sync_artifact(self, jobstep, job_name, build_no, artifact):
        if self.sync_log_artifacts and artifact['fileName'].endswith('.log'):
            self._sync_artifact_as_log(jobstep, job_name, build_no, artifact)
        if self.sync_xunit_artifacts and artifact['fileName'].endswith(XUNIT_FILENAMES):
            self._sync_artifact_as_xunit(jobstep, job_name, build_no, artifact)

    def create_job(self, job):
        """
        Creates a job within Jenkins.

        Due to the way the API works, this consists of two steps:

        - Submitting the job
        - Polling for the newly created job to associate either a queue ID
          or a finalized build number.
        """
        job_name = self.job_name
        if not job_name:
            raise UnrecoverableException('Missing Jenkins project configuration')

        json_data = {
            'parameter': [
                {'name': 'CHANGES_BID', 'value': job.id.hex},
            ]
        }
        if job.build.source.revision_sha:
            json_data['parameter'].append(
                {'name': 'REVISION', 'value': job.build.source.revision_sha},
            )

        if job.build.source.patch:
            json_data['parameter'].append(
                {'name': 'PATCH', 'file': 'patch'}
            )
            files = {
                'patch': job.build.source.patch.diff,
            }
        else:
            files = None

        # TODO: Jenkins will return a 302 if it cannot queue the job which I
        # believe implies that there is already a job with the same parameters
        # queued.
        self._get_response('/job/{}/build'.format(job_name), method='POST', data={
            'json': json.dumps(json_data),
        }, files=files)

        # we retry for a period of time as Jenkins doesn't have strong consistency
        # guarantees and the job may not show up right away
        t = time.time() + 5
        job_data = None
        while time.time() < t:
            job_data = self._find_job(job_name, job.id.hex)
            if job_data:
                break
            time.sleep(0.3)

        if job_data is None:
            raise Exception('Unable to find matching job after creation. GLHF')

        job.data = job_data
        if job.data['queued']:
            job.status = Status.queued
        else:
            job.status = Status.in_progress
        db.session.add(job)
        db.session.commit()
