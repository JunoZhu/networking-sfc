# Copyright 2016 All rights reserved.
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

from neutron import manager

from neutron.agent.ovsdb.native import idlutils

from oslo_log import helpers as log_helpers
from oslo_log import log as logging

from networking_sfc.extensions import flowclassifier
from networking_sfc.services.sfc.drivers import base as driver_base
from networking_sfc.services.sfc.drivers.ovs import(
    db as ovs_sfc_db)
from networking_sfc._i18n import _LW, _LI

from networking_ovn.common import utils
from networking_ovn.ovsdb import impl_idl_ovn

LOG = logging.getLogger(__name__)


class OVNSfcDriver(driver_base.SfcDriverBase,
                   ovs_sfc_db.OVSSfcDriverDB):
    """Sfc Driver Base Class."""

    def initialize(self):
        super(OVNSfcDriver, self).initialize()
        self._ovn_property = None
        LOG.debug("OVN SFC driver init done")

    @log_helpers.log_method_call
    def _get_portpair_ids(self, context, pg_id):
        pg = context._plugin.get_port_pair_group(context._plugin_context,
                                                 pg_id)
        return pg['port_pairs']

    def _get_port_pair_detail(self, context, port_pair_id):
        pp = context._plugin.get_port_pair(context._plugin_context,
                                           port_pair_id)
        return pp

    def _get_portchain_fcs(self, port_chain):
        return self._get_fcs_by_ids(port_chain['flow_classifiers'])

    def _get_fcs_by_ids(self, fc_ids):
        flow_classifiers = []
        if not fc_ids:
            return flow_classifiers

        # Get the portchain flow classifiers
        fc_plugin = (
            manager.NeutronManager.get_service_plugins().get(
                flowclassifier.FLOW_CLASSIFIER_EXT)
        )
        if not fc_plugin:
            LOG.warning(_LW("Not found the flow classifier service plugin"))
            return flow_classifiers

        for fc_id in fc_ids:
            fc = fc_plugin.get_flow_classifier(self.admin_context, fc_id)
            flow_classifiers.append(fc)

        return flow_classifiers

    def _create_ovn_dict(self, context, port_chain):
        ovn_dict = {}
        ovn_dict = {
            'id': port_chain['id'],
            'name': port_chain['name'],
            'tenant_id': port_chain['tenant_id'],
            'description': port_chain['description'],
            'port_pair_groups': port_chain['port_pair_groups']
        }
        #
        # Loop over port-pair group gettting individual port-pairs for VNF
        #
        port_pair_group_list = []
        for port_group_item in port_chain['port_pair_groups']:
            port_pair_list = []
            port_pair_group = {}
            port_pair_id = self._get_portpair_ids(context, port_group_item)
            for port_pair_item in port_pair_id:
                LOG.debug("Port Pair Id: %s " % port_pair_id)
                port_pair_detail = self._get_port_pair_detail(
                    context, port_pair_item)
                port_pair_list.append(port_pair_detail)
            port_pair_group["port_pairs"] = port_pair_list
            port_pair_group["id"] = port_group_item
            port_pair_group_list.append(port_pair_group)
        ovn_dict['port_pair_groups'] = port_pair_group_list
        ovn_dict['flow_classifier'] = self._get_portchain_fcs(port_chain)
        LOG.debug("Port Chain Definition: %s " % ovn_dict)
        return ovn_dict

    @log_helpers.log_method_call
    def create_port_chain(self, context):
        port_chain = context.current
        ovn_dict = self._create_ovn_dict(context, port_chain)
        # create ACL entry for flow classifier
        flow_classifiers = ovn_dict['flow_classifier']
        with self._ovn.transaction(check_error=True) as txn:
            for flow_classifier in flow_classifiers:
                flow_classifier_name = self._sfc_name(flow_classifier['id'])
                lswitch_name = self._check_lswitch_exists(
                    context, flow_classifier['logical_source_port'])
                if lswitch_name is None:
                    LOG.error("Logical switch does not exist for "
                              "flow_classifier logical source port: %s" %
                              flow_classifier['logical_source_port'])
                    return False
                lport_uuid_src = self._check_logical_port_exist(
                    flow_classifier['logical_source_port'])
                if lport_uuid_src is None:
                    LOG.error("Logical port %s does not exist",
                              flow_classifier['logical_source_port'])
                    return False
                lport_uuid_dst = self._check_logical_port_exist(
                    flow_classifier['logical_destination_port'])
                if lport_uuid_dst is None:
                    LOG.error("Logical port %s does not exist",
                              flow_classifier['logical_destination_port'])
                    return False
                acl = {'match': 'inport == %s && outport = %s' % (lport_uuid_src, lport_uuid_dst),
                       'action': 'sfc',
                       'priority': 2000,
                       'direction': 'from-lport'}
                txn.add(self._ovn.add_sfc_flow_classifier(lswitch_name, flow_classifier_name, **acl))

    @log_helpers.log_method_call
    def delete_port_chain(self, context):
        pass

    @log_helpers.log_method_call
    def update_port_chain(self, context):
        pass

    @log_helpers.log_method_call
    def create_port_pair_group(self, context):
        pass

    @log_helpers.log_method_call
    def delete_port_pair_group(self, context):
        pass

    @log_helpers.log_method_call
    def update_port_pair_group(self, context):
        pass

    @log_helpers.log_method_call
    def create_port_pair(self, context):
        pass

    @log_helpers.log_method_call
    def delete_port_pair(self, context):
        pass

    @log_helpers.log_method_call
    def update_port_pair(self, context):
        pass

    #
    # Networking OVN Interface
    #
    @property
    def _ovn(self):
        if self._ovn_property is None:
            LOG.info(_LI("Getting OvsdbOvnIdl"))
            self._ovn_property = impl_idl_ovn.OvsdbNbOvnIdl(self)
        return self._ovn_property

    #
    # Interface into OVN - adds new rules to direct
    # traffic to VNF port-pair
    #
    def _sfc_name(self, id):
        # The name of the OVN entry will be neutron-sfc-<UUID>
        # This is due to the fact that the OVN application checks if the name
        # is a UUID. If so then there will be no matches.
        # We prefix the UUID to enable us to use the Neutron UUID when
        # updating, deleting etc.
        return 'neutron-sfc-%s' % id

    #
    # Check logical switch exists for network port
    #
    def _check_lswitch_exists(self, context, port_id):
        lswitch_name = None
        core_plugin = manager.NeutronManager.get_plugin()
        #
        # Get network id belonging to port
        #
        port = core_plugin.get_port(self.admin_context, port_id)
        #
        # Check network exists
        #
        lswitch_name = utils.ovn_name(port['network_id'])
        try:
            idlutils.row_by_value(self._ovn.idl, 'Logical_Switch',
                                  'name', lswitch_name)
        except idlutils.RowNotFound:
            msg = ("Logical Switch %s does not exist got port_id %s") % (
                lswitch_name, port_id)
            LOG.error(msg)
            lswitch_name = None
        return lswitch_name

    #
    # Get the logical port uuid
    #
    def _check_logical_port_exist(self, port_name):
        lport_uuid = None
        try:
            lport = idlutils.row_by_value(self._ovn.idl, 'Logical_Switch_Port',
                                          'name', port_name)
            lport_uuid = lport.uuid
        except idlutils.RowNotFound:
            LOG.error("Logical Port %s does not exist", port_name)
            lport_uuid = None
        return lport_uuid

    #
    # Get the port pair uuid
    #
    def _get_port_pair_uuid(self, port_pair_name):
        lpp_uuid = None
        try:
            lpp = idlutils.row_by_value(self._ovn.idl, 'Logical_Port_Pair',
                                        'name', port_pair_name)
            lpp_uuid = lpp.uuid
        except idlutils.RowNotFound:
            LOG.error("Logical Port Pair %s does not exist", port_pair_name)
            lpp_uuid = None
        return lpp_uuid

    def _get_port_pairs_in_port_pair_group(self, port_pair_group_name):
        lppg = idlutils.row_by_value(self._ovn.idl, 'Logical_Port_Pair_Group',
                                     'name', port_pair_group_name)
        return lppg.port_pairs

    def _get_flow_classifier_uuid(self, fc_name):
        fc_uuid = None
        try:
            fc = idlutils.row_by_value(self._ovn.idl,
                                       'Logical_Flow_Classifier',
                                       'name', fc_name)
            fc_uuid = fc.uuid
        except idlutils.RowNotFound:
            LOG.error("Logical flow classifier %s does not exist", fc_name)
            # raise RuntimeError(msg)
        return fc_uuid

    def _set_ovn_port_pair_group(self, port_pair_group):
        LOG.debug("Update ovn port pair group %s", port_pair_group['id'])
        port_pair_uuid_list = []
        port_pairs = port_pair_group['port_pairs']
        lport_pair_group_name = self._sfc_name(port_pair_group['id'])
        with self._ovn.transaction(check_error=True) as txn:
            for port_pair in port_pairs:
                lport_pair_name = self._sfc_name(port_pair)
                port_pair_uuid = self._get_port_pair_uuid(lport_pair_name)
                if port_pair_uuid is None:
                    LOG.error("Logical port pair %s does not exist",
                              port_pair)
                    return False
                port_pair_uuid_list.append(port_pair_uuid)
            txn.add(self._ovn.set_lport_pair_group(
                lport_pair_group_name=lport_pair_group_name,
                port_pairs=port_pair_uuid_list))
        return True

    def _create_ovn_port_pair_group(self, context, port_chain,
                                    port_pair_groups):
        status = True

        with self._ovn.transaction(check_error=True) as txn:
            lport_chain_name = self._sfc_name(port_chain)
            for group in port_pair_groups:
                lport_pair_group_name = self._sfc_name(group['id'])
                port_pairs = group['port_pairs']
                # Insert Ports Pair into OVN
                #
                port_pair_uuid_list = []
                for port_pair in port_pairs:
                    lport_pair_name = self._sfc_name(port_pair['id'])
                    port_pair_uuid = self._get_port_pair_uuid(lport_pair_name)
                    if port_pair_uuid is None:
                        LOG.error("Logical port pair %s does not exist",
                                  port_pair['id'])
                        return False
                    port_pair_uuid_list.append(port_pair_uuid)
                txn.add(self._ovn.create_lport_pair_group(
                        lport_pair_group_name=lport_pair_group_name,
                        lport_chain_name=lport_chain_name,
                        port_pairs=port_pair_uuid_list))

        return status

    def _create_ovn_flow_classifier(self, context, port_chain,
                                    flow_classifiers):
        status = True

        lport_chain_name = self._sfc_name(port_chain)
        with self._ovn.transaction(check_error=True) as txn:
            for flow_classifier in flow_classifiers:
                flow_classifier_name = self._sfc_name(flow_classifier['id'])
                lport_uuid = self._check_logical_port_exist(
                    flow_classifier['logical_source_port'])
                if lport_uuid is None:
                    LOG.error("Logical port %s does not exist",
                              flow_classifier['logical_source_port'])
                    return False
                flow_classifier['logical_source_port'] = lport_uuid
                lport_uuid = self._check_logical_port_exist(
                    flow_classifier['logical_destination_port'])
                if lport_uuid is None:
                    LOG.error("Logical port %s does not exist",
                              flow_classifier['logical_destination_port'])
                    return False
                flow_classifier['logical_destination_port'] = lport_uuid
                # Remove the flow classifier parameters not support in ovn
                flow_classifier.pop('id')
                flow_classifier.pop('description')
                flow_classifier.pop('l7_parameters')
                flow_classifier.pop('name')
                flow_classifier.pop('tenant_id')
                txn.add(self._ovn.create_lflow_classifier(
                    lport_chain_name=lport_chain_name,
                    lflow_classifier_name=flow_classifier_name,
                    **flow_classifier))
        return status

    def _create_ovn_sfc_about_logical_switch(self, context, sfc_instance):
        status = True

        with self._ovn.transaction(check_error=True) as txn:
            #
            # Insert Port Chain into OVN
            #
            lport_chain_name = self._sfc_name(sfc_instance['id'])
            for flow_classifier in sfc_instance['flow_classifier']:
                lswitch_name = self._check_lswitch_exists(
                    context, flow_classifier['logical_source_port'])
                if lswitch_name is None:
                    LOG.error("Logical switch does not exist for "
                              "flow_classifier logical source port: %s" %
                              flow_classifier['logical_source_port'])
                    return False
                txn.add(self._ovn.create_lport_chain(
                    lswitch_name=lswitch_name,
                    lport_chain_name=lport_chain_name))

            port_pair_groups = sfc_instance['port_pair_groups']
            for group in port_pair_groups:
                port_pairs = group['port_pairs']
                # Insert Ports Pair into OVN
                #
                for port_pair in port_pairs:
                    lport_pair_name = self._sfc_name(port_pair['id'])
                    lswitch_name = self._check_lswitch_exists(
                        context, port_pair['ingress'])
                    if lswitch_name is None:
                        LOG.error("Logical switch does not exist for "
                                  "port pair %s ingress port" %
                                  port_pair['id'])
                        return False
                    inport_uuid = self._check_logical_port_exist(
                        port_pair['ingress'])
                    outport_uuid = self._check_logical_port_exist(
                        port_pair['egress'])
                    if inport_uuid is None or outport_uuid is None:
                        LOG.error("Logical ingress port or egress port does "
                                  "not exist for port pair %s",
                                  port_pair['id'])
                        return False
                    txn.add(self._ovn.create_lport_pair(
                            lport_pair_name=lport_pair_name,
                            lswitch_name=lswitch_name,
                            outport=outport_uuid,
                            inport=inport_uuid))
        return status

    def _delete_ovn_sfc(self, context, sfc_instance):
        status = True
        lport_chain_name = self._sfc_name(sfc_instance['id'])
        with self._ovn.transaction(check_error=True) as txn:
            #
            # delete port pair from logcial switch
            #
            port_pair_groups = sfc_instance['port_pair_groups']
            for group in port_pair_groups:
                port_pairs = group['port_pairs']
                lport_pair_group_name = self._sfc_name(group['id'])
                for port_pair in port_pairs:
                    lport_pair_name = self._sfc_name(port_pair['id'])
                    lswitch_name = self._check_lswitch_exists(
                        context, port_pair['ingress'])
                    if lswitch_name is None:
                        LOG.error("Logical switch does not exist for "
                                  "port pair %s ingress port" %
                                  port_pair['id'])
                        return False
                    txn.add(self._ovn.delete_lport_pair(
                            lport_pair_name=lport_pair_name,
                            lswitch=lswitch_name,
                            lport_pair_group_name=lport_pair_group_name))
            #
            # delete port chain from OVN
            #
            for flow_classifier in sfc_instance['flow_classifier']:
                lswitch_name = self._check_lswitch_exists(
                    context, flow_classifier['logical_source_port'])
                if lswitch_name is None:
                    LOG.error("Logical switch does not exist for "
                              "flow_classifier logical source port: %s" %
                              flow_classifier['logical_source_port'])
                    return False
                txn.add(self._ovn.delete_lport_chain(
                    lswitch_name=lswitch_name,
                    lport_chain_name=lport_chain_name))
        return status
