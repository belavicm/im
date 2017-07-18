#! /usr/bin/env python
# IM - Infrastructure Manager
# Copyright (C) 2011 - GRyCAP - Universitat Politecnica de Valencia
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from optparse import OptionParser
import re
import time
import logging
import logging.config
import sys
import subprocess
import os
import getpass
import json
import yaml
try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO
import socket
from multiprocessing import Queue

from IM.SSH import SSH, AuthenticationException


class CtxtAgent():

    SSH_WAIT_TIMEOUT = 600
    # This value enables to retry the playbooks to avoid some SSH connectivity problems
    # The minimum value is 1. This value will be in the data file generated by
    # the ConfManager
    PLAYBOOK_RETRIES = 1

    INTERNAL_PLAYBOOK_RETRIES = 1

    PK_FILE = "/tmp/ansible_key"

    CONF_DATA_FILENAME = None
    VM_CONF_DATA_FILENAME = None

    logger = None

    @staticmethod
    def wait_winrm_access(vm):
        """
         Test the WinRM access to the VM
        """
        delay = 10
        wait = 0
        last_tested_private = False
        while wait < CtxtAgent.SSH_WAIT_TIMEOUT:
            if 'ctxt_ip' in vm:
                vm_ip = vm['ctxt_ip']
            elif 'private_ip' in vm and not last_tested_private:
                # First test the private one
                vm_ip = vm['private_ip']
                last_tested_private = True
            else:
                vm_ip = vm['ip']
                last_tested_private = False
            try:
                CtxtAgent.logger.debug("Testing WinRM access to VM: " + vm_ip)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                result = sock.connect_ex((vm_ip, vm['remote_port']))
            except:
                CtxtAgent.logger.exception("Error connecting with WinRM with: " + vm_ip)
                result = -1

            if result == 0:
                vm['ctxt_ip'] = vm_ip
                return True
            else:
                wait += delay
                time.sleep(delay)

    @staticmethod
    def wait_ssh_access(vm, delay=10, max_wait=None, quiet=False):
        """
         Test the SSH access to the VM
         return: init, new or pk_file or None if it fails
        """
        if not max_wait:
            max_wait = CtxtAgent.SSH_WAIT_TIMEOUT
        wait = 0
        success = False
        res = None
        last_tested_private = False
        while wait < max_wait:
            if 'ctxt_ip' in vm:
                vm_ip = vm['ctxt_ip']
            elif 'private_ip' in vm and not last_tested_private:
                # First test the private one
                vm_ip = vm['private_ip']
                last_tested_private = True
            else:
                vm_ip = vm['ip']
                last_tested_private = False
            if not quiet:
                CtxtAgent.logger.debug("Testing SSH access to VM: %s:%s" % (vm_ip, vm['remote_port']))
            wait += delay
            try:
                ssh_client = SSH(vm_ip, vm['user'], vm['passwd'], vm[
                                 'private_key'], vm['remote_port'])
                success = ssh_client.test_connectivity(delay)
                res = 'init'
            except AuthenticationException:
                try_ansible_key = True
                if 'new_passwd' in vm:
                    try_ansible_key = False
                    # If the process of changing credentials has finished in the
                    # VM, we must use the new ones
                    if not quiet:
                        CtxtAgent.logger.debug("Error connecting with SSH with initial credentials with: " +
                                               vm_ip + ". Try to use new ones.")
                    try:
                        ssh_client = SSH(vm_ip, vm['user'], vm['new_passwd'], vm[
                                         'private_key'], vm['remote_port'])
                        success = ssh_client.test_connectivity()
                        res = "new"
                    except AuthenticationException:
                        try_ansible_key = True

                if try_ansible_key:
                    # In some very special cases the last two cases fail, so check
                    # if the ansible key works
                    if not quiet:
                        CtxtAgent.logger.debug("Error connecting with SSH with initial credentials with: " +
                                               vm_ip + ". Try to ansible_key.")
                    try:
                        ssh_client = SSH(vm_ip, vm['user'], None, CtxtAgent.PK_FILE, vm['remote_port'])
                        success = ssh_client.test_connectivity()
                        res = 'pk_file'
                    except:
                        CtxtAgent.logger.exception("Error connecting with SSH with: " + vm_ip)
                        success = False

            if success:
                vm['ctxt_ip'] = vm_ip
                return res
            else:
                time.sleep(delay)

        return None

    @staticmethod
    def run_command(command, timeout=None, poll_delay=5):
        """
         Function to run a command
        """
        try:
            p = subprocess.Popen(command, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, shell=True)

            if timeout is not None:
                wait = 0
                while p.poll() is None and wait < timeout:
                    time.sleep(poll_delay)
                    wait += poll_delay

                if p.poll() is None:
                    p.kill()
                    return "TIMEOUT"

            (out, err) = p.communicate()

            if p.returncode != 0:
                return "ERROR: " + err + out
            else:
                return out
        except Exception as ex:
            return "ERROR: Exception msg: " + str(ex)

    @staticmethod
    def wait_thread(thread_data, general_conf_data, copy, output=None, poll_delay=1, copy_step=10):
        """
         Wait for a thread to finish
        """
        thread, result = thread_data
        if not copy:
            thread.join()
        else:
            vm_dir = os.path.abspath(os.path.dirname(CtxtAgent.VM_CONF_DATA_FILENAME))
            ssh_master = CtxtAgent.get_master_ssh(general_conf_data)
            cont = 0
            while thread.is_alive():
                cont += 1
                time.sleep(poll_delay)
                if cont % copy_step == 0:
                    try:
                        ssh_master.sftp_put(vm_dir + "/ctxt_agent.log", vm_dir + "/ctxt_agent.log")
                    except:
                        CtxtAgent.logger.exception("Error putting %s file" % (vm_dir + "/ctxt_agent.log"))

        try:
            _, (return_code, hosts_with_errors), _ = result.get(timeout=60)
        except:
            CtxtAgent.logger.exception('Error getting ansible results.')
            return_code = -1
            hosts_with_errors = []

        if output:
            if return_code == 0:
                CtxtAgent.logger.info(output)
            else:
                CtxtAgent.logger.error(output)

        return (return_code == 0, hosts_with_errors)

    @staticmethod
    def wait_remote(data, poll_delay=2, ssh_step=10):
        ssh_client, pid = data
        if not pid:
            return False
        exit_status = 0
        vm_dir = os.path.abspath(os.path.dirname(CtxtAgent.VM_CONF_DATA_FILENAME))
        cont = 0
        while exit_status == 0:
            cont += 1
            # Only check the process status every ssh_step or if the out file exists
            if cont % ssh_step == 0 or os.path.exists(vm_dir + "/ctxt_agent.out"):
                CtxtAgent.logger.debug("Check status of remote process: %s" % pid)
                (_, _, exit_status) = ssh_client.execute("ps " + str(pid))
            if exit_status == 0:
                time.sleep(poll_delay)

        if os.path.exists(vm_dir + "/ctxt_agent.out"):
            try:
                with open(vm_dir + "/ctxt_agent.out", "r") as f:
                    results = json.load(f)
                    return results["OK"]
            except:
                CtxtAgent.logger.exception("Error parsing %s." % (vm_dir + "/ctxt_agent.out"))
                return False
        else:
            CtxtAgent.logger.error("Error file %s does not exist." % (vm_dir + "/ctxt_agent.out"))
            return False

    @staticmethod
    def LaunchRemoteAgent(vm, vault_pass, pk_file, changed_pass_ok):
        CtxtAgent.logger.debug('Launch Ctxt agent on node: %s' % vm['ip'])

        ssh_client = CtxtAgent.get_ssh(vm, changed_pass_ok, pk_file)
        # copy the config file
        if not vm['master']:
            ssh_client.execute("mkdir -p %s" % os.path.dirname(CtxtAgent.VM_CONF_DATA_FILENAME))
            ssh_client.sftp_put(CtxtAgent.VM_CONF_DATA_FILENAME, CtxtAgent.VM_CONF_DATA_FILENAME)

        vault_export = ""
        if vault_pass:
            vault_export = "export VAULT_PASS='%s' && " % vault_pass
        pid = None
        vm_dir = os.path.abspath(os.path.dirname(CtxtAgent.VM_CONF_DATA_FILENAME))
        remote_dir = os.path.abspath(os.path.dirname(CtxtAgent.CONF_DATA_FILENAME))
        try:
            (pid, _, _) = ssh_client.execute(vault_export + "nohup python_ansible " + remote_dir + "/ctxt_agent_dist.py " +
                                             CtxtAgent.CONF_DATA_FILENAME + " " + CtxtAgent.VM_CONF_DATA_FILENAME +
                                             " 1 > " + vm_dir + "/stdout 2> " + vm_dir +
                                             "/stderr < /dev/null & echo -n $!")
        except:
            CtxtAgent.logger.exception('Error launch Ctxt agent on node: %s' % vm['ip'])
        return ssh_client, pid

    @staticmethod
    def set_ansible_connection_local(general_conf_data, vm):
        filename = general_conf_data['conf_dir'] + "/hosts"
        vm_id = vm['ip'] + "_" + str(vm['remote_port'])
        with open(filename) as f:
            inventoy_data = ""
            for line in f:
                if "ansible_connection=local" in line:
                    line = line.replace("ansible_connection=local", "")
                if vm_id in line:
                    line = line[:-1] + " ansible_connection=local\n"
                inventoy_data += line
        with open(filename, 'w+') as f:
            f.write(inventoy_data)

    @staticmethod
    def LaunchAnsiblePlaybook(output, remote_dir, playbook_file, vm, threads, inventory_file, pk_file,
                              retries, change_pass_ok, vault_pass):
        CtxtAgent.logger.debug('Call Ansible')

        extra_vars = {'IM_HOST': vm['ip'] + "_" + str(vm['remote_port'])}
        user = None
        if vm['os'] == "windows":
            gen_pk_file = None
            passwd = vm['passwd']
            if 'new_passwd' in vm and vm['new_passwd'] and change_pass_ok:
                passwd = vm['new_passwd']
        else:
            passwd = vm['passwd']
            if 'new_passwd' in vm and vm['new_passwd'] and change_pass_ok:
                passwd = vm['new_passwd']
            if pk_file:
                gen_pk_file = pk_file
            else:
                if vm['private_key'] and not vm['passwd']:
                    gen_pk_file = "/tmp/pk_" + vm['ip'] + ".pem"
                    pk_out = open(gen_pk_file, 'w')
                    pk_out.write(vm['private_key'])
                    pk_out.close()
                    os.chmod(gen_pk_file, 0o600)
                else:
                    gen_pk_file = None

        # Set local_tmp dir different for any VM
        os.environ['DEFAULT_LOCAL_TMP'] = remote_dir + "/.ansible_tmp"
        # it must be set before doing the import
        from IM.ansible_utils.ansible_launcher import AnsibleThread

        result = Queue()
        t = AnsibleThread(result, output, playbook_file, None, threads, gen_pk_file,
                          passwd, retries, inventory_file, user, vault_pass, extra_vars)
        t.start()
        return (t, result)

    @staticmethod
    def changeVMCredentials(vm, pk_file):
        if vm['os'] == "windows":
            if 'passwd' in vm and vm['passwd'] and 'new_passwd' in vm and vm['new_passwd']:
                try:
                    import winrm
                except:
                    CtxtAgent.logger.exception("Error importing winrm.")
                    return False
                try:
                    url = "https://" + vm['ip'] + ":5986"
                    s = winrm.Session(url, auth=(vm['user'], vm['passwd']), server_cert_validation='ignore')
                    r = s.run_cmd('net', ['user', vm['user'], vm['new_passwd']])

                    # this part of the code is never reached ...
                    if r.status_code == 0:
                        vm['passwd'] = vm['new_passwd']
                        return True
                    else:
                        CtxtAgent.logger.error(
                            "Error changing password to Windows VM: " + r.std_out)
                        return False
                except winrm.exceptions.AuthenticationError:
                    # if the password is correctly changed the command returns this
                    # error
                    try:
                        # let's check that the new password works
                        s = winrm.Session(url, auth=(vm['user'], vm['new_passwd']), server_cert_validation='ignore')
                        r = s.run_cmd('echo', ['OK'])
                        if r.status_code == 0:
                            vm['passwd'] = vm['new_passwd']
                            return True
                        else:
                            CtxtAgent.logger.error(
                                "Error changing password to Windows VM: " + r.std_out)
                            return False
                    except:
                        CtxtAgent.logger.exception(
                            "Error changing password to Windows VM: " + vm['ip'] + ".")
                        return False
                except:
                    CtxtAgent.logger.exception(
                        "Error changing password to Windows VM: " + vm['ip'] + ".")
                    return False
        else:  # Linux VMs
            # Check if we must change user credentials in the VM
            if 'passwd' in vm and vm['passwd'] and 'new_passwd' in vm and vm['new_passwd']:
                CtxtAgent.logger.info("Changing password to VM: " + vm['ip'])
                try:
                    ssh_client = CtxtAgent.get_ssh(vm, False, pk_file)

                    sudo_pass = ""
                    if ssh_client.password:
                        sudo_pass = "echo '" + ssh_client.password + "' | "
                    (out, err, code) = ssh_client.execute(sudo_pass + 'sudo -S bash -c \'echo "' +
                                                          vm['user'] + ':' + vm['new_passwd'] +
                                                          '" | /usr/sbin/chpasswd && echo "OK"\' 2> /dev/null')
                except:
                    CtxtAgent.logger.exception(
                        "Error changing password to VM: " + vm['ip'] + ".")
                    return False

                if code == 0:
                    vm['passwd'] = vm['new_passwd']
                    return True
                else:
                    CtxtAgent.logger.error("Error changing password to VM: " +
                                           vm['ip'] + ". " + out + err)
                    return False

            if 'new_public_key' in vm and vm['new_public_key'] and 'new_private_key' in vm and vm['new_private_key']:
                CtxtAgent.logger.info("Changing public key to VM: " + vm['ip'])
                try:
                    ssh_client = CtxtAgent.get_ssh(vm, False, pk_file)
                    (out, err, code) = ssh_client.execute_timeout('echo ' + vm['new_public_key'] +
                                                                  ' >> .ssh/authorized_keys', 5)
                except:
                    CtxtAgent.logger.exception(
                        "Error changing public key to VM: " + vm['ip'] + ".")
                    return False

                if code != 0:
                    CtxtAgent.logger.error("Error changing public key to VM:: " +
                                           vm['ip'] + ". " + out + err)
                    return False
                else:
                    vm['private_key'] = vm['new_private_key']
                    return True

        return False

    @staticmethod
    def removeRequiretty(vm, changed_pass, pk_file):
        if not vm['master']:
            CtxtAgent.logger.info("Removing requiretty to VM: " + vm['ip'])
            try:
                ssh_client = CtxtAgent.get_ssh(vm, changed_pass, pk_file)
                # Activate tty mode to avoid some problems with sudo in REL
                ssh_client.tty = True
                sudo_pass = ""
                if ssh_client.password:
                    sudo_pass = "echo '" + ssh_client.password + "' | "
                (stdout, stderr, code) = ssh_client.execute_timeout(
                    sudo_pass + "sudo -S sed -i 's/.*requiretty$/#Defaults requiretty/' /etc/sudoers", 5)
                CtxtAgent.logger.debug("OUT: " + stdout + stderr)
                return code == 0
            except:
                CtxtAgent.logger.exception("Error removing requiretty to VM: " + vm['ip'])
                return False
        else:
            return True

    @staticmethod
    def replace_vm_ip(vm_data):
        # Add the Ctxt IP with the one that is actually working
        # in the inventory and in the general info file
        with open(CtxtAgent.CONF_DATA_FILENAME) as f:
            general_conf_data = json.load(f)

        for vm in general_conf_data['vms']:
            if vm['id'] == vm_data['id']:
                vm['ctxt_ip'] = vm_data['ctxt_ip']

        with open(CtxtAgent.CONF_DATA_FILENAME, 'w+') as f:
            json.dump(general_conf_data, f, indent=2)

        # Now in the ansible inventory
        filename = general_conf_data['conf_dir'] + "/hosts"
        with open(filename) as f:
            inventoy_data = ""
            for line in f:
                line = re.sub(" ansible_host=%s " % vm_data['ip'],
                              " ansible_host=%s " % vm_data['ctxt_ip'], line)
                line = re.sub(" ansible_ssh_host=%s " % vm_data['ip'],
                              " ansible_ssh_host=%s " % vm_data['ctxt_ip'], line)
                inventoy_data += line

        with open(filename, 'w+') as f:
            f.write(inventoy_data)

    @staticmethod
    def get_master_ssh(general_conf_data):
        ctxt_vm = None
        for vm in general_conf_data['vms']:
            if vm['master']:
                ctxt_vm = vm
                break
        if not ctxt_vm:
            CtxtAgent.logger.error('Not VM master found to get ssh.')
            return None

        cred_used = CtxtAgent.wait_ssh_access(ctxt_vm, 2, 10, True)
        passwd = ctxt_vm['passwd']
        if cred_used == 'new':
            passwd = ctxt_vm['new_passwd']

        private_key = ctxt_vm['private_key']
        if cred_used == "pk_file":
            private_key = CtxtAgent.PK_FILE

        return SSH(ctxt_vm['ip'], ctxt_vm['user'], passwd, private_key, ctxt_vm['remote_port'])

    @staticmethod
    def get_ssh(ctxt_vm, changed_pass, pk_file):
        passwd = ctxt_vm['passwd']
        if 'new_passwd' in ctxt_vm and ctxt_vm['new_passwd'] and changed_pass:
            passwd = ctxt_vm['new_passwd']

        private_key = ctxt_vm['private_key']
        if pk_file:
            private_key = pk_file

        return SSH(ctxt_vm['ip'], ctxt_vm['user'], passwd, private_key, ctxt_vm['remote_port'])

    @staticmethod
    def contextualize_vm(general_conf_data, vm_conf_data, ctxt_vm, local):
        vault_pass = None
        if 'VAULT_PASS' in os.environ:
            vault_pass = os.environ['VAULT_PASS']

        res_data = {}
        CtxtAgent.logger.info('Generate and copy the ssh key')

        # If the file exists, do not create it again
        if not os.path.isfile(CtxtAgent.PK_FILE):
            out = CtxtAgent.run_command('ssh-keygen -t rsa -C ' + getpass.getuser() +
                                        ' -q -N "" -f ' + CtxtAgent.PK_FILE)
            CtxtAgent.logger.debug(out)

        if not ctxt_vm:
            CtxtAgent.logger.error("No VM to Contextualize!")
            res_data['OK'] = False
            return res_data

        for task in vm_conf_data['tasks']:
            task_ok = False
            num_retries = 0
            while not task_ok and num_retries < CtxtAgent.PLAYBOOK_RETRIES:
                num_retries += 1
                CtxtAgent.logger.info('Launch task: ' + task)
                if ctxt_vm['os'] == "windows":
                    # playbook = general_conf_data['conf_dir'] + "/" + task + "_task_all_win.yml"
                    playbook = general_conf_data['conf_dir'] + "/" + task + "_task.yml"
                else:
                    playbook = general_conf_data['conf_dir'] + "/" + task + "_task_all.yml"
                inventory_file = general_conf_data['conf_dir'] + "/hosts"

                ansible_thread = None
                remote_process = None
                if task == "install_ansible":
                    if ctxt_vm['os'] == "windows":
                        CtxtAgent.logger.info("Waiting WinRM access to VM: " + ctxt_vm['ip'])
                        cred_used = CtxtAgent.wait_winrm_access(ctxt_vm)
                        if not cred_used:
                            CtxtAgent.logger.error("Error Waiting access to VM: " + ctxt_vm['ip'])
                            res_data['SSH_WAIT'] = False
                            res_data['OK'] = False
                            return res_data
                        res_data['CHANGE_CREDS'] = CtxtAgent.changeVMCredentials(ctxt_vm, None)
                        CtxtAgent.logger.info("Windows VM do not install Ansible.")
                    elif not ctxt_vm['master']:
                        # This is always the fist step, so put the SSH test, the
                        # requiretty removal and change password here
                        CtxtAgent.logger.info("Waiting SSH access to VM: " + ctxt_vm['ip'])
                        cred_used = CtxtAgent.wait_ssh_access(ctxt_vm)

                        if not cred_used:
                            CtxtAgent.logger.error("Error Waiting access to VM: " + ctxt_vm['ip'])
                            res_data['SSH_WAIT'] = False
                            res_data['OK'] = False
                            return res_data
                        else:
                            res_data['SSH_WAIT'] = True
                            CtxtAgent.logger.info("Remote access to VM: " + ctxt_vm['ip'] + " Open!")

                        # The install_ansible task uses the credentials of VM stored in ctxt_vm
                        pk_file = None
                        changed_pass = False
                        if cred_used == "pk_file":
                            pk_file = CtxtAgent.PK_FILE
                        elif cred_used == "new":
                            changed_pass = True

                        success = CtxtAgent.removeRequiretty(ctxt_vm, changed_pass, pk_file)
                        if success:
                            CtxtAgent.logger.info("Requiretty successfully removed")
                        else:
                            CtxtAgent.logger.error("Error removing Requiretty")

                        # Check if we must change user credentials
                        # Do not change it on the master. It must be changed only by
                        # the ConfManager
                        if changed_pass:
                            change_creds = True
                        else:
                            change_creds = CtxtAgent.changeVMCredentials(ctxt_vm, pk_file)
                        res_data['CHANGE_CREDS'] = change_creds

                        playbook = general_conf_data['conf_dir'] + "/" + "conf-ansible.yml"
                        ansible_thread = CtxtAgent.LaunchAnsiblePlaybook(CtxtAgent.logger, vm_conf_data['remote_dir'],
                                                                         playbook, ctxt_vm, 2,
                                                                         inventory_file, pk_file,
                                                                         CtxtAgent.INTERNAL_PLAYBOOK_RETRIES,
                                                                         changed_pass, vault_pass)
                        # Copy dir general_conf_data['conf_dir'] to node
                        try:
                            ssh_client = CtxtAgent.get_ssh(ctxt_vm, changed_pass, pk_file)
                            _, _, code = ssh_client.execute("mkdir -p %s" % general_conf_data['conf_dir'])
                            if code != 0:
                                raise Exception("Error creating dir %s" % general_conf_data['conf_dir'])
                            ssh_client.sftp_put_dir(general_conf_data['conf_dir'], general_conf_data['conf_dir'])
                            # Put the correct permissions on the key file
                            ssh_client.sftp_chmod(CtxtAgent.PK_FILE, 0o600)
                        except:
                            CtxtAgent.logger.exception("Error copying playbooks to VM: " + ctxt_vm['ip'])
                            res_data['COPY_PLAYBOOKS'] = False
                            res_data['OK'] = False
                            return res_data
                    else:
                        CtxtAgent.logger.info("Master VM do not install Ansible.")
                elif task == "wait_all_ssh":
                    # Wait all the VMs to have remote access active
                    for vm in general_conf_data['vms']:
                        if vm['os'] == "windows":
                            CtxtAgent.logger.info("Waiting WinRM access to VM: " + vm['ip'])
                            cred_used = CtxtAgent.wait_winrm_access(vm)
                        else:
                            CtxtAgent.logger.info("Waiting SSH access to VM: " + vm['ip'])
                            cred_used = CtxtAgent.wait_ssh_access(vm)

                        if not cred_used:
                            CtxtAgent.logger.error("Error Waiting access to VM: " + vm['ip'])
                            res_data['SSH_WAIT'] = False
                            res_data['OK'] = False
                            return res_data
                        else:
                            res_data['SSH_WAIT'] = True
                            CtxtAgent.logger.info("Remote access to VM: " + vm['ip'] + " Open!")

                        # the IP has changed public for private
                        if 'ctxt_ip' in vm and vm['ctxt_ip'] != vm['ip']:
                            # update the ansible inventory
                            CtxtAgent.logger.info("Changing the IP %s for %s in config files." % (vm['ctxt_ip'],
                                                                                                  vm['ip']))
                            CtxtAgent.replace_vm_ip(vm)
                elif task == "basic":
                    if ctxt_vm['os'] == "windows":
                        CtxtAgent.logger.info("Waiting WinRM access to VM: " + ctxt_vm['ip'])
                        cred_used = CtxtAgent.wait_winrm_access(ctxt_vm)
                    else:
                        CtxtAgent.logger.info("Waiting SSH access to VM: " + ctxt_vm['ip'])
                        cred_used = CtxtAgent.wait_ssh_access(ctxt_vm)

                    if not cred_used:
                        CtxtAgent.logger.error("Error Waiting access to VM: " + ctxt_vm['ip'])
                        res_data['SSH_WAIT'] = False
                        res_data['OK'] = False
                        return res_data
                    else:
                        res_data['SSH_WAIT'] = True
                        CtxtAgent.logger.info("Remote access to VM: " + ctxt_vm['ip'] + " Open!")

                    # The basic task uses the credentials of VM stored in ctxt_vm
                    pk_file = None
                    changed_pass = False
                    if cred_used == "pk_file":
                        pk_file = CtxtAgent.PK_FILE
                    elif cred_used == "new":
                        changed_pass = True

                    if ctxt_vm['os'] != "windows":
                        if local:
                            # this step is not needed in windows systems
                            CtxtAgent.set_ansible_connection_local(general_conf_data, ctxt_vm)
                            ansible_thread = CtxtAgent.LaunchAnsiblePlaybook(CtxtAgent.logger,
                                                                             vm_conf_data['remote_dir'],
                                                                             playbook, ctxt_vm, 2,
                                                                             inventory_file, pk_file,
                                                                             CtxtAgent.INTERNAL_PLAYBOOK_RETRIES,
                                                                             changed_pass, vault_pass)
                        else:
                            remote_process = CtxtAgent.LaunchRemoteAgent(ctxt_vm, vault_pass, pk_file, changed_pass)
                else:
                    # in the other tasks pk_file can be used
                    if ctxt_vm['os'] != "windows" and not ctxt_vm['master'] and not local:
                        remote_process = CtxtAgent.LaunchRemoteAgent(ctxt_vm, vault_pass, CtxtAgent.PK_FILE,
                                                                     vm_conf_data['changed_pass'])
                    else:
                        if ctxt_vm['os'] != "windows":
                            CtxtAgent.set_ansible_connection_local(general_conf_data, ctxt_vm)
                        ansible_thread = CtxtAgent.LaunchAnsiblePlaybook(CtxtAgent.logger, vm_conf_data['remote_dir'],
                                                                         playbook, ctxt_vm, 2,
                                                                         inventory_file, CtxtAgent.PK_FILE,
                                                                         CtxtAgent.INTERNAL_PLAYBOOK_RETRIES,
                                                                         vm_conf_data['changed_pass'], vault_pass)

                if ansible_thread:
                    copy = True
                    if task == "install_ansible" or ctxt_vm['master'] or ctxt_vm['os'] == "windows":
                        copy = False
                    (task_ok, _) = CtxtAgent.wait_thread(ansible_thread, general_conf_data, copy)
                elif remote_process:
                    task_ok = CtxtAgent.wait_remote(remote_process)
                else:
                    task_ok = True
                if not task_ok:
                    CtxtAgent.logger.warn("ERROR executing task %s: (%s/%s)" %
                                          (task, num_retries, CtxtAgent.PLAYBOOK_RETRIES))
                else:
                    CtxtAgent.logger.info('Task %s finished successfully' % task)

            res_data[task] = task_ok
            if not task_ok:
                res_data['OK'] = False
                return res_data

        res_data['OK'] = True

        CtxtAgent.logger.info('Process finished')
        return res_data

    @staticmethod
    def run(general_conf_file, vm_conf_file, local):
        CtxtAgent.CONF_DATA_FILENAME = os.path.abspath(general_conf_file)
        CtxtAgent.VM_CONF_DATA_FILENAME = os.path.abspath(vm_conf_file)

        with open(CtxtAgent.CONF_DATA_FILENAME) as f:
            general_conf_data = json.load(f)
        with open(vm_conf_file) as f:
            vm_conf_data = json.load(f)

        ctxt_vm = None
        for vm in general_conf_data['vms']:
            if vm['id'] == vm_conf_data['id']:
                ctxt_vm = vm
                break

        if local or ctxt_vm['master'] or "install_ansible" in vm_conf_data['tasks'] or ctxt_vm['os'] == 'windows':
            log_file = vm_conf_data['remote_dir'] + "/ctxt_agent.log"
        else:
            log_file = vm_conf_data['remote_dir'] + "/ctxt_agentr.log"

        # Root logger: is used by paramiko
        logging.basicConfig(filename=log_file,
                            level=logging.WARNING,
                            # format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                            format='%(message)s',
                            datefmt='%m-%d-%Y %H:%M:%S')
        # ctxt_agent logger
        CtxtAgent.logger = logging.getLogger('ctxt_agent')
        CtxtAgent.logger.setLevel(logging.DEBUG)

        if 'playbook_retries' in general_conf_data:
            CtxtAgent.PLAYBOOK_RETRIES = general_conf_data['playbook_retries']

        CtxtAgent.PK_FILE = general_conf_data['conf_dir'] + "/" + "ansible_key"

        res_data = CtxtAgent.contextualize_vm(general_conf_data, vm_conf_data, ctxt_vm, local)

        if local or ctxt_vm['master'] or "install_ansible" in vm_conf_data['tasks'] or ctxt_vm['os'] == 'windows':
            ctxt_out = open(vm_conf_data['remote_dir'] + "/ctxt_agent.out", 'w+')
        else:
            ctxt_out = open(vm_conf_data['remote_dir'] + "/ctxt_agentr.out", 'w+')
        json.dump(res_data, ctxt_out, indent=2)
        ctxt_out.close()

        if local and not ctxt_vm['master'] and ctxt_vm['os'] != "windows":
            try:
                ssh_master = CtxtAgent.get_master_ssh(general_conf_data)
                if os.path.exists(vm_conf_data['remote_dir'] + "/ctxt_agent.log"):
                    ssh_master.sftp_put(vm_conf_data['remote_dir'] + "/ctxt_agent.log",
                                        vm_conf_data['remote_dir'] + "/ctxt_agent.log")
                    os.unlink(vm_conf_data['remote_dir'] + "/ctxt_agent.log")
                else:
                    CtxtAgent.logger.error("File %s does not exist" % vm_conf_data['remote_dir'] + "/ctxt_agent.log")
                    return False
                if os.path.exists(vm_conf_data['remote_dir'] + "/ctxt_agent.out"):
                    ssh_master.sftp_put(vm_conf_data['remote_dir'] + "/ctxt_agent.out",
                                        vm_conf_data['remote_dir'] + "/ctxt_agent.out")
                    os.unlink(vm_conf_data['remote_dir'] + "/ctxt_agent.out")
                else:
                    CtxtAgent.logger.error("File %s does not exist" % vm_conf_data['remote_dir'] + "/ctxt_agent.out")
                    return False
            except:
                CtxtAgent.logger.exception("Error copying back the results")
                return False

        return res_data['OK']

if __name__ == "__main__":
    parser = OptionParser(
        usage="%prog [general_input_file] [vm_input_file]", version="%prog 1.0")
    (options, args) = parser.parse_args()

    local = False
    if len(args) == 3:
        local = bool(int(args[2]))
    elif len(args) != 2:
        parser.error("Error: Incorrect parameters")

    if CtxtAgent.run(args[0], args[1], local):
        sys.exit(0)
    else:
        sys.exit(1)
