"""Overclock config/service detection for the BC-250.

Same rationale as topology.py: this is config state, not telemetry — it only
changes when someone edits a config file or (de)activates a service, so it's
fetched once per page load rather than polled every 700ms.

Two independent, unrelated OC tools live on a BC-250, with two different
service lifecycles:
  - bc250-smu-oc: a one-shot applier. It pushes the CPU curve/frequency
    override straight to the SMU over I2C, then *exits* — there's no
    RemainAfterExit, so systemd shows it as "inactive (dead)" a second or
    two after boot even on complete success. That's the expected happy
    path, not a sign the OC isn't applied — so "is this in effect" here
    means "did the last run finish successfully", not "is it active".
  - cyan-skillfish-governor-smu: a real long-running daemon that keeps
    adjusting GPU clock continuously. Here "active" (main process still
    alive and running) genuinely does mean "currently in effect".
"""

import configparser
import subprocess

try:
    import tomllib
except ImportError:
    tomllib = None

CPU_OC_CONF = '/etc/bc250-smu-oc.conf'
CPU_OC_SERVICE = 'bc250-smu-oc.service'
GPU_GOV_CONF = '/etc/cyan-skillfish-governor-smu/config.toml'
GPU_GOV_SERVICE = 'cyan-skillfish-governor-smu.service'


def _service_active(name):
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', name],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() == 'active'
    except (OSError, subprocess.SubprocessError):
        return False


def _service_ran_successfully(name):
    """For one-shot appliers: true if the last run actually completed with
    exit code 0, regardless of ActiveState (which will be "inactive" the
    moment a no-RemainAfterExit unit's process exits — success or not).

    A unit that was never installed/loaded at all also reports
    Result=success/ExecMainStatus=0 (systemd's unset defaults, not "it ran
    and succeeded") — so LoadState and an actual start timestamp must be
    checked too, or a system that never had this service would show a false
    "Active"."""
    try:
        result = subprocess.run(
            ['systemctl', 'show', name, '-p', 'LoadState', '-p', 'Result',
             '-p', 'ExecMainStatus', '-p', 'ExecMainStartTimestamp'],
            capture_output=True, text=True, timeout=2,
        )
        props = dict(line.split('=', 1) for line in result.stdout.strip().splitlines() if '=' in line)
        return (
            props.get('LoadState') == 'loaded'
            and bool(props.get('ExecMainStartTimestamp'))
            and props.get('Result') == 'success'
            and props.get('ExecMainStatus') == '0'
        )
    except (OSError, subprocess.SubprocessError):
        return False


def read_cpu_oc():
    parser = configparser.ConfigParser()
    target_freq = None
    max_temp = None
    try:
        if parser.read(CPU_OC_CONF):
            target_freq = parser.getint('overclock', 'frequency', fallback=None)
            max_temp = parser.getint('overclock', 'max_temperature', fallback=None)
    except (OSError, configparser.Error, ValueError):
        pass
    return {
        'config_present': target_freq is not None,
        'active': _service_ran_successfully(CPU_OC_SERVICE),
        'target_freq_mhz': target_freq,
        'max_temp_c': max_temp,
    }


def read_gpu_governor():
    max_freq = None
    min_freq = None
    if tomllib is not None:
        try:
            with open(GPU_GOV_CONF, 'rb') as f:
                data = tomllib.load(f)
            freq_range = data.get('frequency-range', {})
            max_freq = freq_range.get('max') or None
            min_freq = freq_range.get('min') or None
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return {
        'config_present': max_freq is not None,
        'active': _service_active(GPU_GOV_SERVICE),
        'max_freq_mhz': max_freq,
        'min_freq_mhz': min_freq,
    }


def read_overclock():
    return {
        'cpu': read_cpu_oc(),
        'gpu': read_gpu_governor(),
    }


if __name__ == '__main__':
    print(read_overclock())
