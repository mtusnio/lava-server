import cStringIO
import datetime
import json
import xmlrpclib
import logging
import sys
from dashboard_app.models import BundleStream

from django.contrib.auth.models import Group, Permission, User
from django.test import TransactionTestCase
from django.test.client import Client

from django_testscenarios.ubertest import TestCase

from linaro_django_xmlrpc.models import AuthToken

from lava_scheduler_app.models import (
    Device,
    DeviceType,
    JSONDataError,
    Tag,
    TestJob,
    DevicesUnavailableException,
)
from lava_scheduler_daemon.dbjobsource import DatabaseJobSource
import simplejson

logger = logging.getLogger()
logger.level = logging.INFO  # change to DEBUG to see *all* output
stream_handler = logging.StreamHandler(sys.stdout)
logger.addHandler(stream_handler)


# Based on http://www.technobabble.dk/2008/apr/02/xml-rpc-dispatching-through-django-test-client/
class TestTransport(xmlrpclib.Transport, object):
    """Handles connections to XML-RPC server through Django test client."""

    def __init__(self, user=None, password=None):
        super(TestTransport, self).__init__()
        self.client = Client()
        if user:
            success = self.client.login(username=user, password=password)
            if not success:
                raise AssertionError("Login attempt failed!")
        self._use_datetime = True

    def request(self, host, handler, request_body, verbose=0):
        self.verbose = verbose
        response = self.client.post(
            handler, request_body, content_type="text/xml")
        res = cStringIO.StringIO(response.content)
        res.seek(0)
        return self.parse_response(res)


class ModelFactory(object):

    def __init__(self):
        self._int = 0

    def getUniqueInteger(self):
        self._int += 1
        return self._int

    def getUniqueString(self, prefix='generic'):
        return '%s-%d' % (prefix, self.getUniqueInteger())

    def make_user(self):
        return User.objects.create_user(
            self.getUniqueString(),
            '%s@mail.invalid' % (self.getUniqueString(),),
            self.getUniqueString())

    def ensure_device_type(self, name=None):
        if name is None:
            name = self.getUniqueString('name')
        logging.debug("asking for a device_type with name %s" % name)
        device_type = DeviceType.objects.get_or_create(name=name)[0]
        self.make_device(device_type)
        return device_type

    def make_device_type(self, name=None, health_check_job=None):
        if name is None:
            name = self.getUniqueString('name')
        device_type = DeviceType.objects.create(
            name=name, health_check_job=health_check_job)
        device_type.save()
        logging.debug("asking for a device of type %s" % device_type.name)
        return device_type

    def ensure_tag(self, name):
        return Tag.objects.get_or_create(name=name)[0]

    def make_device(self, device_type=None, hostname=None, tags=None, **kw):
        if device_type is None:
            device_type = self.ensure_device_type()
        if hostname is None:
            hostname = self.getUniqueString()
        if type(tags) != list:
            tags = []
        device = Device(device_type=device_type, is_public=True, hostname=hostname, **kw)
        device.tags = tags
        logging.debug("making a device of type %s %s %s with tags '%s'"
                      % (device_type, device.is_public, device.hostname, ", ".join([x.name for x in device.tags.all()])))
        device.save()
        return device

    def make_job_data(self, actions=[], **kw):
        data = {'actions': actions, 'timeout': 1, 'health_check': False}
        data.update(kw)
        if 'target' not in data and 'device_type' not in data:
            if DeviceType.objects.all():
                data['device_type'] = DeviceType.objects.all()[0].name
            else:
                device_type = self.ensure_device_type()
                self.make_device(device_type)
                data['device_type'] = device_type.name
        return data

    def make_job_json(self, **kw):
        return simplejson.dumps(self.make_job_data(**kw), sort_keys=True, indent=4 * ' ')

    def make_testjob(self, definition=None, submitter=None, **kwargs):
        if definition is None:
            definition = self.make_job_json()
        if submitter is None:
            submitter = self.make_user()
        if 'user' not in kwargs:
            kwargs['user'] = submitter
        testjob = TestJob(
            definition=definition, submitter=submitter, **kwargs)
        testjob.save()
        return testjob


class TestCaseWithFactory(TestCase):

    def setUp(self):
        TestCase.setUp(self)
        self.factory = ModelFactory()


class TestTestJob(TestCaseWithFactory):

    def test_from_json_and_user_sets_definition(self):
        definition = self.factory.make_job_json()
        job = TestJob.from_json_and_user(definition, self.factory.make_user())
        self.assertEqual(definition, job.definition)

    def test_from_json_and_user_sets_submitter(self):
        user = self.factory.make_user()
        job = TestJob.from_json_and_user(
            self.factory.make_job_json(), user)
        self.assertEqual(user, job.submitter)

    def test_from_json_and_user_sets_device_type(self):
        panda_type = self.factory.ensure_device_type(name='panda')
        job = TestJob.from_json_and_user(
            self.factory.make_job_json(device_type='panda'),
            self.factory.make_user())
        self.assertEqual(panda_type, job.requested_device_type)

    def test_from_json_and_user_sets_target(self):
        panda_board = self.factory.make_device(hostname='panda01')
        job = TestJob.from_json_and_user(
            self.factory.make_job_json(target='panda01'),
            self.factory.make_user())
        self.assertEqual(panda_board, job.requested_device)

    def test_from_json_and_user_does_not_set_device_type_from_target(self):
        panda_type = self.factory.ensure_device_type(name='panda')
        self.factory.make_device(device_type=panda_type, hostname='panda01')
        job = TestJob.from_json_and_user(
            self.factory.make_job_json(target='panda01'),
            self.factory.make_user())
        self.assertEqual(None, job.requested_device_type)

    def test_from_json_and_user_sets_date_submitted(self):
        before = datetime.datetime.now()
        job = TestJob.from_json_and_user(
            self.factory.make_job_json(),
            self.factory.make_user())
        after = datetime.datetime.now()
        self.assertTrue(before < job.submit_time < after)

    def test_from_json_and_user_sets_status_to_SUBMITTED(self):
        job = TestJob.from_json_and_user(
            self.factory.make_job_json(),
            self.factory.make_user())
        self.assertEqual(job.status, TestJob.SUBMITTED)

    def test_from_json_and_user_sets_no_tags_if_no_tags(self):
        job = TestJob.from_json_and_user(
            self.factory.make_job_json(device_tags=[]),
            self.factory.make_user())
        self.assertEqual(set(job.tags.all()), set([]))

    def test_from_json_and_user_errors_on_unknown_tags(self):
        """
        Tests that tags which are not already defined in the database
        cause job submissions to be rejected.
        """
        self.assertRaises(
            JSONDataError, TestJob.from_json_and_user,
            self.factory.make_job_json(device_tags=['unknown']),
            self.factory.make_user())

    def test_from_json_and_user_errors_on_unsupported_tags(self):
        """
        Tests that tags which do exist but are not defined for the
        any of the devices of the requested type cause the submission
        to be rejected with Devices Unavailable.
        """
        device_type = self.factory.ensure_device_type(name='panda')
        self.factory.make_device(device_type=device_type, hostname="panda2")
        self.factory.ensure_tag('tag1')
        self.factory.ensure_tag('tag2')
        try:
            TestJob.from_json_and_user(
                self.factory.make_job_json(device_tags=['tag1', 'tag2']),
                self.factory.make_user())
        except DevicesUnavailableException:
            pass
        else:
            self.fail("Device tags failure: job submitted without any devices supporting the requested tags")

    def test_from_json_and_user_sets_tag_from_device_tags(self):
        device_type = self.factory.ensure_device_type(name='panda')
        self.factory.ensure_tag('tag')
        tags = list(Tag.objects.filter(name='tag'))
        self.factory.make_device(device_type=device_type, hostname="panda1", tags=tags)
        job = TestJob.from_json_and_user(
            self.factory.make_job_json(device_tags=['tag']),
            self.factory.make_user())
        self.assertEqual(
            set(tag.name for tag in job.tags.all()), {'tag'})

    def test_from_json_and_user_sets_multiple_tag_from_device_tags(self):
        device_type = self.factory.ensure_device_type(name='panda')
        tag_list = [
            self.factory.ensure_tag('tag1'),
            self.factory.ensure_tag('tag2')
        ]
        self.factory.make_device(device_type=device_type, hostname="panda2", tags=tag_list)
        job = TestJob.from_json_and_user(
            self.factory.make_job_json(device_tags=['tag1', 'tag2']),
            self.factory.make_user())
        self.assertEqual(
            set(tag.name for tag in job.tags.all()), {'tag1', 'tag2'})

    def test_from_json_and_user_reuses_tag_objects(self):
        device_type = self.factory.ensure_device_type(name='panda')
        self.factory.ensure_tag('tag')
        tags = list(Tag.objects.filter(name='tag'))
        self.factory.make_device(device_type=device_type, hostname="panda3", tags=tags)
        job1 = TestJob.from_json_and_user(
            self.factory.make_job_json(device_tags=['tag']),
            self.factory.make_user())
        job2 = TestJob.from_json_and_user(
            self.factory.make_job_json(device_tags=['tag']),
            self.factory.make_user())
        self.assertEqual(
            set(tag.pk for tag in job1.tags.all()),
            set(tag.pk for tag in job2.tags.all()))

    def test_from_json_and_user_matches_available_tags(self):
        """
        Test that with more than one device of the requested type supporting
        tags, that the tag list set for the TestJob matches the list requested,
        not a shorter list from a different device or a combined list of multiple
        devices.
        """
        device_type = self.factory.ensure_device_type(name='panda')
        tag_list = [
            self.factory.ensure_tag('common_tag1'),
            self.factory.ensure_tag('common_tag2')
        ]
        self.factory.make_device(device_type=device_type, hostname="panda4", tags=tag_list)
        tag_list.append(self.factory.ensure_tag('unique_tag'))
        self.factory.make_device(device_type=device_type, hostname="panda5", tags=tag_list)
        job = TestJob.from_json_and_user(
            self.factory.make_job_json(device_tags=['common_tag1', 'common_tag2', 'unique_tag']),
            self.factory.make_user())
        self.assertEqual(
            set(tag for tag in job.tags.all()),
            set(tag_list)
        )

    def test_from_json_and_user_rejects_invalid_json(self):
        self.assertRaises(
            ValueError, TestJob.from_json_and_user, '{',
            self.factory.make_user())

    def test_from_json_and_user_rejects_invalid_job(self):
        # job data must have the 'actions' and 'timeout' properties, so this
        # will be rejected.
        self.assertRaises(
            ValueError, TestJob.from_json_and_user, '{}',
            self.factory.make_user())

    def make_job_json_for_stream_name(self, stream_name):
        return self.factory.make_job_json(
            actions=[
                {
                    'command': 'submit_results',
                    'parameters': {
                        'server': 'http://localhost/RPC2',
                        'stream': stream_name,
                    }
                }
            ])

    def test_from_json_and_user_sets_group_from_bundlestream(self):
        group = Group.objects.create(name='group')
        user = self.factory.make_user()
        user.groups.add(group)
        b = BundleStream.objects.create(
            group=group, slug='blah', is_public=True)
        b.save()
        j = self.make_job_json_for_stream_name(b.pathname)
        job = TestJob.from_json_and_user(j, user)
        self.assertEqual(group, job.group)

    def test_from_json_and_user_can_submit_to_anonymous(self):
        user = self.factory.make_user()
        anon_user = User.objects.get_or_create(username="anonymous-owner")[0]
        b = BundleStream.objects.create(
            slug='anonymous', is_anonymous=True, user=anon_user,
            is_public=True)
        b.save()
        j = self.make_job_json_for_stream_name('/anonymous/anonymous/')
        job = TestJob.from_json_and_user(j, user)
        self.assertEqual(user, job.submitter)

    def test_from_json_and_user_sets_is_public_from_bundlestream(self):
        group = Group.objects.create(name='group')
        user = self.factory.make_user()
        user.groups.add(group)
        b = BundleStream.objects.create(
            group=group, slug='blah', is_public=False)
        b.save()
        j = self.make_job_json_for_stream_name(b.pathname)
        job = TestJob.from_json_and_user(j, user)
        self.assertEqual(False, job.is_public)

    def test_from_json_and_user_rejects_missing_bundlestream(self):
        user = self.factory.make_user()
        j = self.make_job_json_for_stream_name('no such stream')
        self.assertRaises(ValueError, TestJob.from_json_and_user, j, user)

    def test_from_json_and_user_rejects_inaccessible_bundlestream(self):
        stream_user = self.factory.make_user()
        job_user = self.factory.make_user()
        b = BundleStream.objects.create(
            user=stream_user, slug='blah', is_public=True)
        b.save()
        j = self.make_job_json_for_stream_name(b.pathname)
        self.assertRaises(ValueError, TestJob.from_json_and_user, j, job_user)


class TestSchedulerAPI(TestCaseWithFactory):

    def server_proxy(self, user=None, password=None):
        return xmlrpclib.ServerProxy(
            'http://localhost/RPC2/',
            transport=TestTransport(user=user, password=password))

    def test_submit_job_rejects_anonymous(self):
        server = self.server_proxy()
        try:
            server.scheduler.submit_job("{}")
        except xmlrpclib.Fault as f:
            self.assertEqual(401, f.faultCode)
        else:
            self.fail("fault not raised")

    def test_submit_job_rejects_unpriv_user(self):
        User.objects.create_user('test', 'e@mail.invalid', 'test').save()
        server = self.server_proxy('test', 'test')
        try:
            server.scheduler.submit_job("{}")
        except xmlrpclib.Fault as f:
            self.assertEqual(403, f.faultCode)
        else:
            self.fail("fault not raised")

    def test_submit_job_sets_definition(self):
        user = User.objects.create_user('test', 'e@mail.invalid', 'test')
        user.user_permissions.add(
            Permission.objects.get(codename='add_testjob'))
        user.save()
        server = self.server_proxy('test', 'test')
        definition = self.factory.make_job_json()
        job_id = server.scheduler.submit_job(definition)
        job = TestJob.objects.get(id=job_id)
        self.assertEqual(definition, job.definition)

    def test_cancel_job_rejects_anonymous(self):
        job = self.factory.make_testjob()
        server = self.server_proxy()
        try:
            server.scheduler.cancel_job(job.id)
        except xmlrpclib.Fault as f:
            self.assertEqual(401, f.faultCode)
        else:
            self.fail("fault not raised")

    def test_cancel_job_rejects_unpriv_user(self):
        job = self.factory.make_testjob()
        User.objects.create_user('test', 'e@mail.invalid', 'test').save()
        server = self.server_proxy('test', 'test')
        try:
            server.scheduler.cancel_job(job.id)
        except xmlrpclib.Fault as f:
            self.assertEqual(403, f.faultCode)
        else:
            self.fail("fault not raised")

    def test_cancel_job_cancels_job(self):
        user = User.objects.create_user('test', 'e@mail.invalid', 'test')
        user.save()
        job = self.factory.make_testjob(submitter=user)
        server = self.server_proxy('test', 'test')
        server.scheduler.cancel_job(job.id)
        job = TestJob.objects.get(pk=job.pk)
        self.assertEqual(TestJob.CANCELED, job.status)


class TransactionTestCaseWithFactory(TransactionTestCase):

    def setUp(self):
        TransactionTestCase.setUp(self)
        self.factory = ModelFactory()


class NonthreadedDatabaseJobSource(DatabaseJobSource):
    deferToThread = staticmethod(lambda f, *args, **kw: f(*args, **kw))


class TestDBJobSource(TransactionTestCaseWithFactory):

    def setUp(self):
        super(TestDBJobSource, self).setUp()
        self.source = NonthreadedDatabaseJobSource()
        # The lava-health user is created by a migration in production
        # databases, but removed from the test database by the django
        # machinery.
        User.objects.create_user(
            username='lava-health', email='lava@lava.invalid')

    @property
    def health_job(self):
        return self.factory.make_job_json(health_check=True)

    @property
    def ordinary_job(self):
        return self.factory.make_job_json(health_check=False)

    def assertHealthJobAssigned(self, device):
        pass

    def assertHealthJobNotAssigned(self, device):
        pass

    def _makeBoardWithTags(self, tags):
        board = self.factory.make_device()
        for tag_name in tags:
            board.tags.add(Tag.objects.get_or_create(name=tag_name)[0])
        return board

    def _makeJobWithTagsForBoard(self, tags, board):
        job = self.factory.make_testjob(requested_device=board)
        for tag_name in tags:
            job.tags.add(Tag.objects.get_or_create(name=tag_name)[0])
        return job

    def assertBoardWithTagsGetsJobWithTags(self, board_tags, job_tags):
        pass

    def assertBoardWithTagsDoesNotGetJobWithTags(self, board_tags, job_tags):
        pass