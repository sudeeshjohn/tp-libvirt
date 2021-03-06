import logging
import os
import random
import time

from avocado.utils import process
from avocado.utils import cpu

from virttest import virsh
from virttest import virt_vm
from virttest import data_dir
from virttest import utils_test
from virttest import libvirt_xml
from virttest.utils_test import libvirt
from virttest import utils_hotplug


def run(test, params, env):
    """
    Different vcpupin scenario tests
    1) prepare the guest with given topology, memory and if any devices
    2) Start and login to the guest, check for cpu, memory
    3) Do different combinations of vcpupin and in parallel run stress
       if given
    4) Do a optional step based on config
    5) Check guest and host functional

    :param test: QEMU test object
    :param params: Dictionary with the test parameters
    :param env: Dictionary with test environment.
    """

    def set_condition(vm_name, condn, reset=False, guestbt=None):
        """
        Set domain to given state or reset it.
        """
        bt = None
        if not reset:
            if condn == "stress":
                bt = utils_test.run_avocado_bg(vm, params, test)
                if not bt:
                    test.cancel("guest stress failed to start")
                # Allow stress to start
                time.sleep(condn_sleep_sec)
                return bt
            elif condn in ["save", "managedsave"]:
                # No action
                pass
            elif condn == "suspend":
                result = virsh.suspend(vm_name, ignore_status=True, debug=True)
                libvirt.check_exit_status(result)
            elif condn == "hotplug":
                result = virsh.setvcpus(vm_name, max_vcpu, "--live",
                                        ignore_status=True, debug=True)
                libvirt.check_exit_status(result)
                exp_vcpu = {'max_config': max_vcpu, 'max_live': max_vcpu,
                            'cur_config': current_vcpu, 'cur_live': max_vcpu,
                            'guest_live': max_vcpu}
                result = utils_hotplug.check_vcpu_value(vm, exp_vcpu,
                                                        option="--live")
            elif condn == "host_smt":
                if cpu.get_cpu_arch() == 'power9':
                    result = process.run("ppc64_cpu --smt=4", shell=True)
                else:
                    test.cancel("Host SMT changes not allowed during guest live")
            else:
                logging.debug("No operation for the domain")

        else:
            if condn == "save":
                save_file = os.path.join(data_dir.get_tmp_dir(), vm_name + ".save")
                result = virsh.save(vm_name, save_file,
                                    ignore_status=True, debug=True)
                libvirt.check_exit_status(result)
                time.sleep(condn_sleep_sec)
                if os.path.exists(save_file):
                    result = virsh.restore(save_file, ignore_status=True,
                                           debug=True)
                    libvirt.check_exit_status(result)
                    os.remove(save_file)
                else:
                    test.error("No save file for domain restore")
            elif condn == "managedsave":
                result = virsh.managedsave(vm_name,
                                           ignore_status=True, debug=True)
                libvirt.check_exit_status(result)
                time.sleep(condn_sleep_sec)
                result = virsh.start(vm_name, ignore_status=True, debug=True)
                libvirt.check_exit_status(result)
            elif condn == "suspend":
                result = virsh.resume(vm_name, ignore_status=True, debug=True)
                libvirt.check_exit_status(result)
            elif condn == "stress":
                guestbt.join(ignore_status=True)
            elif condn == "hotplug":
                result = virsh.setvcpus(vm_name, current_vcpu, "--live",
                                        ignore_status=True, debug=True)
                libvirt.check_exit_status(result)
                exp_vcpu = {'max_config': max_vcpu, 'max_live': current_vcpu,
                            'cur_config': current_vcpu, 'cur_live': current_vcpu,
                            'guest_live': current_vcpu}
                result = utils_hotplug.check_vcpu_value(vm, exp_vcpu,
                                                        option="--live")
            elif condn == "host_smt":
                result = process.run("ppc64_cpu --smt=2", shell=True)
                # Change back the host smt
                result = process.run("ppc64_cpu --smt=4", shell=True)
            else:
                logging.debug("No need recover the domain")
        return bt

    vm_name = params.get("main_vm")
    max_vcpu = int(params.get("max_vcpu", 2))
    current_vcpu = int(params.get("current_vcpu", 1))
    vm_cores = int(params.get("limit_vcpu_cores", 2))
    vm_threads = int(params.get("limit_vcpu_threads", 1))
    vm_sockets = int(params.get("limit_vcpu_sockets", 1))
    vm = env.get_vm(vm_name)
    condition = params.get("condn", "")
    condn_sleep_sec = int(params.get("condn_sleep_sec", 30))
    pintype = params.get("pintype", "random")
    emulatorpin = "yes" == params.get("emulatorpin", "no")
    iterations = int(params.get("itr", 1))
    vmxml = libvirt_xml.VMXML.new_from_inactive_dumpxml(vm_name)
    org_xml = vmxml.copy()
    # Destroy the vm
    vm.destroy()
    try:
        # Set vcpu and topology
        libvirt_xml.VMXML.set_vm_vcpus(vm_name, max_vcpu, current_vcpu,
                                       vm_sockets, vm_cores, vm_threads)
        try:
            vm.start()
        except virt_vm.VMStartError, detail:
            test.fail("%s" % detail)

        cpus_list = cpu.cpu_online_list()
        cpucount = vm.get_cpu_count()
        if cpucount != current_vcpu:
            test.fail("Incorrect initial guest vcpu\nExpected:%s Actual:%s",
                      cpucount, current_vcpu)

        if condition:
            condn_result = set_condition(vm_name, condition)

        # Action:
        for _ in range(iterations):
            if emulatorpin:
                # To make sure cpu to be offline during host_smt
                hostcpu = cpus_list[-1]
                result = virsh.emulatorpin(vm_name, hostcpu, debug=True)
                libvirt.check_exit_status(result)
            for vcpu in range(max_vcpu):
                if pintype == "random":
                    hostcpu = random.choice(cpus_list)
                if pintype == "sequential":
                    hostcpu = cpus_list[vcpu % len(cpus_list)]
                result = virsh.vcpupin(vm_name, vcpu, hostcpu,
                                       ignore_status=True, debug=True)
                libvirt.check_exit_status(result)

        if condition:
            set_condition(vm_name, condition, reset=True, guestbt=condn_result)

        # Check for guest functional
        cpucount = vm.get_cpu_count()
        if cpucount != current_vcpu:
            test.fail("Incorrect final guest vcpu\nExpected:%s Actual:%s",
                      cpucount, current_vcpu)
    finally:
        org_xml.sync()
