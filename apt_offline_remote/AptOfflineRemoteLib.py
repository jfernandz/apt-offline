import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from apt_offline_core.AptOfflineCoreLib import fetcher
from apt_offline_core.AptOfflineSSHLib import (
    _detect_transfer_method,
    _latest_state,
    _ssh_run,
    _transfer_get,
    _transfer_put,
)

_REMOTE_CACHE = ".cache/apt-offline"



def _run_fetcher(sig_path, bundle_path):
    tmpdir = tempfile.mkdtemp(prefix="apt-offline-")
    saved_cwd = os.getcwd()
    try:
        try:
            fetcher(argparse.Namespace(
                get=sig_path,
                bundle_file=bundle_path,
                socket_timeout=30,
                download_dir=tmpdir,
                cache_dir=None,
                disable_md5check=False,
                num_of_threads=1,
                proxy_host=None,
                proxy_port=None,
                https_cert_file=None,
                https_key_file=None,
                http_basicauth=[],
                disable_cert_check=False,
                deb_bugs=False,
                quiet=False,
            ))
        except SystemExit as e:
            if e.code not in (0, None):
                raise RuntimeError("apt-offline get failed (exit %s)" % e.code)
    finally:
        os.chdir(saved_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _detect_sudo(host):
    result = subprocess.run(
        ["ssh", "-o", "LogLevel=QUIET", host, "id -u"],
        capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip().splitlines()[-1] == "0":
        return []
    return ["sudo"]


def _cleanup_host(work_dir, keep):
    states = sorted(glob.glob(os.path.join(work_dir, "op-*.state")))
    to_remove = states[:-keep] if keep > 0 else states
    for state_path in to_remove:
        ts = os.path.basename(state_path)[len("op-"):-len(".state")]
        for path in [
            os.path.join(work_dir, "op-%s.sig" % ts),
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
        base_dir = "/tmp/apt-offline"
    else:
        base_dir = os.path.expanduser("~/.cache/apt-offline")
    work_dir = os.path.join(base_dir, host)

    phase = args.remote_phase  # "fetch", "download", "finish-install", or None (full)
    keep = args.keep_latest
    clean_remote = args.clean_remote

    # standalone cleanup — no phase, no operation flags
    if (phase is None
            and not args.remote_update
            and not args.remote_upgrade
            and not args.remote_dist_upgrade
            and not args.remote_install_packages
            and (keep is not None or clean_remote)):
        if clean_remote:
            log.msg("==> Cleaning remote cache on %s...\n" % host)
            _ssh_run(host, ["rm", "-rf", _REMOTE_CACHE])
            log.msg("==> Remote cache cleaned for %s\n" % host)
        if keep is not None:
            if not os.path.isdir(work_dir):
                log.msg("==> Nothing to clean locally for %s (work dir does not exist)\n" % host)
            else:
                _cleanup_host(work_dir, keep)
                log.msg("==> Cleaned up local work dir for %s (kept %d)\n" % (host, keep))
        return

    # ------------------------------------------------------------------ phase 2
    if phase == "download":
        if not os.path.isdir(work_dir):
            raise RuntimeError("no local state for %s — run --fetch first" % host)
        state = _latest_state(work_dir)
        ts = state["timestamp"]
        local_sig = os.path.join(work_dir, "op-%s.sig" % ts)
        local_bundle = os.path.join(work_dir, "bundle-%s.zip" % ts)
        log.msg("==> Creating bundle from %s...\n" % local_sig)
        _run_fetcher(local_sig, local_bundle)
        log.success("Bundle created: %s\n" % local_bundle)
        return

    # ------------------------------------------------------------------ phase 3
    if phase == "finish-install":
        if not os.path.isdir(work_dir):
            raise RuntimeError("no local state for %s — run --fetch first" % host)
        state = _latest_state(work_dir)
        sudo_prefix = _detect_sudo(host)
        ts = state["timestamp"]
        local_bundle = os.path.join(work_dir, "bundle-%s.zip" % ts)
        remote_bundle = "%s/bundle-%s.zip" % (_REMOTE_CACHE, ts)
        _ssh_run(host, ["mkdir", "-p", _REMOTE_CACHE])
        log.msg("==> Detecting transfer method...\n")
        transfer = _detect_transfer_method(host)
        log.msg("==> Using %s for file transfers\n" % transfer)
        log.msg("==> [1/3] Sending bundle to %s...\n" % host)
        _transfer_put(transfer, host, local_bundle, remote_bundle)
        log.msg("==> [2/3] Installing bundle on %s...\n" % host)
        _ssh_run(host, sudo_prefix + ["apt-offline", "install", remote_bundle])
        log.msg("==> [3/3] Running apt-get on %s...\n" % host)
        apt_env = ["env", "DEBIAN_FRONTEND=noninteractive"]
        apt_opts = ["-y", "-o", "Dpkg::Options::=--force-confold"]
        if state["dist_upgrade"]:
            _ssh_run(host, sudo_prefix + apt_env + ["apt-get", "dist-upgrade"] + apt_opts)
        elif state["upgrade"]:
            _ssh_run(host, sudo_prefix + apt_env + ["apt-get", "upgrade"] + apt_opts)
        elif state["install_packages"]:
            _ssh_run(host, sudo_prefix + apt_env + ["apt-get", "install"] + apt_opts + state["install_packages"])
        log.success("Operation completed successfully for %s\n" % host)
        if args.reboot:
            log.msg("==> Rebooting %s...\n" % host)
            _ssh_run(host, sudo_prefix + ["reboot"])
        return

    # --------------------------------------------------- phase 1 / full pipeline
    if (not args.remote_update
            and not args.remote_upgrade
            and not args.remote_dist_upgrade
            and not args.remote_install_packages):
        log.err("At least one of --update, --upgrade, --dist-upgrade or --install-packages must be specified\n")
        raise SystemExit(1)

    os.makedirs(work_dir, exist_ok=True)

    sudo_prefix = _detect_sudo(host)
    timestamp = int(time.time())
    remote_sig = "%s/op-%s.sig" % (_REMOTE_CACHE, timestamp)
    remote_bundle = "%s/bundle-%s.zip" % (_REMOTE_CACHE, timestamp)
    local_sig = os.path.join(work_dir, "op-%s.sig" % timestamp)
    local_bundle = os.path.join(work_dir, "bundle-%s.zip" % timestamp)
    local_state = os.path.join(work_dir, "op-%s.state" % timestamp)
    _ssh_run(host, ["mkdir", "-p", _REMOTE_CACHE])

    log.msg("==> Detecting transfer method...\n")
    transfer = _detect_transfer_method(host)
    log.msg("==> Using %s for file transfers\n" % transfer)

    _ssh_run(host, sudo_prefix + ["mkdir", "-p", "/var/lib/apt/lists/partial"])

    steps = 2 if phase == "fetch" else 5
    log.msg("==> [1/%d] Generating signature on %s...\n" % (steps, host))
    set_cmd = sudo_prefix + ["apt-offline", "set", remote_sig]
    if args.remote_install_packages:
        set_cmd += ["--install-packages"] + args.remote_install_packages
    if args.remote_update:
        set_cmd += ["--update"]
    if args.remote_upgrade:
        set_cmd += ["--upgrade"]
    if args.remote_dist_upgrade:
        set_cmd += ["--upgrade", "--upgrade-type", "dist-upgrade"]
    _ssh_run(host, set_cmd)

    log.msg("==> [2/%d] Fetching signature...\n" % steps)
    _transfer_get(transfer, host, remote_sig, local_sig)

    with open(local_state, "w") as f:
        json.dump({
            "timestamp": timestamp,
            "update": args.remote_update,
            "upgrade": args.remote_upgrade,
            "dist_upgrade": args.remote_dist_upgrade,
            "install_packages": args.remote_install_packages or [],
        }, f, indent=2)

    if phase == "fetch":
        log.success("Signature fetched: %s\n" % local_sig)
        log.msg("==> Run 'apt-offline remote %s --download' to create the bundle when online\n" % host)
        return

    log.msg("==> [3/5] Creating bundle locally...\n")
    _run_fetcher(local_sig, local_bundle)

    log.msg("==> [4/5] Sending bundle to remote...\n")
    _transfer_put(transfer, host, local_bundle, remote_bundle)

    log.msg("==> [5/5] Installing bundle on remote...\n")
    _ssh_run(host, sudo_prefix + ["apt-offline", "install", remote_bundle])

    apt_env = ["env", "DEBIAN_FRONTEND=noninteractive"]
    apt_opts = ["-y", "-o", "Dpkg::Options::=--force-confold"]
    if args.remote_dist_upgrade:
        _ssh_run(host, sudo_prefix + apt_env + ["apt-get", "dist-upgrade"] + apt_opts)
    elif args.remote_upgrade:
        _ssh_run(host, sudo_prefix + apt_env + ["apt-get", "upgrade"] + apt_opts)
    elif args.remote_install_packages:
        _ssh_run(host, sudo_prefix + apt_env + ["apt-get", "install"] + apt_opts + args.remote_install_packages)

    log.success("Operation completed successfully for %s\n" % host)

    if args.reboot:
        log.msg("==> Rebooting %s...\n" % host)
        _ssh_run(host, sudo_prefix + ["reboot"])


def _read_hosts_file(path):
    hosts = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
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
    if args.remote_phase is not None and (args.keep_latest is not None or args.clean_remote):
        log.err("--keep-latest and --clean-remote cannot be combined with --fetch, --download or --finish-install\n")
        sys.exit(1)
    if args.remote_phase in ("download", "finish-install") and (
            args.remote_update
            or args.remote_upgrade
            or args.remote_dist_upgrade
            or args.remote_install_packages):
        log.err("--update, --upgrade, --dist-upgrade and --install-packages cannot be combined with --download or --finish-install (operation was already saved by --fetch)\n")
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
        help="Local base directory for sig/bundle files (default: ~/.cache/apt-offline)",
        action="store",
        type=str,
        default=None,
        metavar="DIR",
    )
    work_dir_group.add_argument(
        "--temp",
        dest="temp",
        help="Use /tmp/apt-offline as the local working directory",
        action="store_true",
        default=False,
    )

    parser_remote.add_argument(
        "--keep-latest",
        dest="keep_latest",
        help="Keep only the N most recent local operations per host (default: 1 if flag is given)",
        nargs="?",
        const=1,
        default=None,
        type=int,
        metavar="N",
    )

    parser_remote.add_argument(
        "--clean-remote",
        dest="clean_remote",
        help="Remove ~/.cache/apt-offline on the remote host(s)",
        action="store_true",
        default=False,
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
