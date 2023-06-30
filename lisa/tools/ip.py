# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
from __future__ import annotations

import re
from typing import List, Optional, Type

from assertpy import assert_that

from lisa.executable import Tool
from lisa.tools import Cat
from lisa.tools.start_configuration import StartConfiguration
from lisa.tools.whoami import Whoami
from lisa.util import LisaException, find_groups_in_lines


class IpInfo:
    def __init__(self, nic_name: str, mac_addr: str, ip_addr: str):
        self.nic_name = nic_name
        self.mac_addr = mac_addr
        self.ip_addr = ip_addr


class Ip(Tool):
    # 00:0d:3a:c5:13:6f
    __mac_address_pattern = re.compile(
        "[0-9a-f]{2}([-:]?)[0-9a-f]{2}(\\1[0-9a-f]{2}){4}$", re.M
    )
    """
    3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state ...
        UP group default qlen 1000
        link/ether 00:22:48:79:69:b4 brd ff:ff:ff:ff:ff:ff
        inet 10.0.1.4/24 brd 10.0.1.255 scope global eth1
        valid_lft forever preferred_lft forever
        inet6 fe80::222:48ff:fe79:69b4/64 scope link
        valid_lft forever preferred_lft forever
    4: enP13530s1: <BROADCAST,MULTICAST,SLAVE,UP,LOWER_UP> mtu 1500 ...
        qdisc mq master eth0 state UP group default qlen 1000
        link/ether 00:22:48:79:6c:c2 brd ff:ff:ff:ff:ff:ff
    6: ib0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 2044 qdisc mq state UP ...
        link/infiniband 00:00:09:27:fe:80:00:00:00:00:00:00:00:15:5d:...
        inet 172.16.1.118/16 brd 172.16.255.255 scope global ib0
            valid_lft forever preferred_lft forever
        inet6 fe80::215:5dff:fd33:ff7f/64 scope link
            valid_lft forever preferred_lft forever
    5: ibP257s429327: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 2044 qdisc mq state UP group default qlen 256  # noqa: E501
        link/infiniband 00:00:01:49:fe:80:00:00:00:00:00:00:00:15:5d:ff:fd:33:ff:17 brd  # noqa: E501
        00:ff:ff:ff:ff:12:40:1b:80:0a:00:00:00:00:00:00:ff:ff:ff:ff
        altname ibP257p0s0
        inet 172.16.1.14/16 scope global ibP257s429327
        valid_lft forever preferred_lft forever
        inet6 fe80::215:5dff:fd33:ff17/64 scope link
        valid_lft forever preferred_lft forever
    """
    __ip_addr_show_regex = re.compile(
        (
            r"\d+: (?P<name>\w+): \<.+\> .+\n\s+link\/(?:ether|infiniband) "
            r"(?P<mac>[0-9a-z:]+) .+\n(?:(?:.+\n\s+|.*)altname \w+)?"
            r"(?:\s+inet (?P<ip_addr>[\d.]+)\/.*\n)?"
        )
    )
    # capturing from ip route show
    # ex:
    #    default via 10.57.0.1 dev eth0 proto dhcp src 10.57.0.4 metric 100
    __dev_regex = re.compile(
        r"default via\s+"  # looking for default route
        r"[0-9a-fA-F]{1,3}\."  # identify ip address
        r"[0-9a-fA-F]{1,3}\."
        r"[0-9a-fA-F]{1,3}\."
        r"[0-9a-fA-F]{1,3}"
        r"\s+dev\s+"  # looking for the device for the default route
        r"([a-zA-Z0-9]+)"  # capture device
    )

    @property
    def command(self) -> str:
        return "ip"

    def _check_exists(self) -> bool:
        return True

    @classmethod
    def _freebsd_tool(cls) -> Optional[Type[Tool]]:
        return IpFreebsd

    def _set_device_status(
        self, nic_name: str, status: str, persist: bool = False
    ) -> None:
        self.node.execute(
            f"ip link set {nic_name} {status}",
            shell=True,
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                f"Could not set {nic_name} to '{status}'"
            ),
        )
        if persist:
            self.node.tools[StartConfiguration].add_command(
                f"ip link set {nic_name} {status}"
            )

    def up(self, nic_name: str, persist: bool = False) -> None:
        self._set_device_status(nic_name, "up", persist=persist)

    def down(self, nic_name: str, persist: bool = False) -> None:
        self._set_device_status(nic_name, "down", persist=persist)

    def addr_flush(self, nic_name: str) -> None:
        self.node.execute(
            f"ip addr flush dev {nic_name}",
            shell=True,
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                f"Could not flush address for device {nic_name}"
            ),
        )

    def add_ipv4_address(self, nic_name: str, ip: str, persist: bool = True) -> None:
        self.run(
            f"addr add {ip} dev {nic_name}",
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                f"Could not add address to device {nic_name}"
            ),
        )
        if persist:
            self.node.tools[StartConfiguration].add_command(
                f"ip addr add {ip} dev {nic_name}"
            )

    def restart_device(
        self,
        nic_name: str,
        run_dhclient: bool = False,
        default_route: str = "",
    ) -> None:
        cmd = f"ip link set dev {nic_name} down;ip link set dev {nic_name} up "
        if run_dhclient:
            # if no ip address
            # firstly kill dhclient if it is running
            # then run dhclient to get ip address
            cmd += (
                f' && (ip addr show {nic_name} | grep "inet ") || '
                "(pidof dhclient && kill $(pidof dhclient) && "
                f"dhclient -r {nic_name}; dhclient {nic_name})"
            )
        if default_route:
            # need add wait 1 second, for some distro, e.g.
            # redhat rhel 7-lvm 7.8.2021051701
            # the ip route will be back after nic down and up for a while
            cmd += " && sleep 1 "
            # if no default route, add it back
            cmd += f" && ip route show | grep default || ip route add {default_route}"
        self.node.execute(
            cmd,
            shell=True,
            sudo=True,
            nohup=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                f"fail to restart [down then up] the nic {nic_name}"
            ),
        )

    def get_mtu(self, nic_name: str) -> int:
        cat = self.node.tools[Cat]
        return int(cat.read(f"/sys/class/net/{nic_name}/mtu", force_run=True))

    def set_mtu(self, nic_name: str, mtu: int) -> None:
        self.run(f"link set dev {nic_name} mtu {mtu}", force_run=True, sudo=True)
        new_mtu = self.get_mtu(nic_name=nic_name)
        assert_that(new_mtu).described_as("set mtu failed").is_equal_to(mtu)

    def set_mac_address(self, nic_name: str, mac_address: str) -> None:
        if not self.__mac_address_pattern.match(mac_address):
            raise LisaException(f"MAC address {mac_address} is invalid")
        self.down(nic_name)
        try:
            self.node.execute(
                f"/sbin/ip link set {nic_name} address {mac_address}",
                shell=True,
                sudo=True,
                expected_exit_code=0,
                expected_exit_code_failure_message=(
                    f"fail to set mac address {mac_address} for nic {nic_name}"
                ),
            )
        finally:
            self.up(nic_name)

    def nic_exists(self, nic_name: str) -> bool:
        result = self.run(f"link show {nic_name}", force_run=True, sudo=True)
        return not (
            (result.stderr and "not exist" in result.stderr)
            or (result.stdout and "not exist" in result.stdout)
        )

    def get_mac(self, nic_name: str) -> str:
        result = self.run(f"link show {nic_name}", force_run=True, sudo=True)
        matched = self.__ip_addr_show_regex.match(result.stdout)
        assert matched
        return matched.group("mac")

    def get_info(self, nic_name: Optional[str] = None) -> List[IpInfo]:
        command = "ip addr show"
        if nic_name:
            command += f" {nic_name}"
        result = self.node.execute(
            command,
            shell=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                f"Could not run {command} on node {self.node.name}"
            ),
        )
        entries = find_groups_in_lines(
            result.stdout, self.__ip_addr_show_regex, single_line=False
        )
        found_nics: List[IpInfo] = []
        for entry in entries:
            found_nics.append(
                IpInfo(
                    nic_name=entry["name"],
                    mac_addr=entry["mac"],
                    ip_addr=entry["ip_addr"],
                )
            )
        return found_nics

    def setup_bridge(self, name: str, ip: str) -> None:
        if self.nic_exists(name):
            self._log.debug(f"Bridge {name} already exists")
            return

        # create bridge
        self.run(
            f"link add {name} type bridge",
            force_run=True,
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=f"Could not create bridge {name}",
        )
        self.add_ipv4_address(name, ip)
        self.up(name)

    def set_bridge_configuration(self, name: str, key: str, value: str) -> None:
        self.run(
            f"link set dev {name} type bridge {key} {value}",
            force_run=True,
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                f"Could not set bridge {name} configuation: {key} {value}"
            ),
        )
        self.restart_device(name)

    def delete_interface(self, name: str) -> None:
        # check if the interface exists
        if not self.nic_exists(name):
            self._log.debug(f"Interface {name} does not exist")
            return

        # delete interface
        self.down(name)
        self.run(
            f"link delete {name}",
            force_run=True,
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=f"Could not delete interface {name}",
        )

    def set_master(self, child_interface: str, master_interface: str) -> None:
        self.run(
            f"link set dev {child_interface} master {master_interface}",
            force_run=True,
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                f"Could not set bridge {master_interface} as master for"
                f" {child_interface}"
            ),
        )

    def setup_tap(self, name: str, bridge: str) -> None:
        if self.nic_exists(name):
            self._log.debug(f"Tap {name} already exists")
            return

        # create tap
        user = self.node.tools[Whoami].run().stdout.strip()
        self.run(
            f"tuntap add {name} mode tap user {user} multi_queue",
            force_run=True,
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=f"Could not create tap {name}",
        )

        # add tap to bridge
        self.set_master(name, bridge)

        # start interface
        self.up(name)

    def get_ip_address(self, nic_name: str) -> str:
        result = self.run(f"addr show {nic_name}", force_run=True, sudo=True)
        matched = self.__ip_addr_show_regex.match(result.stdout)
        assert matched
        return matched.group("ip_addr")

    def get_default_route_info(self) -> tuple[str, str]:
        result = self.run("route", force_run=True, sudo=True)
        result.assert_exit_code()
        assert_that(result.stdout).is_not_empty()
        dev_match = self.__dev_regex.search(result.stdout)
        if not dev_match or not dev_match.groups():
            raise LisaException(
                "Could not locate default network interface"
                f" in output:\n{result.stdout}"
            )
        assert_that(dev_match.groups()).is_length(1)
        return dev_match.group(1), dev_match.group()

    def get_interface_list(self) -> list[str]:
        raise NotImplementedError()


class IpFreebsd(Ip):
    @property
    def command(self) -> str:
        return "ifconfig"

    def get_interface_list(self) -> list[str]:
        output = self.run(
            "-l",
            force_run=True,
            expected_exit_code=0,
            expected_exit_code_failure_message="Failed to get interface list",
        )
        return output.stdout.split()
