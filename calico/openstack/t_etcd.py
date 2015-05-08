# -*- coding: utf-8 -*-
#
# Copyright (c) 2015 Metaswitch Networks
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

# Etcd-based transport for the Calico/OpenStack Plugin.

# Standard Python library imports.
import etcd
import json
import re
import weakref

from collections import namedtuple

# OpenStack imports.
from oslo.config import cfg
from neutron.openstack.common import log

# Calico imports.
from calico.datamodel_v1 import (READY_KEY, CONFIG_DIR, TAGS_KEY_RE, HOST_DIR,
                                 key_for_endpoint, PROFILE_DIR, RULES_KEY_RE,
                                 key_for_profile, key_for_profile_rules,
                                 key_for_profile_tags, key_for_config)

# Register Calico-specific options.
calico_opts = [
    cfg.StrOpt('etcd_host', default='localhost',
               help="The hostname or IP of the etcd node/proxy"),
    cfg.IntOpt('etcd_port', default=4001,
               help="The port to use for the etcd node/proxy"),
]
cfg.CONF.register_opts(calico_opts, 'calico')

OPENSTACK_ENDPOINT_RE = re.compile(
    r'^' + HOST_DIR +
    r'/(?P<hostname>[^/]+)/.*openstack.*/endpoint/(?P<endpoint_id>[^/]+)')

json_decoder = json.JSONDecoder()

PERIODIC_RESYNC_INTERVAL_SECS = 30

LOG = log.getLogger(__name__)


# Objects for lightly wrapping etcd return values for use in the mechanism
# driver.
Endpoint = namedtuple('Endpoint', ['id', 'key', 'modified_index'])
Profile = namedtuple(
    'Profile', ['id', 'tags_modified_index', 'rules_modified_index']
)


class CalicoTransportEtcd(object):
    """Calico transport implementation based on etcd."""

    def __init__(self, driver):
        # Explicitly store the driver as a weakreference. This prevents
        # the reference loop between transport and driver keeping the objects
        # alive.
        self.driver = weakref.proxy(driver)

        # Prepare client for accessing etcd data.
        self.client = etcd.Client(host=cfg.CONF.calico.etcd_host,
                                  port=cfg.CONF.calico.etcd_port)

    def write_profile_to_etcd(self, profile):
        """
        Write a single security profile into etcd.
        """
        self.client.write(
            key_for_profile_rules(profile.id),
            json.dumps(profile_rules(profile))
        )
        self.client.write(
            key_for_profile_tags(profile.id),
            json.dumps(profile_tags(profile))
        )

    def endpoint_created(self, port):
        """
        Write appropriate data to etcd for an endpoint creation event.
        """
        # Write etcd data for the new endpoint.
        self.write_port_to_etcd(port)

    def endpoint_deleted(self, port):
        """
        Delete data from etcd for an endpoint deleted event.
        """
        # TODO: What do we do about profiles here?
        # Delete the etcd key for this endpoint.
        key = self.port_etcd_key(port)
        try:
            self.client.delete(key)
        except etcd.EtcdKeyNotFound:
            # Already gone, treat as success.
            LOG.debug("Key %s, which we were deleting, disappeared", key)

    def write_port_to_etcd(self, port):
        """
        Writes a given port dictionary to etcd.
        """
        data = port_etcd_data(port)
        self.client.write(port_etcd_key(port), json.dumps(data))

    def provide_felix_config(self):
        """Specify the prefix of the TAP interfaces that Felix should
        look for and work with.  This config setting does not have a
        default value, because different cloud systems will do
        different things.  Here we provide the prefix that Neutron
        uses.
        """
        # First read the config values, so as to avoid unnecessary
        # writes.
        prefix = None
        ready = None
        iface_pfx_key = key_for_config('InterfacePrefix')
        try:
            prefix = self.client.read(iface_pfx_key).value
            ready = self.client.read(READY_KEY).value
        except etcd.EtcdKeyNotFound:
            LOG.info('%s values are missing', CONFIG_DIR)

        # Now write the values that need writing.
        if prefix != 'tap':
            LOG.info('%s -> tap', iface_pfx_key)
            self.client.write(iface_pfx_key, 'tap')
        if ready != 'true':
            # TODO Set this flag only once we're really ready!
            LOG.info('%s -> true', READY_KEY)
            self.client.write(READY_KEY, 'true')

    def get_endpoints(self):
        """
        Gets information about every endpoint in etcd. Returns a generator of
        ``Endpoint`` objects.
        """
        result = self.client.read(HOST_DIR, recursive=True, timeout=5)
        nodes = result.children

        for node in nodes:
            match = OPENSTACK_ENDPOINT_RE.match(node.key)
            if match is None:
                continue

            endpoint_id = match.group('endpoint_id')
            yield Endpoint(endpoint_id, node.key, node.modifiedIndex)

    def atomic_delete_endpoint(self, endpoint):
        """
        Atomically delete a given endpoint. This method allows exceptions from
        etcd to bubble up.
        """
        self.client.delete(
            endpoint.key, prevIndex=endpoint.modified_index, timeout=5
        )

    def get_profiles(self):
        """
        Gets information about every profile in etcd. Returns a generator of
        ``Profile`` objects.
        """
        result = self.client.read(PROFILE_DIR, recursive=True, timeout=5)
        nodes = result.children

        tag_indices = {}
        rules_indices = {}

        for node in nodes:
            # All groups have both tags and rules, and we need the
            # modifiedIndex for both.
            tags_match = TAGS_KEY_RE.match(node.key)
            rules_match = RULES_KEY_RE.match(node.key)
            if tags_match:
                profile_id = tags_match.group('profile_id')
                tag_indices[profile_id] = node.modifiedIndex
            elif rules_match:
                profile_id = rules_match.group('profile_id')
                rules_indices[profile_id] = node.modifiedIndex
            else:
                continue

            # Check whether we have a complete set. If we do, remove them and
            # yield.
            if profile_id in tag_indices and profile_id in rules_indices:
                tag_modified = tag_indices.pop(profile_id)
                rules_modified = rules_indices.pop(profile_id)
                yield Profile(profile_id, tag_modified, rules_modified)

        # Quickly confirm that the tag and rule indices are empty (they should
        # be).
        if tag_indices or rules_indices:
            LOG.warning(
                "Imbalanced profile tags and rules! "
                "Extra tags %s, extra rules %s" % tag_indices, rules_indices
            )

    def atomic_delete_profile(self, profile):
        """
        Atomically delete a profile. This occurs in two stages: first the tag,
        then the rules. Abort if the first stage fails, as we can assume that
        someone else is trying to replace the profile.
        """
        self.client.delete(
            key_for_profile_tags(profile.id),
            prevIndex=profile.tags_modified_index,
            timeout=5
        )
        self.client.delete(
            key_for_profile_rules(profile.id),
            prevIndex=profile.rules_modified_index,
            timeout=5
        )


def _neutron_rule_to_etcd_rule(rule):
    """
    Translate a single Neutron rule dict to a single dict in our
    etcd format.
    """
    ethertype = rule['ethertype']
    etcd_rule = {}
    # Map the ethertype field from Neutron to etcd format.
    etcd_rule['ip_version'] = {'IPv4': 4,
                               'IPv6': 6}[ethertype]
    # Map the protocol field from Neutron to etcd format.
    if rule['protocol'] is None or rule['protocol'] == -1:
        pass
    elif rule['protocol'] == 'icmp':
        etcd_rule['protocol'] = {'IPv4': 'icmp',
                                 'IPv6': 'icmpv6'}[ethertype]
    else:
        etcd_rule['protocol'] = rule['protocol']

    # OpenStack (sometimes) represents 'any IP address' by setting
    # both 'remote_group_id' and 'remote_ip_prefix' to None.  We
    # translate that to an explicit 0.0.0.0/0 (for IPv4) or ::/0
    # (for IPv6).
    net = rule['remote_ip_prefix']
    if not (net or rule['remote_group_id']):
        net = {'IPv4': '0.0.0.0/0',
               'IPv6': '::/0'}[ethertype]
    port_spec = None
    if rule['protocol'] == 'icmp':
        # OpenStack stashes the ICMP match criteria in
        # port_range_min/max.
        icmp_type = rule['port_range_min']
        if icmp_type is not None and icmp_type != -1:
            etcd_rule['icmp_type'] = icmp_type
        icmp_code = rule['port_range_max']
        if icmp_code is not None and icmp_code != -1:
            etcd_rule['icmp_code'] = icmp_code
    else:
        # src/dst_ports is a list in which each entry can be a
        # single number, or a string describing a port range.
        if rule['port_range_min'] == -1:
            port_spec = ['1:65535']
        elif rule['port_range_min'] == rule['port_range_max']:
            if rule['port_range_min'] is not None:
                port_spec = [rule['port_range_min']]
        else:
            port_spec = ['%s:%s' % (rule['port_range_min'],
                                    rule['port_range_max'])]

    # Put it all together and add to either the inbound or the
    # outbound list.
    if rule['direction'] == 'ingress':
        if rule['remote_group_id'] is not None:
            etcd_rule['src_tag'] = rule['remote_group_id']
        if net is not None:
            etcd_rule['src_net'] = net
        if port_spec is not None:
            etcd_rule['dst_ports'] = port_spec
        LOG.info("=> Inbound Calico rule %s" % etcd_rule)
    else:
        if rule['remote_group_id'] is not None:
            etcd_rule['dst_tag'] = rule['remote_group_id']
        if net is not None:
            etcd_rule['dst_net'] = net
        if port_spec is not None:
            etcd_rule['dst_ports'] = port_spec
        LOG.info("=> Outbound Calico rule %s" % etcd_rule)

    return etcd_rule


def port_etcd_key(port):
    """
    Determine what the etcd key is for a port.
    """
    return key_for_endpoint(port['binding:host_id'],
                            "openstack",
                            port['device_id'],
                            port['id'])


def port_etcd_data(port):
    """
    Build the dictionary of data that will be written into etcd for a port.
    """
    # Construct the simpler port data.
    data = {'state': 'active' if port['admin_state_up'] else 'inactive',
            'name': port['interface_name'],
            'mac': port['mac_address'],
            'profile_id': port['security_groups']}

    # Collect IPv6 and IPv6 addresses.  On the way, also set the
    # corresponding gateway fields.  If there is more than one IPv4 or IPv6
    # gateway, the last one (in port['fixed_ips']) wins.
    ipv4_nets = []
    ipv6_nets = []
    for ip in port['fixed_ips']:
        if ':' in ip['ip_address']:
            ipv6_nets.append(ip['ip_address'] + '/128')
            if ip['gateway'] is not None:
                data['ipv6_gateway'] = ip['gateway']
        else:
            ipv4_nets.append(ip['ip_address'] + '/32')
            if ip['gateway'] is not None:
                data['ipv4_gateway'] = ip['gateway']
    data['ipv4_nets'] = ipv4_nets
    data['ipv6_nets'] = ipv6_nets

    # Return that data.
    return data


def profile_tags(profile):
    """
    Get the tags from a given security profile.
    """
    # TODO: This is going to be a no-op now, so consider removing it.
    return profile.id.split('_')


def profile_rules(profile):
    """
    Get a dictionary of profile rules, ready for writing into etcd as JSON.
    """
    inbound_rules = [
        _neutron_rule_to_etcd_rule(rule) for rule in profile.inbound_rules
    ]
    outbound_rules = [
        _neutron_rule_to_etcd_rule(rule) for rule in profile.outbound_rules
    ]

    return {'inbound_rules': inbound_rules, 'outbound_rules': outbound_rules}
