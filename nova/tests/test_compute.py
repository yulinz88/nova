# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Tests For Compute
"""

import datetime
import mox

from nova import compute
from nova import context
from nova import db
from nova import exception
from nova import flags
from nova import log as logging
from nova import rpc
from nova import test
from nova import utils
from nova.auth import manager
from nova.compute import manager as compute_manager
from nova.compute import power_state
from nova.db.sqlalchemy import models


LOG = logging.getLogger('nova.tests.compute')
FLAGS = flags.FLAGS
flags.DECLARE('stub_network', 'nova.compute.manager')
flags.DECLARE('live_migration_retry_count', 'nova.compute.manager')


class ComputeTestCase(test.TestCase):
    """Test case for compute"""
    def setUp(self):
        super(ComputeTestCase, self).setUp()
        self.flags(connection_type='fake',
                   stub_network=True,
                   network_manager='nova.network.manager.FlatManager')
        self.compute = utils.import_object(FLAGS.compute_manager)
        self.compute_api = compute.API()
        self.manager = manager.AuthManager()
        self.user = self.manager.create_user('fake', 'fake', 'fake')
        self.project = self.manager.create_project('fake', 'fake', 'fake')
        self.context = context.RequestContext('fake', 'fake', False)

    def tearDown(self):
        self.manager.delete_user(self.user)
        self.manager.delete_project(self.project)
        super(ComputeTestCase, self).tearDown()

    def _create_instance(self):
        """Create a test instance"""
        inst = {}
        inst['image_id'] = 'ami-test'
        inst['reservation_id'] = 'r-fakeres'
        inst['launch_time'] = '10'
        inst['user_id'] = self.user.id
        inst['project_id'] = self.project.id
        inst['instance_type'] = 'm1.tiny'
        inst['mac_address'] = utils.generate_mac()
        inst['ami_launch_index'] = 0
        return db.instance_create(self.context, inst)['id']

    def _create_group(self):
        values = {'name': 'testgroup',
                  'description': 'testgroup',
                  'user_id': self.user.id,
                  'project_id': self.project.id}
        return db.security_group_create(self.context, values)

    def _get_dummy_instance(self):
        """Get mock-return-value instance object
           Use this when any testcase executed later than test_run_terminate
        """
        vol1 = models.Volume()
        vol1.__setitem__('id', 1)
        vol2 = models.Volume()
        vol2.__setitem__('id', 2)
        instance_ref = models.Instance()
        instance_ref.__setitem__('id', 1)
        instance_ref.__setitem__('volumes', [vol1, vol2])
        instance_ref.__setitem__('hostname', 'i-00000001')
        instance_ref.__setitem__('host', 'dummy')
        return instance_ref

    def test_create_instance_defaults_display_name(self):
        """Verify that an instance cannot be created without a display_name."""
        cases = [dict(), dict(display_name=None)]
        for instance in cases:
            ref = self.compute_api.create(self.context,
                FLAGS.default_instance_type, None, **instance)
            try:
                self.assertNotEqual(ref[0]['display_name'], None)
            finally:
                db.instance_destroy(self.context, ref[0]['id'])

    def test_create_instance_associates_security_groups(self):
        """Make sure create associates security groups"""
        group = self._create_group()
        instance_ref = models.Instance()
        instance_ref.__setitem__('id', 1)
        instance_ref.__setitem__('volumes', [{'id': 1}, {'id': 2}])
        instance_ref.__setitem__('hostname', 'i-00000001')
        return instance_ref

    def test_create_instance_defaults_display_name(self):
        """Verify that an instance cannot be created without a display_name."""
        cases = [dict(), dict(display_name=None)]
        for instance in cases:
            ref = self.compute_api.create(self.context,
                FLAGS.default_instance_type, None, **instance)
            try:
                self.assertNotEqual(ref[0]['display_name'], None)
            finally:
                db.instance_destroy(self.context, ref[0]['id'])

    def test_create_instance_associates_security_groups(self):
        """Make sure create associates security groups"""
        group = self._create_group()
        ref = self.compute_api.create(
                self.context,
                instance_type=FLAGS.default_instance_type,
                image_id=None,
                security_group=['testgroup'])
        try:
            self.assertEqual(len(db.security_group_get_by_instance(
                             self.context, ref[0]['id'])), 1)
            group = db.security_group_get(self.context, group['id'])
            self.assert_(len(group.instances) == 1)
        finally:
            db.security_group_destroy(self.context, group['id'])
            db.instance_destroy(self.context, ref[0]['id'])

    def test_destroy_instance_disassociates_security_groups(self):
        """Make sure destroying disassociates security groups"""
        group = self._create_group()

        ref = self.compute_api.create(
                self.context,
                instance_type=FLAGS.default_instance_type,
                image_id=None,
                security_group=['testgroup'])
        try:
            db.instance_destroy(self.context, ref[0]['id'])
            group = db.security_group_get(self.context, group['id'])
            self.assert_(len(group.instances) == 0)
        finally:
            db.security_group_destroy(self.context, group['id'])

    def test_destroy_security_group_disassociates_instances(self):
        """Make sure destroying security groups disassociates instances"""
        group = self._create_group()

        ref = self.compute_api.create(
                self.context,
                instance_type=FLAGS.default_instance_type,
                image_id=None,
                security_group=['testgroup'])

        try:
            db.security_group_destroy(self.context, group['id'])
            group = db.security_group_get(context.get_admin_context(
                                          read_deleted=True), group['id'])
            self.assert_(len(group.instances) == 0)
        finally:
            db.instance_destroy(self.context, ref[0]['id'])

    def test_run_terminate(self):
        """Make sure it is possible to  run and terminate instance"""
        instance_id = self._create_instance()

        self.compute.run_instance(self.context, instance_id)

        instances = db.instance_get_all(context.get_admin_context())
        LOG.info(_("Running instances: %s"), instances)
        self.assertEqual(len(instances), 1)

        self.compute.terminate_instance(self.context, instance_id)

        instances = db.instance_get_all(context.get_admin_context())
        LOG.info(_("After terminating instances: %s"), instances)
        self.assertEqual(len(instances), 0)

    def test_run_terminate_timestamps(self):
        """Make sure timestamps are set for launched and destroyed"""
        instance_id = self._create_instance()
        instance_ref = db.instance_get(self.context, instance_id)
        self.assertEqual(instance_ref['launched_at'], None)
        self.assertEqual(instance_ref['deleted_at'], None)
        launch = datetime.datetime.utcnow()
        self.compute.run_instance(self.context, instance_id)
        instance_ref = db.instance_get(self.context, instance_id)
        self.assert_(instance_ref['launched_at'] > launch)
        self.assertEqual(instance_ref['deleted_at'], None)
        terminate = datetime.datetime.utcnow()
        self.compute.terminate_instance(self.context, instance_id)
        self.context = self.context.elevated(True)
        instance_ref = db.instance_get(self.context, instance_id)
        self.assert_(instance_ref['launched_at'] < terminate)
        self.assert_(instance_ref['deleted_at'] > terminate)

    def test_pause(self):
        """Ensure instance can be paused"""
        instance_id = self._create_instance()
        self.compute.run_instance(self.context, instance_id)
        self.compute.pause_instance(self.context, instance_id)
        self.compute.unpause_instance(self.context, instance_id)
        self.compute.terminate_instance(self.context, instance_id)

    def test_suspend(self):
        """ensure instance can be suspended"""
        instance_id = self._create_instance()
        self.compute.run_instance(self.context, instance_id)
        self.compute.suspend_instance(self.context, instance_id)
        self.compute.resume_instance(self.context, instance_id)
        self.compute.terminate_instance(self.context, instance_id)

    def test_reboot(self):
        """Ensure instance can be rebooted"""
        instance_id = self._create_instance()
        self.compute.run_instance(self.context, instance_id)
        self.compute.reboot_instance(self.context, instance_id)
        self.compute.terminate_instance(self.context, instance_id)

    def test_set_admin_password(self):
        """Ensure instance can have its admin password set"""
        instance_id = self._create_instance()
        self.compute.run_instance(self.context, instance_id)
        self.compute.set_admin_password(self.context, instance_id)
        self.compute.terminate_instance(self.context, instance_id)

    def test_inject_file(self):
        """Ensure we can write a file to an instance"""
        instance_id = self._create_instance()
        self.compute.run_instance(self.context, instance_id)
        self.compute.inject_file(self.context, instance_id, "/tmp/test",
                "File Contents")
        self.compute.terminate_instance(self.context, instance_id)

    def test_snapshot(self):
        """Ensure instance can be snapshotted"""
        instance_id = self._create_instance()
        name = "myfakesnapshot"
        self.compute.run_instance(self.context, instance_id)
        self.compute.snapshot_instance(self.context, instance_id, name)
        self.compute.terminate_instance(self.context, instance_id)

    def test_console_output(self):
        """Make sure we can get console output from instance"""
        instance_id = self._create_instance()
        self.compute.run_instance(self.context, instance_id)

        console = self.compute.get_console_output(self.context,
                                                        instance_id)
        self.assert_(console)
        self.compute.terminate_instance(self.context, instance_id)

    def test_ajax_console(self):
        """Make sure we can get console output from instance"""
        instance_id = self._create_instance()
        self.compute.run_instance(self.context, instance_id)

        console = self.compute.get_ajax_console(self.context,
                                                instance_id)
        self.assert_(console)
        self.compute.terminate_instance(self.context, instance_id)

    def test_run_instance_existing(self):
        """Ensure failure when running an instance that already exists"""
        instance_id = self._create_instance()
        self.compute.run_instance(self.context, instance_id)
        self.assertRaises(exception.Error,
                          self.compute.run_instance,
                          self.context,
                          instance_id)
        self.compute.terminate_instance(self.context, instance_id)

    def test_lock(self):
        """ensure locked instance cannot be changed"""
        instance_id = self._create_instance()
        self.compute.run_instance(self.context, instance_id)

        non_admin_context = context.RequestContext(None, None, False, False)

        # decorator should return False (fail) with locked nonadmin context
        self.compute.lock_instance(self.context, instance_id)
        ret_val = self.compute.reboot_instance(non_admin_context, instance_id)
        self.assertEqual(ret_val, False)

        # decorator should return None (success) with unlocked nonadmin context
        self.compute.unlock_instance(self.context, instance_id)
        ret_val = self.compute.reboot_instance(non_admin_context, instance_id)
        self.assertEqual(ret_val, None)

        self.compute.terminate_instance(self.context, instance_id)

    def _setup_other_managers(self):
        self.volume_manager = utils.import_object(FLAGS.volume_manager)
        self.network_manager = utils.import_object(FLAGS.network_manager)
        self.compute_driver = utils.import_object(FLAGS.compute_driver)

    def test_pre_live_migration_instance_has_no_fixed_ip(self):
        """
           if instances that are intended to be migrated doesnt have fixed_ip
           (not happens usually), pre_live_migration has to raise Exception.
        """
        instance_ref = self._get_dummy_instance()
        c = context.get_admin_context()
        i_id = instance_ref['id']

        dbmock = self.mox.CreateMock(db)
        dbmock.instance_get(c, i_id).AndReturn(instance_ref)
        dbmock.instance_get_fixed_address(c, i_id).AndReturn(None)

        self.compute.db = dbmock
        self.mox.ReplayAll()
        self.assertRaises(exception.NotFound,
                          self.compute.pre_live_migration,
                          c, instance_ref['id'])
        self.mox.ResetAll()

    def test_pre_live_migration_instance_has_volume(self):
        """if any volumes are attached to the instances that are
           intended to be migrated, setup_compute_volume must be
           called because aoe module should be inserted at destination
           host. This testcase checks on it.
        """
        i_ref = self._get_dummy_instance()
        c = context.get_admin_context()

        self._setup_other_managers()
        dbmock = self.mox.CreateMock(db)
        volmock = self.mox.CreateMock(self.volume_manager)
        netmock = self.mox.CreateMock(self.network_manager)
        drivermock = self.mox.CreateMock(self.compute_driver)

        dbmock.instance_get(c, i_ref['id']).AndReturn(i_ref)
        dbmock.instance_get_fixed_address(c, i_ref['id']).AndReturn('dummy')
        for i in range(len(i_ref['volumes'])):
            vid = i_ref['volumes'][i]['id']
            volmock.setup_compute_volume(c, vid).InAnyOrder('g1')
        netmock.setup_compute_network(c, i_ref['id'])
        drivermock.ensure_filtering_rules_for_instance(i_ref)

        self.compute.db = dbmock
        self.compute.volume_manager = volmock
        self.compute.network_manager = netmock
        self.compute.driver = drivermock

        self.mox.ReplayAll()
        ret = self.compute.pre_live_migration(c, i_ref['id'])
        self.assertEqual(ret, None)
        self.mox.ResetAll()

    def test_pre_live_migration_instance_has_no_volume(self):
        """if any volumes are not attached to the instances that are
           intended to be migrated, log message should be appears
           because administrator can proove instance conditions before
           live_migration if any trouble occurs.
        """
        i_ref = self._get_dummy_instance()
        i_ref.__setitem__('volumes', [])
        c = context.get_admin_context()

        self._setup_other_managers()
        dbmock = self.mox.CreateMock(db)
        netmock = self.mox.CreateMock(self.network_manager)
        drivermock = self.mox.CreateMock(self.compute_driver)

        dbmock.instance_get(c, i_ref['id']).AndReturn(i_ref)
        dbmock.instance_get_fixed_address(c, i_ref['id']).AndReturn('dummy')
        self.mox.StubOutWithMock(compute_manager.LOG, 'info')
        compute_manager.LOG.info(_("%s has no volume."), i_ref['hostname'])
        netmock.setup_compute_network(c, i_ref['id'])
        drivermock.ensure_filtering_rules_for_instance(i_ref)

        self.compute.db = dbmock
        self.compute.network_manager = netmock
        self.compute.driver = drivermock

        self.mox.ReplayAll()
        ret = self.compute.pre_live_migration(c, i_ref['id'])
        self.assertEqual(ret, None)
        self.mox.ResetAll()

    def test_pre_live_migration_setup_compute_node_fail(self):
        """setup_compute_node sometimes fail since concurrent request
           comes to iptables and iptables complains. Then this method
           tries to retry, but raise exception in case of over
            max_retry_count. this method confirms raising exception.
        """
        i_ref = self._get_dummy_instance()
        c = context.get_admin_context()

        self._setup_other_managers()
        dbmock = self.mox.CreateMock(db)
        netmock = self.mox.CreateMock(self.network_manager)
        volmock = self.mox.CreateMock(self.volume_manager)

        dbmock.instance_get(c, i_ref['id']).AndReturn(i_ref)
        dbmock.instance_get_fixed_address(c, i_ref['id']).AndReturn('dummy')
        for i in range(len(i_ref['volumes'])):
            volmock.setup_compute_volume(c, i_ref['volumes'][i]['id'])
        for i in range(FLAGS.live_migration_retry_count):
            netmock.setup_compute_network(c, i_ref['id']).\
                AndRaise(exception.ProcessExecutionError())

        self.compute.db = dbmock
        self.compute.network_manager = netmock
        self.compute.volume_manager = volmock

        self.mox.ReplayAll()
        self.assertRaises(exception.ProcessExecutionError,
                          self.compute.pre_live_migration,
                          c, i_ref['id'])
        self.mox.ResetAll()

    def test_live_migration_instance_has_volume(self):
        """Any volumes are mounted by instances to be migrated are found,
           vblade health must be checked before starting live-migration.
           And that is checked by check_for_export().
           This testcase confirms check_for_export() is called.
        """
        i_ref = self._get_dummy_instance()
        c = context.get_admin_context()
        topic = db.queue_get_for(c, FLAGS.compute_topic, i_ref['host'])

        dbmock = self.mox.CreateMock(db)
        dbmock.instance_get(c, i_ref['id']).AndReturn(i_ref)
        self.mox.StubOutWithMock(rpc, 'call')
        rpc.call(c, FLAGS.volume_topic, {"method": "check_for_export",
                                         "args": {'instance_id': i_ref['id']}})
        dbmock.queue_get_for(c, FLAGS.compute_topic, i_ref['host']).\
                             AndReturn(topic)
        rpc.call(c, topic, {"method": "pre_live_migration",
                            "args": {'instance_id': i_ref['id']}})
        self.mox.StubOutWithMock(self.compute.driver, 'live_migration')
        self.compute.driver.live_migration(c, i_ref, i_ref['host'],
                                  self.compute.post_live_migration,
                                  self.compute.recover_live_migration)

        self.compute.db = dbmock
        self.mox.ReplayAll()
        ret = self.compute.live_migration(c, i_ref['id'], i_ref['host'])
        self.assertEqual(ret, None)
        self.mox.ResetAll()

    def test_live_migration_instance_has_volume_and_exception(self):
        """In addition to test_live_migration_instance_has_volume testcase,
           this testcase confirms if any exception raises from
           check_for_export(). Then, valid seaquence of this method should
           recovering instance/volumes status(ex. instance['state_description']
           is changed from 'migrating' -> 'running', was changed by scheduler)
        """
        i_ref = self._get_dummy_instance()
        c = context.get_admin_context()
        topic = db.queue_get_for(c, FLAGS.compute_topic, i_ref['host'])

        dbmock = self.mox.CreateMock(db)
        dbmock.instance_get(c, i_ref['id']).AndReturn(i_ref)
        self.mox.StubOutWithMock(rpc, 'call')
        rpc.call(c, FLAGS.volume_topic, {"method": "check_for_export",
                                         "args": {'instance_id': i_ref['id']}})
        dbmock.queue_get_for(c, FLAGS.compute_topic, i_ref['host']).\
                             AndReturn(topic)
        rpc.call(c, topic, {"method": "pre_live_migration",
                            "args": {'instance_id': i_ref['id']}}).\
                            AndRaise(rpc.RemoteError('', '', ''))
        dbmock.instance_update(c, i_ref['id'], {'state_description': 'running',
                                                'state': power_state.RUNNING,
                                                'host': i_ref['host']})
        for v in i_ref['volumes']:
            dbmock.volume_update(c, v['id'], {'status': 'in-use',
                                              'host': i_ref['host']})

        self.compute.db = dbmock
        self.mox.ReplayAll()
        self.assertRaises(rpc.RemoteError,
                          self.compute.live_migration,
                          c, i_ref['id'], i_ref['host'])
        self.mox.ResetAll()

    def test_live_migration_instance_has_no_volume_and_exception(self):
        """Simpler than
           test_live_migration_instance_has_volume_and_exception
        """
        i_ref = self._get_dummy_instance()
        i_ref.__setitem__('volumes', [])
        c = context.get_admin_context()
        topic = db.queue_get_for(c, FLAGS.compute_topic, i_ref['host'])

        dbmock = self.mox.CreateMock(db)
        dbmock.instance_get(c, i_ref['id']).AndReturn(i_ref)
        dbmock.queue_get_for(c, FLAGS.compute_topic, i_ref['host']).\
                             AndReturn(topic)
        self.mox.StubOutWithMock(rpc, 'call')
        rpc.call(c, topic, {"method": "pre_live_migration",
                            "args": {'instance_id': i_ref['id']}}).\
                            AndRaise(rpc.RemoteError('', '', ''))
        dbmock.instance_update(c, i_ref['id'], {'state_description': 'running',
                                                'state': power_state.RUNNING,
                                                'host': i_ref['host']})

        self.compute.db = dbmock
        self.mox.ReplayAll()
        self.assertRaises(rpc.RemoteError,
                          self.compute.live_migration,
                          c, i_ref['id'], i_ref['host'])
        self.mox.ResetAll()

    def test_live_migration_instance_has_no_volume(self):
        """Simpler than test_live_migration_instance_has_volume."""
        i_ref = self._get_dummy_instance()
        i_ref.__setitem__('volumes', [])
        c = context.get_admin_context()
        topic = db.queue_get_for(c, FLAGS.compute_topic, i_ref['host'])

        dbmock = self.mox.CreateMock(db)
        dbmock.instance_get(c, i_ref['id']).AndReturn(i_ref)
        self.mox.StubOutWithMock(rpc, 'call')
        dbmock.queue_get_for(c, FLAGS.compute_topic, i_ref['host']).\
                             AndReturn(topic)
        rpc.call(c, topic, {"method": "pre_live_migration",
                            "args": {'instance_id': i_ref['id']}})
        self.mox.StubOutWithMock(self.compute.driver, 'live_migration')
        self.compute.driver.live_migration(c, i_ref, i_ref['host'],
                                  self.compute.post_live_migration,
                                  self.compute.recover_live_migration)

        self.compute.db = dbmock
        self.mox.ReplayAll()
        ret = self.compute.live_migration(c, i_ref['id'], i_ref['host'])
        self.assertEqual(ret, None)
        self.mox.ResetAll()

    def test_post_live_migration_working_correctly(self):
        """post_live_migration works as expected correctly """

        dest = 'desthost'
        flo_addr = '1.2.1.2'

        # Preparing datas
        c = context.get_admin_context()
        instance_id = self._create_instance()
        i_ref = db.instance_get(c, instance_id)
        db.instance_update(c, i_ref['id'], {'state_description': 'migrating',
                                            'state': power_state.PAUSED})
        v_ref = db.volume_create(c, {'size': 1, 'instance_id': instance_id})
        fix_addr = db.fixed_ip_create(c, {'address': '1.1.1.1',
                                          'instance_id': instance_id})
        fix_ref = db.fixed_ip_get_by_address(c, fix_addr)
        flo_ref = db.floating_ip_create(c, {'address': flo_addr,
                                        'fixed_ip_id': fix_ref['id']})
        # reload is necessary before setting mocks
        i_ref = db.instance_get(c, instance_id)

        # Preparing mocks
        self.mox.StubOutWithMock(self.compute.volume_manager,
                                 'remove_compute_volume')
        for v in i_ref['volumes']:
            self.compute.volume_manager.remove_compute_volume(c, v['id'])
        self.mox.StubOutWithMock(self.compute.driver, 'unfilter_instance')
        self.compute.driver.unfilter_instance(i_ref)

        # executing
        self.mox.ReplayAll()
        ret = self.compute.post_live_migration(c, i_ref, dest)
        self.mox.UnsetStubs()

        # make sure every data is rewritten to dest
        i_ref = db.instance_get(c, i_ref['id'])
        c1 = (i_ref['host'] == dest)
        v_ref = db.volume_get(c, v_ref['id'])
        c2 = (v_ref['host'] == dest)
        c3 = False
        flo_refs = db.floating_ip_get_all_by_host(c, dest)
        c3 = (len(flo_refs) != 0 and flo_refs[0]['address'] == flo_addr)

        # post operaton
        self.assertTrue(c1 and c2 and c3)
        db.instance_destroy(c, instance_id)
        db.volume_destroy(c, v_ref['id'])
        db.floating_ip_destroy(c, flo_addr)
