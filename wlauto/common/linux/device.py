#    Copyright 2014-2015 ARM Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# pylint: disable=E1101
import os
import re
import time
import base64
import socket
from collections import namedtuple
from subprocess import CalledProcessError

from wlauto.core.extension import Parameter
from wlauto.core.device import Device, RuntimeParameter, CoreParameter
from wlauto.core.resource import NO_ONE
from wlauto.exceptions import ConfigError, DeviceError, TimeoutError, DeviceNotRespondingError
from wlauto.common.resources import Executable
from wlauto.utils.cpuinfo import Cpuinfo
from wlauto.utils.misc import convert_new_lines, escape_double_quotes, ranges_to_list, ABI_MAP
from wlauto.utils.misc import isiterable, list_to_mask
from wlauto.utils.ssh import SshShell
from wlauto.utils.types import boolean, list_of_strings


FSTAB_ENTRY_REGEX = re.compile(r'(\S+) on (\S+) type (\S+) \((\S+)\)')

FstabEntry = namedtuple('FstabEntry', ['device', 'mount_point', 'fs_type', 'options', 'dump_freq', 'pass_num'])
PsEntry = namedtuple('PsEntry', 'user pid ppid vsize rss wchan pc state name')
LsmodEntry = namedtuple('LsmodEntry', ['name', 'size', 'use_count', 'used_by'])

GOOGLE_DNS_SERVER_ADDRESS = '8.8.8.8'


class BaseLinuxDevice(Device):  # pylint: disable=abstract-method

    path_module = 'posixpath'
    has_gpu = True

    parameters = [
        Parameter('scheduler', kind=str, default='unknown',
                  allowed_values=['unknown', 'smp', 'hmp', 'iks', 'ea', 'other'],
                  description="""
                  Specifies the type of multi-core scheduling model utilized in the device. The value
                  must be one of the following:

                  :unknown: A generic Device interface is used to interact with the underlying device
                            and the underlying scheduling model is unkown.
                  :smp: A standard single-core or Symmetric Multi-Processing system.
                  :hmp: ARM Heterogeneous Multi-Processing system.
                  :iks: Linaro In-Kernel Switcher.
                  :ea: ARM Energy-Aware scheduler.
                  :other: Any other system not covered by the above.

                          .. note:: most currently-available systems would fall under ``smp`` rather than
                                    this value. ``other`` is there to future-proof against new schemes
                                    not yet covered by WA.

                  """),
        Parameter('iks_switch_frequency', kind=int, default=None,
                  description="""
                 This is the switching frequency, in kilohertz, of IKS devices. This parameter *MUST NOT*
                 be set for non-IKS device (i.e. ``scheduler != 'iks'``). If left unset for IKS devices,
                 it will default to ``800000``, i.e. 800MHz.
                 """),
        Parameter('property_files', kind=list_of_strings,
                  default=[
                      '/etc/arch-release',
                      '/etc/debian_version',
                      '/etc/lsb-release',
                      '/proc/config.gz',
                      '/proc/cmdline',
                      '/proc/cpuinfo',
                      '/proc/version',
                      '/proc/zconfig',
                      '/sys/kernel/debug/sched_features',
                      '/sys/kernel/hmp',
                  ],
                  description='''
                  A list of paths to files containing static OS properties. These will be pulled into the
                  __meta directory in output for each run in order to provide information about the platfrom.
                  These paths do not have to exist and will be ignored if the path is not present on a
                  particular device.
                  '''),
        Parameter('binaries_directory',
                  description='Location of executable binaries on this device (must be in PATH).'),
        Parameter('working_directory',
                  description='''
                  Working directory to be used by WA. This must be in a location where the specified user
                  has write permissions. This will default to /home/<username>/wa (or to /root/wa, if
                  username is 'root').
                  '''),

    ]

    runtime_parameters = [
        RuntimeParameter('sysfile_values', 'get_sysfile_values', 'set_sysfile_values', value_name='params'),
        CoreParameter('${core}_cores', 'get_number_of_online_cores', 'set_number_of_online_cores',
                      value_name='number'),
        CoreParameter('${core}_min_frequency', 'get_core_min_frequency', 'set_core_min_frequency',
                      value_name='freq'),
        CoreParameter('${core}_max_frequency', 'get_core_max_frequency', 'set_core_max_frequency',
                      value_name='freq'),
        CoreParameter('${core}_frequency', 'get_core_cur_frequency', 'set_core_cur_frequency',
                      value_name='freq'),
        CoreParameter('${core}_governor', 'get_core_governor', 'set_core_governor',
                      value_name='governor'),
        CoreParameter('${core}_governor_tunables', 'get_core_governor_tunables', 'set_core_governor_tunables',
                      value_name='tunables'),
    ]

    dynamic_modules = [
        'devcpufreq',
        'cpuidle',
    ]

    @property
    def abi(self):
        if not self._abi:
            val = self.execute('uname -m').strip()
            for abi, architectures in ABI_MAP.iteritems():
                if val in architectures:
                    self._abi = abi
                    break
            else:
                self._abi = val
        return self._abi

    @property
    def supported_abi(self):
        return [self.abi]

    @property
    def online_cpus(self):
        val = self.get_sysfile_value('/sys/devices/system/cpu/online')
        return ranges_to_list(val)

    @property
    def number_of_cores(self):
        """
        Added in version 2.1.4.

        """
        if self._number_of_cores is None:
            corere = re.compile(r'^\s*cpu\d+\s*$')
            output = self.execute('ls /sys/devices/system/cpu')
            self._number_of_cores = 0
            for entry in output.split():
                if corere.match(entry):
                    self._number_of_cores += 1
        return self._number_of_cores

    @property
    def resource_cache(self):
        return self.path.join(self.working_directory, '.cache')

    @property
    def file_transfer_cache(self):
        return self.path.join(self.working_directory, '.transfer')

    @property
    def cpuinfo(self):
        if not self._cpuinfo:
            self._cpuinfo = Cpuinfo(self.execute('cat /proc/cpuinfo'))
        return self._cpuinfo

    def __init__(self, **kwargs):
        super(BaseLinuxDevice, self).__init__(**kwargs)
        self.busybox = None
        self._is_initialized = False
        self._is_ready = False
        self._just_rebooted = False
        self._is_rooted = None
        self._is_root_user = False
        self._available_frequencies = {}
        self._available_governors = {}
        self._available_governor_tunables = {}
        self._number_of_cores = None
        self._written_sysfiles = []
        self._cpuinfo = None
        self._abi = None

    def validate(self):
        if self.iks_switch_frequency is not None and self.scheduler != 'iks':  # pylint: disable=E0203
            raise ConfigError('iks_switch_frequency must NOT be set for non-IKS devices.')
        if self.iks_switch_frequency is None and self.scheduler == 'iks':  # pylint: disable=E0203
            self.iks_switch_frequency = 800000  # pylint: disable=W0201

    def initialize(self, context):
        self.execute('mkdir -p {}'.format(self.working_directory))
        if not self.binaries_directory:
            self._set_binaries_dir()
        self.execute('mkdir -p {}'.format(self.binaries_directory))
        self.busybox = self.deploy_busybox(context)

    def _set_binaries_dir(self):
        # pylint: disable=attribute-defined-outside-init
        self.binaries_directory = self.path.join(self.working_directory, "bin")

    def is_file(self, filepath):
        output = self.execute('if [ -f \'{}\' ]; then echo 1; else echo 0; fi'.format(filepath))
        # output from ssh my contain part of the expression in the buffer,
        # split out everything except the last word.
        return boolean(output.split()[-1])  # pylint: disable=maybe-no-member

    def is_directory(self, filepath):
        output = self.execute('if [ -d \'{}\' ]; then echo 1; else echo 0; fi'.format(filepath))
        # output from ssh my contain part of the expression in the buffer,
        # split out everything except the last word.
        return boolean(output.split()[-1])  # pylint: disable=maybe-no-member

    def get_properties(self, context):
        for propfile in self.property_files:
            try:
                normname = propfile.lstrip(self.path.sep).replace(self.path.sep, '.')
                outfile = os.path.join(context.host_working_directory, normname)
                if self.is_file(propfile):
                    with open(outfile, 'w') as wfh:
                        if propfile.endswith(".gz"):
                            wfh.write(self.execute('{} zcat {}'.format(self.busybox, propfile)))
                        else:
                            wfh.write(self.execute('cat {}'.format(propfile)))
                elif self.is_directory(propfile):
                    self.pull_file(propfile, outfile)
                else:
                    continue
            except DeviceError:
                # We pull these files "opportunistically", so if a pull fails
                # (e.g. we don't have permissions to read the file), just note
                # it quietly (not as an error/warning) and move on.
                self.logger.debug('Could not pull property file "{}"'.format(propfile))
        return {}

    def get_sysfile_value(self, sysfile, kind=None, binary=False):
        """
        Get the contents of the specified sysfile.

        :param sysfile: The file who's contents will be returned.

        :param kind: The type of value to be expected in the sysfile. This can
                     be any Python callable that takes a single str argument.
                     If not specified or is None, the contents will be returned
                     as a string.
        :param binary: Whether the value should be encoded into base64 for reading
                       to deal with binary format.

        """
        if binary:
            output = self.execute('{} base64 {}'.format(self.busybox, sysfile), as_root=self.is_rooted).strip()
            output = output.decode('base64')
        else:
            output = self.execute('cat \'{}\''.format(sysfile), as_root=self.is_rooted).strip()  # pylint: disable=E1103
        if kind:
            return kind(output)
        else:
            return output

    def set_sysfile_value(self, sysfile, value, verify=True, binary=False):
        """
        Set the value of the specified sysfile. By default, the value will be checked afterwards.
        Can be overridden by setting ``verify`` parameter to ``False``. By default binary values
        will not be written correctly this can be changed by setting the ``binary`` parameter to
        ``True``.

        """
        value = str(value)
        if binary:
            # Value is already string encoded, so need to decode before encoding in base64
            try:
                value = str(value.decode('string_escape'))
            except ValueError as e:
                msg = 'Can not interpret value "{}" for "{}": {}'
                raise ValueError(msg.format(value, sysfile, e.message))

            encoded_value = base64.b64encode(value)
            cmd = 'echo {} | {} base64 -d > \'{}\''.format(encoded_value, self.busybox, sysfile)
        else:
            cmd = 'echo {} > \'{}\''.format(value, sysfile)
        self.execute(cmd, check_exit_code=False, as_root=True)

        if verify:
            output = self.get_sysfile_value(sysfile, binary=binary)
            if output.strip() != value:  # pylint: disable=E1103
                message = 'Could not set the value of {} to {}'.format(sysfile, value)
                raise DeviceError(message)
        self._written_sysfiles.append((sysfile, binary))

    def get_sysfile_values(self):
        """
        Returns a dict mapping paths of sysfiles that were previously set to their
        current values.

        """
        values = {}
        for sysfile, binary in self._written_sysfiles:
            values[sysfile] = self.get_sysfile_value(sysfile, binary=binary)
        return values

    def set_sysfile_values(self, params):
        """
        The plural version of ``set_sysfile_value``. Takes a single parameter which is a mapping of
        file paths to values to be set. By default, every value written will be verified. This can
        be disabled for individual paths by appending ``'!'`` to them. To enable values being
        written as binary data, a ``'^'`` can be prefixed to the path.

        """
        for sysfile, value in params.iteritems():
            verify = not sysfile.endswith('!')
            sysfile = sysfile.rstrip('!')
            binary = sysfile.startswith('^')
            sysfile = sysfile.lstrip('^')
            self.set_sysfile_value(sysfile, value, verify=verify, binary=binary)

    def deploy_busybox(self, context, force=False):
        """
        Deploys the busybox binary to the specified device and returns
        the path to the binary on the device.

        :param context: an instance of ExecutionContext
        :param force: by default, if the binary is already present on the
                    device, it will not be deployed again. Setting force
                    to ``True`` overrides that behavior and ensures that the
                    binary is always copied. Defaults to ``False``.

        :returns: The on-device path to the busybox binary.

        """
        on_device_executable = self.get_binary_path("busybox", search_system_binaries=False)
        if force or not on_device_executable:
            host_file = context.resolver.get(Executable(NO_ONE, self.abi, 'busybox'))
            return self.install(host_file)
        return on_device_executable

    def is_installed(self, name):  # pylint: disable=unused-argument,no-self-use
        raise AttributeError("""Instead of using is_installed, please use
            ``get_binary_path`` or ``install_if_needed`` instead. You should
            use the path returned by these functions to then invoke the binary

            please see: https://pythonhosted.org/wlauto/writing_extensions.html""")

    def is_network_connected(self):
        """
        Checks for internet connectivity on the device by pinging IP address provided.

        :param ip_address: IP address to ping. Default is Google's public DNS server (8.8.8.8)

        :returns: ``True`` if internet is available, ``False`` otherwise.

        """
        self.logger.debug('Checking for internet connectivity...')
        return self._ping_server(GOOGLE_DNS_SERVER_ADDRESS)

    def _ping_server(self, ip_address, timeout=1, packet_count=1):
        output = self.execute('ping -q -c {} -w {} {}'.format(packet_count, timeout, ip_address),
                              check_exit_code=False)

        if 'network is unreachable' in output.lower():
            self.logger.debug('Cannot find IP address {}'.format(ip_address))
            return False
        else:
            self.logger.debug('Found IP address {}'.format(ip_address))
            return True

    def get_binary_path(self, name, search_system_binaries=True):
        """
        Searches the devices ``binary_directory`` for the given binary,
        if it cant find it there it tries using which to find it.

        :param name: The name of the binary
        :param search_system_binaries: By default this function will try using
                                       which to find the binary if it isn't in
                                       ``binary_directory``. When this is set
                                       to ``False`` it will not try this.

        :returns: The on-device path to the binary.

        """
        wa_binary = self.path.join(self.binaries_directory, name)
        if self.file_exists(wa_binary):
            return wa_binary
        if search_system_binaries:
            try:
                return self.execute('{} which {}'.format(self.busybox, name)).strip()
            except DeviceError:
                pass
        return None

    def install_if_needed(self, host_path, search_system_binaries=True):
        """
        Similar to get_binary_path but will install the binary if not found.

        :param host_path: The path to the binary on the host
        :param search_system_binaries: By default this function will try using
                                       which to find the binary if it isn't in
                                       ``binary_directory``. When this is set
                                       to ``False`` it will not try this.

        :returns: The on-device path to the binary.

        """
        binary_path = self.get_binary_path(os.path.split(host_path)[1],
                                           search_system_binaries=search_system_binaries)
        if not binary_path:
            binary_path = self.install(host_path)
        return binary_path

    def list_file_systems(self):
        output = self.execute('mount')
        fstab = []
        for line in output.split('\n'):
            line = line.strip()
            if not line:
                continue
            match = FSTAB_ENTRY_REGEX.search(line)
            if match:
                fstab.append(FstabEntry(match.group(1), match.group(2),
                                        match.group(3), match.group(4),
                                        None, None))
            else:  # assume pre-M Android
                fstab.append(FstabEntry(*line.split()))
        return fstab

    # Process query and control

    def get_pids_of(self, process_name):
        raise NotImplementedError()

    def ps(self, **kwargs):
        raise NotImplementedError()

    def kill(self, pid, signal=None, as_root=False):  # pylint: disable=W0221
        """
        Kill the specified process.

            :param pid: PID of the process to kill.
            :param signal: Specify which singal to send to the process. This must
                           be a valid value for -s option of kill. Defaults to ``None``.

        Modified in version 2.1.4: added ``signal`` parameter.

        """
        signal_string = '-s {}'.format(signal) if signal else ''
        self.execute('kill {} {}'.format(signal_string, pid), as_root=as_root)

    def killall(self, process_name, signal=None, as_root=None):  # pylint: disable=W0221
        """
        Kill all processes with the specified name.

            :param process_name: The name of the process(es) to kill.
            :param signal: Specify which singal to send to the process. This must
                           be a valid value for -s option of kill. Defaults to ``None``.

        Modified in version 2.1.5: added ``as_root`` parameter.

        """
        if as_root is None:
            as_root = self.is_rooted
        for pid in self.get_pids_of(process_name):
            self.kill(pid, signal=signal, as_root=as_root)

    def get_online_cpus(self, c):
        if isinstance(c, int):  # assume c == cluster
            return [i for i in self.online_cpus if self.core_clusters[i] == c]
        elif isinstance(c, basestring):  # assume c == core
            return [i for i in self.online_cpus if self.core_names[i] == c]
        else:
            raise ValueError(c)

    # hotplug

    def enable_cpu(self, cpu):
        """
        Enable the specified core.

        :param cpu: CPU core to enable. This must be the full name as it
                    appears in sysfs, e.g. "cpu0".

        """
        self.hotplug_cpu(cpu, online=True)

    def disable_cpu(self, cpu):
        """
        Disable the specified core.

        :param cpu: CPU core to disable. This must be the full name as it
                    appears in sysfs, e.g. "cpu0".
        """
        self.hotplug_cpu(cpu, online=False)

    def hotplug_cpu(self, cpu, online):
        """
        Hotplug the specified CPU either on or off.
        See https://www.kernel.org/doc/Documentation/cpu-hotplug.txt

        :param cpu: The CPU for which the governor is to be set. This must be
                    the full name as it appears in sysfs, e.g. "cpu0".
        :param online: CPU will be enabled if this value bool()'s to True, and
                       will be disabled otherwise.

        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        status = 1 if online else 0
        sysfile = '/sys/devices/system/cpu/{}/online'.format(cpu)
        self.set_sysfile_value(sysfile, status)

    def get_number_of_online_cores(self, core):
        if core not in self.core_names:
            raise ValueError('Unexpected core: {}; must be in {}'.format(core, list(set(self.core_names))))
        online_cpus = self.online_cpus
        num_active_cores = 0
        for i, c in enumerate(self.core_names):
            if c == core and i in online_cpus:
                num_active_cores += 1
        return num_active_cores

    def set_number_of_online_cores(self, core, number):  # NOQA
        if core not in self.core_names:
            raise ValueError('Unexpected core: {}; must be in {}'.format(core, list(set(self.core_names))))
        core_ids = [i for i, c in enumerate(self.core_names) if c == core]
        max_cores = len(core_ids)
        if number > max_cores:
            message = 'Attempting to set the number of active {} to {}; maximum is {}'
            raise ValueError(message.format(core, number, max_cores))

        if not number:
            # make sure at least one other core is enabled to avoid trying to
            # hotplug everything.
            for i, c in enumerate(self.core_names):
                if c != core and i in self.online_cpus:
                    break
            else:  # did not find one
                raise ValueError('Cannot hotplug all cpus on the device!')

        for i in xrange(0, number):
            self.enable_cpu(core_ids[i])
        for i in xrange(number, max_cores):
            self.disable_cpu(core_ids[i])

    def invoke(self, binary, args=None, in_directory=None, on_cpus=None,
               background=False, as_root=False, timeout=30):
        """
        Executes the specified binary under the specified conditions.

        :binary: binary to execute. Must be present and executable on the device.
        :args: arguments to be passed to the binary. The can be either a list or
               a string.
        :in_directory:  execute the binary in the  specified directory. This must
                        be an absolute path.
        :on_cpus:  taskset the binary to these CPUs. This may be a single ``int`` (in which
                   case, it will be interpreted as the mask), a list of ``ints``, in which
                   case this will be interpreted as the list of cpus, or string, which
                   will be interpreted as a comma-separated list of cpu ranges, e.g.
                   ``"0,4-7"``.
        :background: If ``True``, a ``subprocess.Popen`` object will be returned straight
                     away. If ``False`` (the default), this will wait for the command to
                     terminate and return the STDOUT output
        :as_root: Specify whether the command should be run as root
        :timeout: If the invocation does not terminate within this number of seconds,
                  a ``TimeoutError`` exception will be raised. Set to ``None`` if the
                  invocation should not timeout.

        """
        command = binary
        if args:
            if isiterable(args):
                args = ' '.join(args)
            command = '{} {}'.format(command, args)
        if on_cpus:
            if isinstance(on_cpus, basestring):
                on_cpus = ranges_to_list(on_cpus)
            if isiterable(on_cpus):
                on_cpus = list_to_mask(on_cpus)  # pylint: disable=redefined-variable-type
            command = '{} taskset 0x{:x} {}'.format(self.busybox, on_cpus, command)
        if in_directory:
            command = 'cd {} && {}'.format(in_directory, command)
        return self.execute(command, background=background, as_root=as_root, timeout=timeout)

    def get_device_model(self):
        if self.file_exists("/proc/device-tree/model"):
            raw_model = self.execute("cat /proc/device-tree/model")
            device_model_to_return = '_'.join(raw_model.split()[:2])
            return device_model_to_return.rstrip(' \t\r\n\0')
        # Right now we don't know any other way to get device model
        # info in linux on arm platforms
        return None

    # internal methods

    def _check_ready(self):
        if not self._is_ready:
            raise RuntimeError('Device not ready (has connect() been called?)')

    def _get_core_cluster(self, core):
        """Returns the first cluster that has cores of the specified type. Raises
        value error if no cluster for the specified type has been found"""
        core_indexes = [i for i, c in enumerate(self.core_names) if c == core]
        core_clusters = set(self.core_clusters[i] for i in core_indexes)
        if not core_clusters:
            raise ValueError('No cluster found for core {}'.format(core))
        return sorted(list(core_clusters))[0]


class LinuxDevice(BaseLinuxDevice):

    platform = 'linux'

    default_timeout = 30
    delay = 2
    long_delay = 3 * delay
    ready_timeout = 60

    parameters = [
        Parameter('host', mandatory=True, description='Host name or IP address for the device.'),
        Parameter('username', mandatory=True, description='User name for the account on the device.'),
        Parameter('password', description='Password for the account on the device (for password-based auth).'),
        Parameter('keyfile', description='Keyfile to be used for key-based authentication.'),
        Parameter('port', kind=int, default=22, description='SSH port number on the device.'),
        Parameter('password_prompt', default='[sudo] password',
                  description='Prompt presented by sudo when requesting the password.'),

        Parameter('use_telnet', kind=boolean, default=False,
                  description='Optionally, telnet may be used instead of ssh, though this is discouraged.'),
        Parameter('boot_timeout', kind=int, default=120,
                  description='How long to try to connect to the device after a reboot.'),
    ]

    @property
    def is_rooted(self):
        self._check_ready()
        if self._is_rooted is None:
            # First check if the user is root
            try:
                self.execute('test $(id -u) = 0')
                self._is_root_user = True
                self._is_rooted = True
                return self._is_rooted
            except DeviceError:
                self._is_root_user = False

            # Otherwise, check if the user has sudo rights
            try:
                self.execute('ls /', as_root=True)
                self._is_rooted = True
            except DeviceError:
                self._is_rooted = False
        return self._is_rooted

    def __init__(self, *args, **kwargs):
        super(LinuxDevice, self).__init__(*args, **kwargs)
        self.shell = None
        self._is_rooted = None

    def validate(self):
        if self.working_directory is None:  # pylint: disable=access-member-before-definition
            if self.username == 'root':
                self.working_directory = '/root/wa'  # pylint: disable=attribute-defined-outside-init
            else:
                self.working_directory = '/home/{}/wa'.format(self.username)  # pylint: disable=attribute-defined-outside-init

    def initialize(self, context, *args, **kwargs):
        self.execute('mkdir -p {}'.format(self.binaries_directory))
        self.execute('export PATH={}:$PATH'.format(self.binaries_directory))
        super(LinuxDevice, self).initialize(context, *args, **kwargs)

    # Power control

    def reset(self):
        try:
            self.execute('reboot', as_root=True)
        except DeviceError as e:
            if 'Connection dropped' not in e.message:
                raise e
        self._is_ready = False

    def hard_reset(self):
        self._is_ready = False

    def boot(self, hard=False, **kwargs):
        if hard:
            self.hard_reset()
        else:
            self.reset()
        self.logger.debug('Waiting for device...')
        # Wait a fixed delay before starting polling to give the device time to
        # shut down, otherwise, might create the connection while it's still shutting
        # down resulting in subsequenct connection failing.
        initial_delay = 20
        time.sleep(initial_delay)
        boot_timeout = max(self.boot_timeout - initial_delay, 10)

        start_time = time.time()
        while (time.time() - start_time) < boot_timeout:
            try:
                s = socket.create_connection((self.host, self.port), timeout=5)
                s.close()
                break
            except socket.timeout:
                pass
            except socket.error:
                time.sleep(5)
        else:
            raise DeviceError('Could not connect to {} after reboot'.format(self.host))

    def connect(self):  # NOQA pylint: disable=R0912
        self.shell = SshShell(password_prompt=self.password_prompt,
                              timeout=self.default_timeout, telnet=self.use_telnet)
        self.shell.login(self.host, self.username, self.password, self.keyfile, self.port)
        self._is_ready = True

    def disconnect(self):  # NOQA pylint: disable=R0912
        self.shell.logout()
        self._is_ready = False

    # Execution

    def execute(self, command, timeout=default_timeout, check_exit_code=True, background=False,
                as_root=False, strip_colors=True, **kwargs):
        """
        Execute the specified command on the device using adb.

        Parameters:

            :param command: The command to be executed. It should appear exactly
                            as if you were typing it into a shell.
            :param timeout: Time, in seconds, to wait for adb to return before aborting
                            and raising an error. Defaults to ``AndroidDevice.default_timeout``.
            :param check_exit_code: If ``True``, the return code of the command on the Device will
                                    be check and exception will be raised if it is not 0.
                                    Defaults to ``True``.
            :param background: If ``True``, will execute create a new ssh shell rather than using
                               the default session and will return it immediately. If this is ``True``,
                               ``timeout``, ``strip_colors`` and (obvisously) ``check_exit_code`` will
                               be ignored; also, with this, ``as_root=True``  is only valid if ``username``
                               for the device was set to ``root``.
            :param as_root: If ``True``, will attempt to execute command in privileged mode. The device
                            must be rooted, otherwise an error will be raised. Defaults to ``False``.

                            Added in version 2.1.3

        :returns: If ``background`` parameter is set to ``True``, the subprocess object will
                  be returned; otherwise, the contents of STDOUT from the device will be returned.

        """
        self._check_ready()
        try:
            if background:
                if as_root and self.username != 'root':
                    raise DeviceError('Cannot execute in background with as_root=True unless user is root.')
                return self.shell.background(command)
            else:
                # If we're already the root user, don't bother with sudo
                if self._is_root_user:
                    as_root = False
                return self.shell.execute(command, timeout, check_exit_code, as_root, strip_colors)
        except CalledProcessError as e:
            raise DeviceError(e)

    def kick_off(self, command, as_root=None):
        """
        Like execute but closes ssh session and returns immediately, leaving the command running on the
        device (this is different from execute(background=True) which keeps ssh connection open and returns
        a subprocess object).

        """
        if as_root is None:
            as_root = self.is_rooted
        self._check_ready()
        command = 'sh -c "{}" 1>/dev/null 2>/dev/null &'.format(escape_double_quotes(command))
        return self.shell.execute(command, as_root=as_root)

    def get_pids_of(self, process_name):
        """Returns a list of PIDs of all processes with the specified name."""
        # result should be a column of PIDs with the first row as "PID" header
        result = self.execute('ps -C {} -o pid'.format(process_name),  # NOQA
                              check_exit_code=False).strip().split()
        if len(result) >= 2:  # at least one row besides the header
            return map(int, result[1:])
        else:
            return []

    def ps(self, **kwargs):
        command = 'ps -eo user,pid,ppid,vsize,rss,wchan,pcpu,state,fname'
        lines = iter(convert_new_lines(self.execute(command)).split('\n'))
        lines.next()  # header

        result = []
        for line in lines:
            parts = re.split(r'\s+', line, maxsplit=8)
            if parts:
                result.append(PsEntry(*(parts[0:1] + map(int, parts[1:5]) + parts[5:])))

        if not kwargs:
            return result
        else:
            filtered_result = []
            for entry in result:
                if all(getattr(entry, k) == v for k, v in kwargs.iteritems()):
                    filtered_result.append(entry)
            return filtered_result

    # File management

    def push_file(self, source, dest, as_root=False, timeout=default_timeout):  # pylint: disable=W0221
        self._check_ready()
        try:
            if not as_root or self.username == 'root':
                self.shell.push_file(source, dest, timeout=timeout)
            else:
                tempfile = self.path.join(self.working_directory, self.path.basename(dest))
                self.shell.push_file(source, tempfile, timeout=timeout)
                self.shell.execute('cp -r {} {}'.format(tempfile, dest), timeout=timeout, as_root=True)
        except CalledProcessError as e:
            raise DeviceError(e)

    def pull_file(self, source, dest, as_root=False, timeout=default_timeout):  # pylint: disable=W0221
        self._check_ready()
        try:
            if not as_root or self.username == 'root':
                self.shell.pull_file(source, dest, timeout=timeout)
            else:
                tempfile = self.path.join(self.working_directory, self.path.basename(source))
                self.shell.execute('cp -r {} {}'.format(source, tempfile), timeout=timeout, as_root=True)
                self.shell.execute('chown -R {} {}'.format(self.username, tempfile), timeout=timeout, as_root=True)
                self.shell.pull_file(tempfile, dest, timeout=timeout)
        except CalledProcessError as e:
            raise DeviceError(e)

    def delete_file(self, filepath, as_root=False):  # pylint: disable=W0221
        self.execute('rm -rf {}'.format(filepath), as_root=as_root)

    def file_exists(self, filepath):
        output = self.execute('if [ -e \'{}\' ]; then echo 1; else echo 0; fi'.format(filepath))
        # output from ssh my contain part of the expression in the buffer,
        # split out everything except the last word.
        return boolean(output.split()[-1])  # pylint: disable=maybe-no-member

    def listdir(self, path, as_root=False, **kwargs):
        contents = self.execute('ls -1 {}'.format(path), as_root=as_root).strip()
        if not contents:
            return []
        return [x.strip() for x in contents.split('\n')]  # pylint: disable=maybe-no-member

    def install(self, filepath, timeout=default_timeout, with_name=None):  # pylint: disable=W0221
        destpath = self.path.join(self.binaries_directory,
                                  with_name or self.path.basename(filepath))
        self.push_file(filepath, destpath, as_root=True)
        self.execute('chmod a+x {}'.format(destpath), timeout=timeout, as_root=True)
        return destpath

    install_executable = install  # compatibility

    def uninstall(self, executable_name):
        on_device_executable = self.get_binary_path(executable_name, search_system_binaries=False)
        if not on_device_executable:
            raise DeviceError("Could not uninstall {}, binary not found".format(on_device_executable))
        self.delete_file(on_device_executable, as_root=self.is_rooted)

    uninstall_executable = uninstall  # compatibility

    # misc

    def lsmod(self):
        """List loaded kernel modules."""
        lines = self.execute('lsmod').splitlines()
        entries = []
        for line in lines[1:]:  # first line is the header
            if not line.strip():
                continue
            parts = line.split()
            name = parts[0]
            size = int(parts[1])
            use_count = int(parts[2])
            if len(parts) > 3:
                used_by = ''.join(parts[3:]).split(',')
            else:
                used_by = []
            entries.append(LsmodEntry(name, size, use_count, used_by))
        return entries

    def insmod(self, path):
        """Install a kernel module located on the host on the target device."""
        target_path = self.path.join(self.working_directory, os.path.basename(path))
        self.push_file(path, target_path)
        self.execute('insmod {}'.format(target_path), as_root=True)

    def ping(self):
        try:
            # May be triggered inside initialize()
            self.shell.execute('ls /', timeout=5)
        except (TimeoutError, CalledProcessError):
            raise DeviceNotRespondingError(self.host)

    def capture_screen(self, filepath):
        if not self.get_binary_path('scrot'):
            self.logger.debug('Could not take screenshot as scrot is not installed.')
            return
        try:
            tempfile = self.path.join(self.working_directory, os.path.basename(filepath))
            self.execute('DISPLAY=:0.0 scrot {}'.format(tempfile))
            self.pull_file(tempfile, filepath)
            self.delete_file(tempfile)
        except DeviceError as e:
            if "Can't open X dispay." not in e.message:
                raise e
            message = e.message.split('OUTPUT:', 1)[1].strip()
            self.logger.debug('Could not take screenshot: {}'.format(message))

    def is_screen_on(self):
        pass  # TODO

    def ensure_screen_is_on(self):
        pass  # TODO
