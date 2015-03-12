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

import mock

from nova import test
import os
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import vios

VIOS_FEED = 'fake_vios_feed.txt'


class TestVios(test.TestCase):

    def setUp(self):
        super(TestVios, self).setUp()
        self.pypvm_fix = self.useFixture(fx.PyPowerVM())
        self.adpt = self.pypvm_fix.apt

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()
        self.vios_feed_resp = resp(VIOS_FEED)

    def test_get_physical_wwpns(self):
        self.adpt.read.return_value = self.vios_feed_resp
        expected = set(['21000024FF649105', '21000024FF649104',
                        '21000024FF649107', '21000024FF649106'])
        result = set(vios.get_physical_wwpns(self.adpt, 'fake_uuid'))
        self.assertSetEqual(expected, result)

    def test_get_vios_wrap(self):
        self.adpt.read.return_value = self.vios_feed_resp.feed.entries[0]
        vios_w = vios.get_vios_wrap(self.adpt, 'vios_uuid', 'vios_name')
        self.assertIsNotNone(vios_w)
        self.assertIsInstance(vios_w, pvm_vios.VIOS)

    @mock.patch('pypowervm.wrappers.virtual_io_server.VStorageMapping.'
                '_crt_related_href')
    def test_add_vfc_mappings(self, mock_rel_href):
        # Mock data
        self.adpt.read.return_value = self.vios_feed_resp.feed.entries[0]
        mock_rel_href.return_value = 'href'
        vfc_map = pvm_vios.VFCMapping.bld(self.adpt, 'host_uuid',
                                          'client_lpar_uuid', 'fcs0',
                                          ['aa', 'bb'])

        # Validate that the vfc_mapping is proper size
        def validate_update(*kargs, **kwargs):
            vios_w = kargs[0]
            self.assertIsNotNone(vios_w)
            self.assertEqual(1, len(vios_w.vfc_mappings))

        self.adpt.update.side_effect = validate_update

        # Function call
        vios.add_vfc_mapping(self.adpt, 'vios_uuid', 'vios_name', vfc_map)

        # Make sure update is invoked
        self.assertEqual(1, self.adpt.update.call_count)