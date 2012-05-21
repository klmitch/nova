#!/usr/bin/env python
# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011 OpenStack LLC.
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
This script is used to configure openvswitch flows on XenServer hosts.
"""

import os
import simplejson as json
import sys

# This is written to Python 2.4, since that is what is available on XenServer
import netaddr
import novalib


OVS_OFCTL = '/usr/bin/ovs-ofctl'


class OvsFlow(object):
    def __init__(self, bridge, params):
        self.bridge = bridge
        self.params = params

    def set_param(self, key, value):
        self.params[key] = value

    def add(self, rule):
        novalib.execute(OVS_OFCTL, 'add-flow', self.bridge, rule % self.params)

    def clear_flows(self, ofport):
        novalib.execute(OVS_OFCTL, 'del-flows',
                                        self.bridge, "in_port=%s" % ofport)


def main(command, vif_raw, net_type):
    if command not in ('online', 'offline'):
        return

    vif_name, dom_id, vif_index = vif_raw.split('-')
    vif = "%s%s.%s" % (vif_name, dom_id, vif_index)

    bridge = novalib.execute_get_output('/usr/bin/ovs-vsctl',
                                                    'iface-to-br', vif)

    xsls = novalib.execute_get_output('/usr/bin/xenstore-ls',
                              '/local/domain/%s/vm-data/networking' % dom_id)
    macs = [line.split("=")[0].strip() for line in xsls.splitlines()]

    for mac in macs:
        xsread = novalib.execute_get_output('/usr/bin/xenstore-read',
                                    '/local/domain/%s/vm-data/networking/%s' %
                                    (dom_id, mac))
        data = json.loads(xsread)
        if data["label"] == "public":
            this_vif = "vif%s.0" % dom_id
            phys_dev = "eth0"
        else:
            this_vif = "vif%s.1" % dom_id
            phys_dev = "eth1"

        if vif == this_vif:
            vif_ofport = novalib.execute_get_output('/usr/bin/ovs-vsctl',
                                    'get', 'Interface', vif, 'ofport')
            phys_ofport = novalib.execute_get_output('/usr/bin/ovs-vsctl',
                                    'get', 'Interface', phys_dev, 'ofport')

            params = dict(VIF_NAME=vif,
                          MAC=data['mac'],
                          OF_PORT=vif_ofport,
                          PHYS_PORT=phys_ofport)

            ovs = OvsFlow(bridge, params)

            if command == 'offline':
                # I haven't found a way to clear only IPv4 or IPv6 rules.
                ovs.clear_flows(vif_ofport)

            if command == 'online':
                if net_type in ('ipv4', 'all') and 'ips' in data:
                    for ip4 in data['ips']:
                        ovs.set_param('IPV4_ADDR', ip4['ip'])
                        apply_ovs_ipv4_flows(ovs)
                if net_type in ('ipv6', 'all') and 'ip6s' in data:
                    for ip6 in data['ip6s']:
                        link_local = str(netaddr.EUI(data['mac']).eui64()\
                                        .ipv6_link_local())
                        ovs.set_param('IPV6_LINK_LOCAL_ADDR', link_local)
                        ovs.set_param('IPV6_GLOBAL_ADDR', ip6['ip'])
                        apply_ovs_ipv6_flows(ovs)


def apply_ovs_ipv4_flows(ovs):
    # Drop IP bcast/mcast -- matching the multicast bit in the dst mac
    # catches both traffic types
    ovs.add("priority=6,ip,in_port=%(OF_PORT)s,"
            "dl_dst=01:00:00:00:00:00/01:00:00:00:00:00,actions=drop")

    # When valid ARP traffic arrives from a vif, push it to virtual
    # port 9999 for further processing
    ovs.add("priority=4,arp,in_port=%(OF_PORT)s,dl_src=%(MAC)s,"
            "nw_src=%(IPV4_ADDR)s,arp_sha=%(MAC)s,actions=resubmit:9999")
    ovs.add("priority=4,arp,in_port=%(OF_PORT)s,dl_src=%(MAC)s,"
            "nw_src=0.0.0.0,arp_sha=%(MAC)s,actions=resubmit:9999")

    # When valid IP traffic arrives from a vif, push it to virtual
    # port 9999 for further processing
    ovs.add("priority=4,ip,in_port=%(OF_PORT)s,dl_src=%(MAC)s,"
            "nw_src=%(IPV4_ADDR)s,actions=resubmit:9999")

    # Pass ARP requests coming from any VMs on the local HV (port
    # 9999) or coming from external sources (PHYS_PORT) to the VM and
    # physical NIC if destination is local.  For ARP requests from
    # local VIFS, we send them to the physical NIC as well, since with
    # instances of shared ip groups, the active host for the
    # destination IP might be elsewhere...
    ovs.add("priority=3,arp,in_port=9999,nw_dst=%(IPV4_ADDR)s,"
            "actions=output:%(OF_PORT)s,output:%(PHYS_PORT)s")
    ovs.add("priority=3,arp,in_port=%(PHYS_PORT)s,nw_dst=%(IPV4_ADDR)s,"
            "actions=output:%(OF_PORT)s")

    # Pass ARP traffic originating from external sources the VM with
    # the matching IP address
    ovs.add("priority=3,arp,in_port=%(PHYS_PORT)s,nw_dst=%(IPV4_ADDR)s,"
            "actions=output:%(OF_PORT)s")

    # Pass ARP traffic from one VM (src mac already validated) to
    # another VM on the same HV
    ovs.add("priority=3,arp,in_port=9999,dl_dst=%(MAC)s,"
            "actions=output:%(OF_PORT)s")

    # Pass ARP replies coming from the external environment to the
    # target VM
    ovs.add("priority=3,arp,in_port=%(PHYS_PORT)s,dl_dst=%(MAC)s,"
            "actions=output:%(OF_PORT)s")

    # ALL IP traffic: Pass IP data coming from any VMs on the local HV
    # (port 9999) or coming from external sources (PHYS_PORT) to the
    # VM and physical NIC.  We output this to the physical NIC as
    # well, since with instances of shared ip groups, the active host
    # for the destination IP might be elsewhere...
    ovs.add("priority=3,ip,in_port=9999,dl_dst=%(MAC)s,"
            "nw_dst=%(IPV4_ADDR)s,actions=output:%(OF_PORT)s,"
            "output:%(PHYS_PORT)s")

    # Pass IP traffic from the external environment to the VM
    ovs.add("priority=3,ip,in_port=%(PHYS_PORT)s,dl_dst=%(MAC)s,"
            "nw_dst=%(IPV4_ADDR)s,actions=output:%(OF_PORT)s")

    # Send any local traffic to the physical NIC's OVS port for
    # physical network learning
    ovs.add("priority=2,in_port=9999,actions=output:%(PHYS_PORT)s")


def apply_ovs_ipv6_flows(ovs):
    # Drop flows for which the next header could not be determined
    ovs.add("priority=6,ipv6,nw_proto=59,actions=drop")

    # Drop ICMPv6 packets whose type is not present
    ovs.add("priority=6,icmp6,icmp_type=0,actions=drop")

    # Drop fragmented neighbor solicitation/advertisement to make sure
    # nothing slips by
    ovs.add("priority=6,icmp6,icmp_type=135,ip_frag=yes,actions=drop")
    ovs.add("priority=6,icmp6,icmp_type=136,ip_frag=yes,actions=drop")

    # Push neighbor solicitation/advertisement from approved mac/ip to
    # port 9999 for further processing.  Match on hop limit (TTL) of
    # 255 to make sure the packet is local.  Neighbor Solicitation
    ovs.add("priority=6,in_port=%(OF_PORT)s,dl_src=%(MAC)s,icmp6,"
            "ipv6_src=%(IPV6_LINK_LOCAL_ADDR)s,nw_ttl=255,icmp_type=135,"
            "nd_sll=%(MAC)s,actions=resubmit:9999")
    ovs.add("priority=6,in_port=%(OF_PORT)s,dl_src=%(MAC)s,icmp6,"
            "ipv6_src=%(IPV6_GLOBAL_ADDR)s,nw_ttl=255,icmp_type=135,"
            "nd_sll=%(MAC)s,actions=resubmit:9999")
    ovs.add("priority=6,in_port=%(OF_PORT)s,dl_src=%(MAC)s,icmp6,"
            "ipv6_src=::,nw_ttl=255,icmp_type=135,nd_sll=0:0:0:0:0:0,"
            "actions=resubmit:9999")

    # Neighbor Advertisement
    ovs.add("priority=6,in_port=%(OF_PORT)s,dl_src=%(MAC)s,icmp6,"
            "ipv6_src=%(IPV6_LINK_LOCAL_ADDR)s,nw_ttl=255,icmp_type=136,"
            "nd_target=%(IPV6_LINK_LOCAL_ADDR)s,actions=resubmit:9999")
    ovs.add("priority=6,in_port=%(OF_PORT)s,dl_src=%(MAC)s,icmp6,"
            "ipv6_src=%(IPV6_GLOBAL_ADDR)s,nw_ttl=255,icmp_type=136,"
            "nd_target=%(IPV6_GLOBAL_ADDR)s,actions=resubmit:9999")

    # Drop specific ICMPv6 types
    # Neighbor Solicitation from non-approved mac/ip
    ovs.add("priority=5,in_port=%(OF_PORT)s,icmp6,icmp_type=135,action=drop")
    # Neighbor Advertisement from non-approved mac/ip
    ovs.add("priority=5,in_port=%(OF_PORT)s,icmp6,icmp_type=136,action=drop")
    # Router Advertisement
    ovs.add("priority=5,in_port=%(OF_PORT)s,icmp6,icmp_type=134,action=drop")
    # Redirect Gateway
    ovs.add("priority=5,in_port=%(OF_PORT)s,icmp6,icmp_type=137,action=drop")
    # Mobile Prefix Solicitation
    ovs.add("priority=5,in_port=%(OF_PORT)s,icmp6,icmp_type=146,action=drop")
    # Mobile Prefix Advertisement
    ovs.add("priority=5,in_port=%(OF_PORT)s,icmp6,icmp_type=147,action=drop")
    # Multicast Router Advertisement
    ovs.add("priority=5,in_port=%(OF_PORT)s,icmp6,icmp_type=151,action=drop")
    # Multicast Router Solicitation
    ovs.add("priority=5,in_port=%(OF_PORT)s,icmp6,icmp_type=152,action=drop")
    # Multicast Router Termination
    ovs.add("priority=5,in_port=%(OF_PORT)s,icmp6,icmp_type=153,action=drop")

    # Push valid traffic to port 9999 for further processing
    ovs.add("priority=4,in_port=%(OF_PORT)s,dl_src=%(MAC)s,ipv6,"
            "ipv6_src=%(IPV6_GLOBAL_ADDR)s,actions=resubmit:9999")
    ovs.add("priority=4,in_port=%(OF_PORT)s,dl_src=%(MAC)s,ipv6,"
            "ipv6_src=%(IPV6_LINK_LOCAL_ADDR)s,actions=resubmit:9999")

    # Pass neighbor solicitation/advertisement requests originating
    # from any VMs on the local HV (port 9999) or coming from external
    # sources (PHYS_PORT) to the VM and physical NIC.  We output this
    # to the physical NIC as well, since with instances of shared ip
    # groups, the active host for the destination IP might be
    # elsewhere
    ovs.add("priority=3,in_port=9999,icmp6,nw_ttl=255,icmp_type=135,"
            "nd_target=%(IPV6_LINK_LOCAL_ADDR)s,actions=output:%(OF_PORT)s,"
            "output:%(PHYS_PORT)s")
    ovs.add("priority=3,in_port=9999,icmp6,nw_ttl=255,icmp_type=135,"
            "nd_target=%(IPV6_GLOBAL_ADDR)s,actions=output:%(OF_PORT)s,"
            "output:%(PHYS_PORT)s")
    ovs.add("priority=3,in_port=9999,icmp6,nw_ttl=255,icmp_type=136,"
            "nd_target=%(IPV6_LINK_LOCAL_ADDR)s,actions=output:%(OF_PORT)s,"
            "output:%(PHYS_PORT)s")
    ovs.add("priority=3,in_port=9999,icmp6,nw_ttl=255,icmp_type=136,"
            "nd_target=%(IPV6_GLOBAL_ADDR)s,actions=output:%(OF_PORT)s,"
            "output:%(PHYS_PORT)s")

    # Pass neighbor solicitation requests originating from external
    # sources to the VM with the matching IP address
    ovs.add("priority=3,in_port=%(PHYS_PORT)s,icmp6,nw_ttl=255,"
            "icmp_type=135,nd_target=%(IPV6_LINK_LOCAL_ADDR)s,"
            "actions=output:%(OF_PORT)s")
    ovs.add("priority=3,in_port=%(PHYS_PORT)s,icmp6,nw_ttl=255,"
            "icmp_type=135,nd_target=%(IPV6_GLOBAL_ADDR)s,"
            "actions=output:%(OF_PORT)s")

    # ALL IPv6 traffic: Pass IP data coming from any VMs on the local
    # HV (port 9999) or coming from external sources (PHYS_PORT) to
    # the VM and physical NIC We output this to the physical NIC as
    # well, since with instances of shared ip groups, the active host
    # for the destination IP might be elsewhere
    ovs.add("priority=3,in_port=9999,dl_dst=%(MAC)s,ipv6,"
            "ipv6_dst=%(IPV6_LINK_LOCAL_ADDR)s,actions=output:%(OF_PORT)s,"
            "output:%(PHYS_PORT)s")
    ovs.add("priority=3,in_port=9999,dl_dst=%(MAC)s,ipv6,"
            "ipv6_dst=%(IPV6_GLOBAL_ADDR)s,actions=output:%(OF_PORT)s,"
            "output:%(PHYS_PORT)s")

    # Pass IPv6 traffic from the external environment to the VM
    ovs.add("priority=3,in_port=%(PHYS_PORT)s,dl_dst=%(MAC)s,ipv6,"
            "ipv6_dst=%(IPV6_LINK_LOCAL_ADDR)s,actions=output:%(OF_PORT)s")
    ovs.add("priority=3,in_port=%(PHYS_PORT)s,dl_dst=%(MAC)s,ipv6,"
            "ipv6_dst=%(IPV6_GLOBAL_ADDR)s,actions=output:%(OF_PORT)s")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print "usage: %s [online|offline] vif-domid-idx [ipv4|ipv6|all] " % \
               os.path.basename(sys.argv[0])
        sys.exit(1)
    else:
        command, vif_raw, net_type = sys.argv[1:4]
        main(command, vif_raw, net_type)
