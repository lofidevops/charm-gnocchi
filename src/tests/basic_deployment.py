# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import amulet
import json
import subprocess
import time


import charmhelpers.contrib.openstack.amulet.deployment as amulet_deployment
import charmhelpers.contrib.openstack.amulet.utils as os_amulet_utils

from gnocchiclient.v1 import client as gnocchi_client

# Use DEBUG to turn on debug logging
u = os_amulet_utils.OpenStackAmuletUtils(os_amulet_utils.DEBUG)


class GnocchiCharmDeployment(amulet_deployment.OpenStackAmuletDeployment):
    """Amulet tests on a basic gnocchi deployment."""

    gnocchi_svcs = ['haproxy', 'gnocchi-metricd', 'apache2']
    gnocchi_proc_names = gnocchi_svcs
    no_origin = ['memcached', 'percona-cluster', 'rabbitmq-server',
                 'ceph-mon', 'ceph-osd']

    def __init__(self, series, openstack=None, source=None, stable=True,
                 snap_source=None):
        """Deploy the entire test environment."""
        super(GnocchiCharmDeployment, self).__init__(series, openstack,
                                                     source, stable)
        self.snap_source = snap_source
        self._add_services()
        self._add_relations()
        self._configure_services()
        self._deploy()

        u.log.info('Waiting on extended status checks...')
        self.exclude_services = ['mysql', 'mongodb', 'memcached']
        # Ceilometer will come up blocked until the ceilometer-upgrade
        # action is run
        self.exclude_services.append("ceilometer")
        self._auto_wait_for_status(exclude_services=self.exclude_services)

        self._initialize_tests()
        self.run_ceilometer_upgrade_action()

    def _add_services(self):
        """Add services

           Add the services that we're testing, where gnocchi is local,
           and the rest of the service are from lp branches that are
           compatible with the local charm (e.g. stable or next).
           """
        this_service = {'name': 'gnocchi'}
        other_services = [
            {'name': 'percona-cluster'},
            {'name': 'ceilometer'},
            {'name': 'keystone'},
            {'name': 'rabbitmq-server'},
            {'name': 'memcached', 'location': 'cs:memcached'},
            {'name': 'ceph-mon', 'units': 3},
            {'name': 'ceph-osd', 'units': 3,
             'storage': {'osd-devices': 'cinder,10G'}},
        ]

        if self._get_openstack_release() < self.xenial_queens:
            other_services.append({'name': 'mongodb'})

        super(GnocchiCharmDeployment, self)._add_services(
            this_service,
            other_services,
            no_origin=self.no_origin
        )

    def _add_relations(self):
        """Add all of the relations for the services."""
        relations = {
            'keystone:shared-db': 'percona-cluster:shared-db',
            'gnocchi:identity-service': 'keystone:identity-service',
            'gnocchi:shared-db': 'percona-cluster:shared-db',
            'gnocchi:storage-ceph': 'ceph-mon:client',
            'gnocchi:metric-service': 'ceilometer:metric-service',
            'gnocchi:coordinator-memcached': 'memcached:cache',
            'ceilometer:amqp': 'rabbitmq-server:amqp',
            'ceph-mon:osd': 'ceph-osd:mon',
        }
        if self._get_openstack_release() >= self.xenial_queens:
            relations['ceilometer:identity-credentials'] = \
                'keystone:identity-credentials'
        else:
            relations['ceilometer:identity-service'] = \
                'keystone:identity-service'
            relations['ceilometer:shared-db'] = 'mongodb:database'
        super(GnocchiCharmDeployment, self)._add_relations(relations)

    def _configure_services(self):
        """Configure all of the services."""
        keystone_config = {'admin-password': 'openstack',
                           'admin-token': 'ubuntutesting'}
        gnocchi_config = {}
        if self.snap_source:
            gnocchi_config['openstack-origin'] = self.snap_source
        elif self.series == 'bionic' and self.openstack is None:
            # pending release of LP: #1813582
            gnocchi_config['openstack-origin'] = 'distro-proposed'
        configs = {'keystone': keystone_config,
                   'gnocchi': gnocchi_config}
        super(GnocchiCharmDeployment, self)._configure_services(configs)

    def _get_token(self):
        return self.keystone.service_catalog.catalog['token']['id']

    def _initialize_tests(self):
        """Perform final initialization before tests get run."""
        # Access the sentries for inspecting service units
        self.gnocchi_sentry = self.d.sentry['gnocchi'][0]
        self.mysql_sentry = self.d.sentry['percona-cluster'][0]
        self.keystone_sentry = self.d.sentry['keystone'][0]
        self.ceil_sentry = self.d.sentry['ceilometer'][0]

        # Authenticate admin with keystone endpoint
        self.keystone_session, self.keystone = u.get_default_keystone_session(
            self.keystone_sentry,
            openstack_release=self._get_openstack_release())

        # Authenticate admin with gnocchi endpoint
        gnocchi_ep = self.keystone.service_catalog.url_for(
            service_type='metric',
            interface='publicURL')

        self.gnocchi = gnocchi_client.Client(session=self.keystone_session,
                                             endpoint_override=gnocchi_ep)

    def check_and_wait(self, check_command, interval=2, max_wait=200,
                       desc=None):
        waited = 0
        while not check_command() or waited > max_wait:
            if desc:
                u.log.debug(desc)
            time.sleep(interval)
            waited = waited + interval
        if waited > max_wait:
            raise Exception('cmd failed {}'.format(check_command))

    def _run_action(self, unit_id, action, *args):
        command = ["juju", "action", "do", "--format=json", unit_id, action]
        command.extend(args)
        output = subprocess.check_output(command)
        output_json = output.decode(encoding="UTF-8")
        data = json.loads(output_json)
        action_id = data[u'Action queued with id']
        return action_id

    def _wait_on_action(self, action_id):
        command = ["juju", "action", "fetch", "--format=json", action_id]
        while True:
            try:
                output = subprocess.check_output(command)
            except Exception as e:
                print(e)
                return False
            output_json = output.decode(encoding="UTF-8")
            data = json.loads(output_json)
            if data[u"status"] == "completed":
                return True
            elif data[u"status"] == "failed":
                return False
            time.sleep(2)

    def run_ceilometer_upgrade_action(self):
        """Run ceilometer-upgrade

        This action will be run early to initialize ceilometer
        when gnocchi is related.
        Ceilometer will be in a blocked state until this runs.
        """
        u.log.debug('Checking ceilometer-upgrade')
        unit = self.ceil_sentry

        action_id = unit.run_action("ceilometer-upgrade")
        assert u.wait_on_action(action_id), "ceilometer-upgrade action failed"
        # Wait for acivte Unit is ready on ceilometer
        self.exclude_services.remove('ceilometer')
        self._auto_wait_for_status(exclude_services=self.exclude_services)
        u.log.debug('OK')

    def test_100_services(self):
        """Verify the expected services are running on the corresponding
           service units."""
        u.log.debug('Checking system services on units...')

        service_names = {
            self.gnocchi_sentry: self.gnocchi_svcs,
        }

        ret = u.validate_services_by_name(service_names)
        if ret:
            amulet.raise_status(amulet.FAIL, msg=ret)

        u.log.debug('OK')

    def test_200_api_connection(self):
        """Simple api calls to check service is up and responding"""
        u.log.debug('Checking api functionality...')
        assert(self.gnocchi.status.get() != [])
        u.log.debug('OK')

    def _assert_services(self, should_run):
        services = self.gnocchi_proc_names
        u.get_unit_process_ids(
            {self.gnocchi_sentry: services},
            expect_success=should_run)

    def test_910_pause_and_resume(self):
        """The services can be paused and resumed. """
        self._assert_services(should_run=True)
        action_id = u.run_action(self.gnocchi_sentry, "pause")
        assert u.wait_on_action(action_id), "Pause action failed."

        self._assert_services(should_run=False)

        action_id = u.run_action(self.gnocchi_sentry, "resume")
        assert u.wait_on_action(action_id), "Resume action failed"
        self._assert_services(should_run=True)


class GnocchiCharmSnapDeployment(GnocchiCharmDeployment):
    """Amulet tests on a snap based gnocchi deployment."""

    gnocchi_svcs = ['haproxy', 'snap.gnocchi.metricd',
                    'snap.gnocchi.uwsgi',
                    'snap.gnocchi.nginx']

    gnocchi_proc_names = ['haproxy', 'gnocchi-metricd', 'uwsgi', 'nginx']
