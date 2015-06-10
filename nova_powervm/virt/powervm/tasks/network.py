# Copyright 2015 IBM Corp.
#
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

import eventlet

from nova import exception
from nova.i18n import _LI, _LE
from nova import utils

from oslo_config import cfg
from oslo_log import log as logging
from taskflow import task

from pypowervm.wrappers import base_partition as pvm_bp

from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)
CONF = cfg.CONF
CONF.import_opt('vif_plugging_is_fatal', 'nova.virt.driver')
CONF.import_opt('vif_plugging_timeout', 'nova.virt.driver')


def state_ok_for_plug(lpar_wrap):
    """Determines if a LPAR is in an OK state for network plugging."""
    if lpar_wrap.state == pvm_bp.LPARState.NOT_ACTIVATED:
        return True
    elif (lpar_wrap.state == pvm_bp.LPARState.RUNNING and
          lpar_wrap.rmc_state == pvm_bp.RMCState.ACTIVE):
        return True
    return False


class PlugVifs(task.Task):
    """The task to plug the Virtual Network Interfaces to a VM."""

    def __init__(self, virt_api, adapter, instance, network_info, host_uuid):
        """Create the task.

        Provides the 'vm_cnas' - the Virtual Machine's Client Network Adapters.

        :param virt_api: The VirtAPI for the operation.
        :param adapter: The pypowervm adapter.
        :param instance: The nova instance.
        :param network_info: The network information containing the nova
                             VIFs to create.
        :param host_uuid: The host system's PowerVM UUID.
        """
        self.virt_api = virt_api
        self.adapter = adapter
        self.instance = instance
        self.network_info = network_info
        self.host_uuid = host_uuid

        super(PlugVifs, self).__init__(name='plug_vifs', provides='vm_cnas',
                                       requires=['lpar_wrap'])

    def execute(self, lpar_wrap):
        LOG.info(_LI('Plugging the Network Interfaces to instance %s'),
                 self.instance.name)

        # Check to see if the LPAR is OK to add VIFs to.
        if not state_ok_for_plug(lpar_wrap):
            LOG.error(_LE('Unable to create VIF(s) for instance %(sys)s.  The '
                          'VM was in a state where VIF plugging is not '
                          'acceptable.  The system may be running without an '
                          'active RMC connection.'),
                      {'sys': self.instance.name}, instance=self.instance)
            raise exception.VirtualInterfaceCreateException()

        # Get the current adapters on the system
        cna_w_list = vm.get_cnas(self.adapter, self.instance, self.host_uuid)

        # Trim the VIFs down to the ones that haven't yet been created.
        crt_vifs = []
        for vif in self.network_info:
            for cna_w in cna_w_list:
                if vm.norm_mac(cna_w.mac) == vif['address']:
                    break
            else:
                crt_vifs.append(vif)

        # For the VIFs, run the creates (and wait for the events back)
        try:
            with self.virt_api.wait_for_instance_event(
                    self.instance, self._get_vif_events(),
                    deadline=CONF.vif_plugging_timeout,
                    error_callback=self._vif_callback_failed):
                for vif in crt_vifs:
                    LOG.info(_LI('Creating VIF with mac %(mac)s for instance '
                                 '%(sys)s'),
                             {'mac': vif['address'],
                              'sys': self.instance.name},
                             instance=self.instance)
                    vm.crt_vif(self.adapter, self.instance, self.host_uuid,
                               vif)
        except eventlet.timeout.Timeout:
            LOG.error(_LE('Error waiting for VIF to be created for instance '
                          '%(sys)s'), {'sys': self.instance.name},
                      instance=self.instance)
            raise exception.VirtualInterfaceCreateException()

        # Return the list of created VIFs.
        return cna_w_list

    def _vif_callback_failed(self, event_name, instance):
        LOG.error(_LE('VIF Plug failure for callback on event '
                      '%(event)s for instance %(uuid)s'),
                  {'event': event_name, 'uuid': instance.uuid})
        if CONF.vif_plugging_is_fatal:
            raise exception.VirtualInterfaceCreateException()

    def _get_vif_events(self):
        """Returns the VIF events that need to be received for a VIF plug.

        In order for a VIF plug to be successful, certain events should be
        received from other components within the OpenStack ecosystem.  If
        using neutron, certain events are needed.  If Nova networking, then no
        events are required.  This method returns the events needed for a given
        deploy.
        """
        # See libvirt's driver.py -> _get_neutron_events method for
        # more information.
        if (utils.is_neutron() and CONF.vif_plugging_is_fatal and
                CONF.vif_plugging_timeout):
            return [('network-vif-plugged', vif['id'])
                    for vif in self.network_info
                    if not vif.get('active', True)]
        else:
            return []


class PlugMgmtVif(task.Task):
    """The task to plug the Management VIF into a VM."""

    def __init__(self, adapter, instance, host_uuid):
        """Create the task.

        Requires the 'vm_cnas'.

        Provides the mgmt_cna.  This may be none if no management device was
        created.  This is the CNA of the mgmt vif for the VM.

        :param adapter: The pypowervm adapter.
        :param instance: The nova instance.
        :param host_uuid: The host system's PowerVM UUID.
        """
        self.adapter = adapter
        self.instance = instance
        self.host_uuid = host_uuid

        super(PlugMgmtVif, self).__init__(
            name='plug_mgmt_vif', provides='mgmt_cna', requires=['vm_cnas'])

    def execute(self, vm_cnas):
        LOG.info(_LI('Plugging the Management Network Interface to instance '
                     '%s'), self.instance.name, instance=self.instance)
        # Determine if we need to create the secure RMC VIF.  This should only
        # be needed if there is not a VIF on the secure RMC vSwitch
        vswitch_w = vm.get_secure_rmc_vswitch(self.adapter, self.host_uuid)
        if vswitch_w is None:
            LOG.debug('No management VIF created for instance %s due to '
                      'lack of Management Virtual Switch',
                      self.instance.name)
            return None

        # This next check verifies that there are no existing NICs on the
        # vSwitch, so that the VM does not end up with multiple RMC VIFs.
        for cna_w in vm_cnas:
            if cna_w.vswitch_uri == vswitch_w.href:
                LOG.debug('Management VIF already created for instance %s',
                          self.instance.name)
                return None

        # Return the created management CNA
        return vm.crt_secure_rmc_vif(self.adapter, self.instance,
                                     self.host_uuid)
