import glob
import json
import os
import subprocess
import sys
import time

from apt_offline_core.AptOfflineCoreLib import (
    _detect_transfer_method,
    _latest_state,
    _ssh_run,
    _transfer_get,
    _transfer_put,
)


def _detect_sudo(host):
    result = subprocess.run(
        ["ssh", "-o", "LogLevel=QUIET", host, "id -u"],
        capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip().splitlines()[-1] == "0":
        return []
    return ["sudo"]


def _cleanup_host(work_dir, keep):
    states = sorted(glob.glob(os.path.join(work_dir, "apt-remote-*.state")))
    to_remove = states[:-keep] if keep > 0 else states
    for state_path in to_remove:
        ts = os.path.basename(state_path)[len("apt-remote-"):-len(".state")]
        for path in [
            os.path.join(work_dir, "apt-remote-%s.sig" % ts),
            os.path.join(work_dir, "bundle-%s.zip" % ts),
            state_path,
        ]:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def _remote_single(host, args, log):
    if args.work_dir:
        base_dir = args.work_dir
    elif args.temp:
        base_dir = "/tmp/apt-offline-remote"
    else:
        base_dir = os.path.expanduser("~/.cache/apt-offline")
    work_dir = os.path.join(base_dir, host)

    phase = args.remote_phase  # "fetch", "download", "finish-install", or None (full)
    keep = args.keep_latest

    # standalone cleanup — no phase, no operation flags
    if (phase is None
            and not args.remote_update
            and not args.remote_upgrade
            and not args.remote_dist_upgrade
            and not args.remote_install_packages
            and keep is not None):
        if not os.path.isdir(work_dir):
            log.msg("==> Nothing to clean for %s (work dir does not exist)\n" % host)
            return
        _cleanup_host(work_dir, keep)
        log.msg("==> Cleaned up work dir for %s (kept %d)\n" % (host, keep))
        return

    os.makedirs(work_dir, exist_ok=True)

    # ------------------------------------------------------------------ phase 2
    if phase == "download":
        state = _latest_state(work_dir)
        local_sig = os.path.join(work_dir, os.path.basename(state["sig"]))
        local_bundle = os.path.join(work_dir, os.path.basename(state["bundle"]))
        log.msg("==> Creating bundle from %s...\n" % state["sig"])
        subprocess.run(
            ["sudo", "apt-offline", "get", "--bundle", local_bundle, local_sig],
            check=True
        )
        log.success("Bundle created: %s\n" % local_bundle)
        if keep is not None:
            _cleanup_host(work_dir, keep)
        return

    # ------------------------------------------------------------------ phase 3
    if phase == "finish-install":
        state = _latest_state(work_dir)
        sudo_prefix = _detect_sudo(host)
        local_bundle = os.path.join(work_dir, os.path.basename(state["bundle"]))
        remote_bundle = state["bundle"]
        _ssh_run(host, ["mkdir", "-p", ".cache/apt-offline"])
        log.msg("==> Detecting transfer method...\n")
        transfer = _detect_transfer_method(host)
        log.msg("==> Using %s for file transfers\n" % transfer)
        log.msg("==> [1/3] Sending bundle to %s...\n" % host)
        _transfer_put(transfer, host, local_bundle, remote_bundle)
        log.msg("==> [2/3] Installing bundle on %s...\n" % host)
        _ssh_run(host, sudo_prefix + ["apt-offline", "install", remote_bundle])
        log.msg("==> [3/3] Running apt-get on %s...\n" % host)
        if state["dist_upgrade"]:
            _ssh_run(host, sudo_prefix + ["apt-get", "dist-upgrade", "-y"])
        elif state["upgrade"]:
            _ssh_run(host, sudo_prefix + ["apt-get", "upgrade", "-y"])
        elif state["install_packages"]:
            _ssh_run(host, sudo_prefix + ["apt-get", "install", "-y"] + state["install_packages"])
        log.success("Operation completed successfully for %s\n" % host)
        if args.reboot:
            log.msg("==> Rebooting %s...\n" % host)
            _ssh_run(host, sudo_prefix + ["reboot"])
        if keep is not None:
            _cleanup_host(work_dir, keep)
        return

    # --------------------------------------------------- phase 1 / full pipeline
    sudo_prefix = _detect_sudo(host)
    timestamp = int(time.time())
    remote_sig = ".cache/apt-offline/apt-remote-%s.sig" % timestamp
    remote_bundle = ".cache/apt-offline/bundle-%s.zip" % timestamp
    local_sig = os.path.join(work_dir, "apt-remote-%s.sig" % timestamp)
    local_bundle = os.path.join(work_dir, "bundle-%s.zip" % timestamp)
    local_state = os.path.join(work_dir, "apt-remote-%s.state" % timestamp)
    _ssh_run(host, ["mkdir", "-p", ".cache/apt-offline"])

    log.msg("==> Detecting transfer method...\n")
    transfer = _detect_transfer_method(host)
    log.msg("==> Using %s for file transfers\n" % transfer)

    steps = 2 if phase == "fetch" else 5
    log.msg("==> [1/%d] Generating signature on %s...\n" % (steps, host))
    set_cmd = sudo_prefix + ["apt-offline", "set"]
    if args.remote_install_packages:
        set_cmd += ["--install-packages"] + args.remote_install_packages
    if args.remote_update:
        set_cmd += ["--update"]
    if args.remote_upgrade:
        set_cmd += ["--upgrade"]
    if args.remote_dist_upgrade:
        set_cmd += ["--upgrade-type", "dist-upgrade"]
    set_cmd += [remote_sig]
    _ssh_run(host, set_cmd)

    log.msg("==> [2/%d] Fetching signature...\n" % steps)
    _transfer_get(transfer, host, remote_sig, local_sig)

    with open(local_state, "w") as f:
        json.dump({
            "sig": remote_sig,
            "bundle": remote_bundle,
            "update": args.remote_update,
            "upgrade": args.remote_upgrade,
            "dist_upgrade": args.remote_dist_upgrade,
            "install_packages": args.remote_install_packages or [],
        }, f, indent=2)

    if phase == "fetch":
        log.success("Signature fetched: %s\n" % local_sig)
        log.msg("==> Run 'apt-offline remote %s --download' to create the bundle when online\n" % host)
        if keep is not None:
            _cleanup_host(work_dir, keep)
        return

    log.msg("==> [3/5] Creating bundle locally...\n")
    subprocess.run(
        ["sudo", "apt-offline", "get", "--bundle", local_bundle, local_sig],
        check=True
    )

    log.msg("==> [4/5] Sending bundle to remote...\n")
    _transfer_put(transfer, host, local_bundle, remote_bundle)

    log.msg("==> [5/5] Installing bundle on remote...\n")
    _ssh_run(host, sudo_prefix + ["apt-offline", "install", remote_bundle])

    if args.remote_dist_upgrade:
        _ssh_run(host, sudo_prefix + ["apt-get", "dist-upgrade", "-y"])
    elif args.remote_upgrade:
        _ssh_run(host, sudo_prefix + ["apt-get", "upgrade", "-y"])
    elif args.remote_install_packages:
        _ssh_run(host, sudo_prefix + ["apt-get", "install", "-y"] + args.remote_install_packages)

    log.success("Operation completed successfully for %s\n" % host)

    if args.reboot:
        log.msg("==> Rebooting %s...\n" % host)
        _ssh_run(host, sudo_prefix + ["reboot"])

    if keep is not None:
        _cleanup_host(work_dir, keep)


def _read_hosts_file(path):
    hosts = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                hosts.append(line)
    if not hosts:
        raise ValueError("No hosts found in %s" % path)
    return hosts


def remote(args):
    from apt_offline_core import AptOfflineCoreLib
    log = AptOfflineCoreLib.log

    if args.remote_host and args.hosts_list:
        log.err("Cannot specify both SSH_HOST and --hosts-list\n")
        sys.exit(1)
    if not args.remote_host and not args.hosts_list:
        log.err("Must specify either SSH_HOST or --hosts-list\n")
        sys.exit(1)

    if args.hosts_list:
        try:
            hosts = _read_hosts_file(args.hosts_list)
        except (OSError, ValueError) as e:
            log.err("Error reading hosts list: %s\n" % e)
            sys.exit(1)
    else:
        hosts = [args.remote_host]

    succeeded = []
    failed = []

    for host in hosts:
        if len(hosts) > 1:
            log.msg("\n==> Host: %s\n" % host)
        try:
            _remote_single(host, args, log)
            succeeded.append(host)
        except Exception as e:
            log.err("FAILED %s: %s\n" % (host, e))
            failed.append(host)

    if len(hosts) > 1:
        log.msg("\n==> Batch summary: %d succeeded, %d failed\n" % (len(succeeded), len(failed)))
        for host in succeeded:
            log.msg("   OK: %s\n" % host)
        for host in failed:
            log.err("%s\n" % host)
        if failed:
            sys.exit(1)


def register_subparser(subparsers, global_options):
    parser_remote = subparsers.add_parser(
        "remote",
        parents=[global_options],
        help="Run the full set/get/install pipeline against a remote host over SSH",
    )
    parser_remote.set_defaults(func=remote)

    parser_remote.add_argument(
        "remote_host",
        nargs="?",
        default=None,
        help="SSH host to target (an alias from ~/.ssh/config, or [user@]hostname)",
        metavar="SSH_HOST",
    )

    parser_remote.add_argument(
        "--hosts-list",
        dest="hosts_list",
        help="Text file with one SSH host per line (# comments and blank lines ignored)",
        action="store",
        type=str,
        default=None,
        metavar="FILE",
    )

    parser_remote.add_argument(
        "--update",
        dest="remote_update",
        help="Generate signature to update APT database on the remote",
        action="store_true",
    )

    parser_remote.add_argument(
        "--upgrade",
        dest="remote_upgrade",
        help="Generate signature of packages to be upgraded on the remote",
        action="store_true",
    )

    parser_remote.add_argument(
        "--dist-upgrade",
        dest="remote_dist_upgrade",
        help="Perform a full dist-upgrade on the remote",
        action="store_true",
    )

    parser_remote.add_argument(
        "--install-packages",
        dest="remote_install_packages",
        help="Packages to install on the remote machine",
        action="store",
        type=str,
        nargs="*",
        metavar="PKG",
    )

    parser_remote.add_argument(
        "--reboot",
        dest="reboot",
        help="Reboot the remote machine after a successful operation",
        action="store_true",
    )

    work_dir_group = parser_remote.add_mutually_exclusive_group()
    work_dir_group.add_argument(
        "--work-dir",
        dest="work_dir",
        help="Local directory for sig/bundle files (default: ~/.cache/apt-offline)",
        action="store",
        type=str,
        default=None,
        metavar="DIR",
    )
    work_dir_group.add_argument(
        "--temp",
        dest="temp",
        help="Use /tmp/apt-offline-remote as the local working directory",
        action="store_true",
        default=False,
    )

    parser_remote.add_argument(
        "--keep-latest",
        dest="keep_latest",
        help="Keep only the N most recent operations per host (default: 1 if flag is given)",
        nargs="?",
        const=1,
        default=None,
        type=int,
        metavar="N",
    )

    phase_group = parser_remote.add_mutually_exclusive_group()
    phase_group.add_argument(
        "--fetch",
        dest="remote_phase",
        action="store_const",
        const="fetch",
        help="Phase 1: generate and fetch the signature from the remote only",
    )
    phase_group.add_argument(
        "--download",
        dest="remote_phase",
        action="store_const",
        const="download",
        help="Phase 2: download packages locally and create bundle from the latest fetched signature",
    )
    phase_group.add_argument(
        "--finish-install",
        dest="remote_phase",
        action="store_const",
        const="finish-install",
        help="Phase 3: push the latest bundle to remote and complete the installation",
    )
    parser_remote.set_defaults(remote_phase=None)
