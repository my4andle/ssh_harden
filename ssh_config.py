#!/usr/bin/python3
"""
Usage:
  ssh_config.py -h | --help
  ssh_config.py (--rhost_file=<rhost_file> | --rhost=<rhost>) (--nonroot_user=<nonroot_user>) [--login_user=<login_user>] [--ssh_pubkey=<ssh_pubkey> | --ssh_new_key=<ssh_new_key>]

Options:
  --rhost_file=<rhost_file>         File containing a single ipv4 target per line
  --rhost=<rhost>                   A single ipv4 target
  --nonroot_user=<nonroot_user>     This is the user you will enable key based authentication for, if not existent we will create it
  --login_user=<login_user>         The OS user used to login and run configuration, [default: root]
  --ssh_pubkey=<ssh_pubkey>         Provide your own public key, make sure you have the private key
  --ssh_new_key=<ssh_new_key>       Path and name of the new ssh key to be generated by the script, [default: ./id_rsa]
"""
import os
import sys
import json
import random
import logging
import paramiko
import ipaddress
import subprocess
import concurrent.futures

from datetime import datetime
from docopt import docopt
from getpass import getpass


# TODO: check for ssh-keygen install
# TODO: enable toggle of ssh hardening for password auth disable
# Tested on kali/debian


class BashException(Exception):
    """Exception to throw when local and remote bash commands fail."""
    pass


class BashCommands:
    """Bash commands for remote ssh configuration."""
    def __init__(self, nonroot_user: str, pubkey: str):
        self.validateUser = "id -u {}".format(nonroot_user)
        self.createUser = "useradd -m {0} && usermod -aG sudo {0} && echo '{0} ALL=(ALL) ALL' >> /etc/sudoers".format(nonroot_user)
        self.chkSshDirMakeDir = "if [ ! -d /home/{0}/.ssh ];then mkdir /home/{0}/.ssh;fi".format(nonroot_user)
        self.chkAuthKeysFileMakeFile = "if [ ! -f /home/{0}/.ssh/authorization_keys ];then touch /home/{0}/.ssh/authorization_keys && chown {0}:{0} /home/{0}/.ssh/authorization_keys && chmod 600 /home/{0}/.ssh/authorization_keys ;fi".format(nonroot_user)
        self.addPubKeyToAuth = "echo '{}' >> /home/{}/.ssh/authorized_keys".format(pubkey, nonroot_user)
        self.disableRootSsh = "sed -i -e 's/PermitRootLogin yes/PermitRootLogin no/g' /etc/ssh/sshd_config"
        self.disablePassAuthSsh = "sed -i -e 's/PasswordAuthentication yes/PasswordAuthentication no/g' /etc/ssh/sshd_config"
        # ["/etc/init.d/ssh reload", "systemctl reload ssh", "/etc/init.d/sshd reload", "service ssh reload"]
        self.restartSshService = "service ssh restart"


class LocalKeygenCommands:
    """Local commands."""
    def __init__(self, abs_path_sshkey: str):
        self.sshKeygen = ["ssh-keygen", "-t", "rsa", "-b", "2048", "-N", '', "-f", "{}".format(abs_path_sshkey)]
        self.sshKeyChmod = ["chmod", "400", "{}".format(abs_path_sshkey)]


def password_prompt(user: str):
    """Logic to ask and validate password prior to code execution."""
    logging.info("[!] Entering password_prompt")
    while True:
        password1 = getpass("[!] Enter password for {}: ".format(user))
        password2 = getpass("[!] Retype password to confirm: ")

        if password1 == password2:
            logging.info("[+] Password match continuing with procedure")
            return password1
        else:
            logging.info("[-] Password mismatch try again please, or ctrl+c to exit")
            pass

def validate_ipv4(ip: str):
    """Validate an IPv4 address."""
    logging.info("[!] Entering validate_IPv4: {}".format(ip))
    try:
        ipaddress.ip_address(ip)
        logging.info("[+] IP valid: {}".format(ip))
        return ip
    except Exception as ex:
        logging.info("[-] IP invalid: {}".format(ip))
        return False

def generate_list_from_file(data_file) -> set:
    """Convert rhosts file to list."""
    logging.info("[!] Entering generate_list_from_file: {}".format(data_file))
    data_list = []
    with open(data_file, 'r') as my_file:
        for line in my_file:
            ip = line.strip('\n').strip(' ')
            if validate_ipv4(ip):
                data_list.append(ip)
    return set(data_list)

def execute_local_commands(cmd: str):
    """Execute local OS commands."""
    logging.info("[!] Entering execute_local_commands: {}".format(cmd))
    cmd_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = cmd_proc.communicate()
    if err:
        raise BashException("Bash command {} failed with error {} on local system".format(cmd, err))

def read_pubkey_file(file: str) -> str:
    """Read a public ssh key file and return contents."""
    logging.info("[!] Entering read_file for: {}".format(file))
    with open(file, "r") as myfile:
        for line in myfile:
            if "ssh-rsa" in line:
                return line.strip()
            else:
                raise BashException("Failed to read the public key file or the file does not contain a public ssh key: {}".format(file))

def login_ssh(target, username, password, port: int=22, timeout: int=5) -> dict:
    """Single SSH login attempt."""
    logging.info("[!] Entering login_ssh: {}".format(target))
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.WarningPolicy)
    client.connect(
        hostname=target, 
        port=port, 
        username=username, 
        password=password, 
        timeout=timeout,
        auth_timeout=timeout,
        banner_timeout=timeout,
        )
    logging.info("[+] Login success: {}".format(target))
    return client

def bash_validate_nonroot_user(cmd: str, user: str, ssh_client: object) -> bool:
    """Validate the userid is valid."""
    logging.info("[!] Entering bash_validate_nonroot_user: {}".format(user))
    try:
        stdin, stdout,stderr = ssh_client.exec_command(cmd)
        error = stderr.read()
        if error:
            logging.info("[-] {} failed with error: {}".format(cmd, error))
            return False
        else:
            result = stdout.read()
            int(result.decode('utf-8'))
            return True
    except ValueError:
        return False

def execute_bash(cmd: str, ssh_client: object, target: str):
    """Execute bash commands."""
    logging.info("[!] Executing {} against target {}".format(cmd, target))
    stdin, stdout,stderr = ssh_client.exec_command(cmd)
    error = stderr.read()
    if error:
        raise BashException("[-] Bash command {} failed with error {} for target {}".format(cmd, error, target))

def configure_target(cmds, target: str, login_user: str, password: str, nonroot_user: str, pubkey: str):
    """Configure the target for non root ssh key based authentication."""
    logging.info("[!] Entering configure_target: {}".format(target))
    try:
        ssh_client = login_ssh(target, login_user, password)
        if not bash_validate_nonroot_user(cmd=cmds.validateUser, user=nonroot_user, ssh_client=ssh_client):
            execute_bash(cmd=cmds.createUser, ssh_client=ssh_client, target=target)
        execute_bash(cmd=cmds.chkSshDirMakeDir, ssh_client=ssh_client, target=target)
        execute_bash(cmd=cmds.chkAuthKeysFileMakeFile, ssh_client=ssh_client, target=target)
        execute_bash(cmd=cmds.addPubKeyToAuth, ssh_client=ssh_client, target=target)
        execute_bash(cmd=cmds.disableRootSsh, ssh_client=ssh_client, target=target)
        execute_bash(cmd=cmds.disablePassAuthSsh, ssh_client=ssh_client, target=target)
        execute_bash(cmd=cmds.restartSshService, ssh_client=ssh_client, target=target)
        return {"Target": target, "Status": "Succeeded"}
    except Exception as ex:
        return {"Target": target, "Status": "Failed"}

def configure_target_concurrent(targets: list, login_user: str, password: str, nonroot_user: str, pubkey: str, cmds):
    """Enumerate a vSphere Server to find virtual machines and details."""
    logging.info("[!] Entering ssh_login_concurrent")
    if len(targets) > 20:
        workers = 20
    else:
        workers = len(targets)
    results_list = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        results = {pool.submit(configure_target, cmds, target, login_user, password, nonroot_user, pubkey): target for target in targets}

        for future in concurrent.futures.as_completed(results):
            if future.result():
                results_list.append(future.result())
    return results_list

def main():
    """Execute the tool."""
    opts = docopt(__doc__)
    try:
        TIME = datetime.now().strftime('%m-%d-%Y_%H:%M')
        LOG_NAME = "{}ssh_config.log".format(TIME)
        WORKING_DIR = os.path.dirname(os.path.abspath(__file__)) + "/{}".format(LOG_NAME)
        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO,
            handlers=[
                logging.FileHandler(filename=WORKING_DIR, mode="w+"),
                logging.StreamHandler()
                ]
            )
        logging.info("Parameters passed: {}".format(opts))

        if opts['--rhost']:
            targets = [validate_ipv4(opts['--rhost'])]
        elif opts['--rhost_file'] and os.path.isfile(opts['--rhost_file']):
            targets = generate_list_from_file(opts['--rhost_file'])

        # TODO: cleanup logic and error handling around the different .pub test cases
        if opts['--ssh_pubkey']:
            ssh_pubkey = opts['--ssh_pubkey']
            if ".pub" not in ssh_pubkey:
                ssh_pubkey = opts['--ssh_pubkey'] + ".pub"
            if not os.path.isfile(ssh_pubkey):
                raise BashException("{} does not exist".format(ssh_pubkey))
        else:
            ssh_pubkey = opts['--ssh_new_key']
            if ".pub" in ssh_pubkey:
                ssh_pubkey = ssh_pubkey.split(".")[0]
            if os.path.isfile(ssh_pubkey):
                raise BashException("[-] {} already exists, we will not overwrite key files please use --ssh_pubkey to supply a key or --ssh_new_key to supply a new name with path".format(ssh_pubkey))
            local_keygen_cmds = LocalKeygenCommands(abs_path_sshkey=ssh_pubkey)
            execute_local_commands(cmd=local_keygen_cmds.sshKeygen)
            execute_local_commands(cmd=local_keygen_cmds.sshKeyChmod)
            ssh_pubkey = ssh_pubkey + ".pub"

        pubkey_str = read_pubkey_file(ssh_pubkey)

        nonroot_user = opts['--nonroot_user']
        cmds = BashCommands(
            nonroot_user=nonroot_user,
            pubkey=pubkey_str
        )
        logging.info("Bash comands: {}".format(cmds))
        password = password_prompt(user=opts['--login_user'])
        results = configure_target_concurrent(
            targets=targets,
            login_user=opts['--login_user'],
            password=password,
            nonroot_user=opts['--nonroot_user'],
            pubkey=pubkey_str,
            cmds=cmds
            )

        print(json.dumps(results, indent=4, sort_keys=True))
    except Exception as ex:
        logging.info(str(ex))

if __name__ == '__main__':
    main()

