#!/usr/bin/env python3
"""Build the configured Perfetto release's Android ARM64 tracebox with GN."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = "https://github.com/google/perfetto.git"
TAG_RE = re.compile(r"v[0-9]+(?:\.[0-9]+){1,2}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
BUILD_FEATURES = ("PERFETTO_TRACED_PERF", "PERFETTO_HEAPPROFD", "PERFETTO_IPC",
                  "PERFETTO_ZLIB", "PERFETTO_ZSTD", "PERFETTO_RE2")
REQUIRED_ARGS = {
    "target_os": '"android"',
    "target_cpu": '"arm64"',
    "is_debug": "false",
    "android_api_level": "32",
    "enable_perfetto_traced_perf": "true",
    "enable_perfetto_heapprofd": "true",
    "enable_perfetto_platform_services": "true",
    "enable_perfetto_ipc": "true",
    "enable_perfetto_traced_probes": "true",
    "enable_perfetto_traced_relay": "true",
    "enable_perfetto_zlib": "true",
    "enable_perfetto_zstd": "true",
    "enable_perfetto_re2": "true",
}


class BuildError(Exception):
    pass


def run(command, cwd=ROOT, capture=False):
    command = [str(part) for part in command]
    print("+ " + shlex.join(command), flush=True)
    return subprocess.run(command, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_tag(value):
    if not isinstance(value, str) or not TAG_RE.fullmatch(value):
        raise BuildError("Perfetto version must be a release tag such as v58.2")
    return value


def positive_jobs(value):
    try:
        jobs = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("jobs must be a positive integer") from exc
    if jobs < 1:
        raise argparse.ArgumentTypeError("jobs must be a positive integer")
    return jobs


def default_jobs():
    return positive_jobs(subprocess.check_output(["nproc"], text=True).strip())


def resolve_commit(version):
    ref = "refs/tags/" + validate_tag(version)
    output = run(["git", "ls-remote", "--exit-code", UPSTREAM,
                  ref, ref + "^{}"], capture=True)
    refs = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and COMMIT_RE.fullmatch(parts[0]):
            refs[parts[1]] = parts[0]
    if ref not in refs:
        raise BuildError(f"Upstream release tag not found: {version}")
    return refs.get(ref + "^{}", refs[ref])


def validate_source(source, version, commit):
    if source.is_symlink() or not (source / ".git").is_dir():
        raise BuildError(f"Not a managed source checkout: {source}")
    origin = run(["git", "remote", "get-url", "origin"], source, True).strip()
    head = run(["git", "rev-parse", "HEAD"], source, True).strip()
    tag = run(["git", "rev-parse", f"refs/tags/{version}^{{commit}}"],
              source, True).strip()
    if origin != UPSTREAM or head != commit or tag != commit:
        raise BuildError(f"Source/tag mismatch in {source}; refusing to reset it")
    if run(["git", "status", "--porcelain", "--untracked-files=normal"],
           source, True).strip():
        raise BuildError(f"Modified source checkout: {source}; preserve or move it first")


def prepare_source(version, commit):
    source_root = ROOT / "sources"
    source_root.mkdir(exist_ok=True)
    source = source_root / f"perfetto-{version}"
    if not source.exists():
        # Only promote a complete, validated checkout. An interrupted clone
        # never becomes the source directory used by the next invocation.
        with tempfile.TemporaryDirectory(prefix=".clone-", dir=source_root) as tmp:
            checkout = Path(tmp) / "perfetto"
            run(["git", "clone", "--depth", "1", "--branch", version,
                 "--single-branch", UPSTREAM, checkout])
            validate_source(checkout, version, commit)
            checkout.rename(source)
    validate_source(source, version, commit)
    return source


def read_args():
    text = (ROOT / "config/args.gn").read_text()
    # This project's config is deliberately a list of literal assignments,
    # not a second build program. GN remains the authority on argument names.
    values = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r'(\w+)\s*=\s*(true|false|[0-9]+|"[^"\n]*")', line)
        if not match or match[1] in values:
            raise BuildError(f"Expected one literal GN assignment per line: {line}")
        values[match[1]] = match[2]
    for key, expected in REQUIRED_ARGS.items():
        if values.get(key) != expected:
            raise BuildError(f"config/args.gn must set {key} = {expected}")
    return text


def verify_dynamic_links(path):
    output = subprocess.check_output(["readelf", "--dynamic", "--wide", str(path)],
                                     text=True, env=dict(os.environ, LC_ALL="C"))
    libraries = sorted(re.findall(r"\(NEEDED\).*Shared library: \[([^\]]+)\]", output))
    system_libraries = {"libc.so", "libdl.so", "libm.so", "liblog.so", "libz.so", "libandroid.so"}
    if not libraries or set(libraries) - system_libraries:
        raise BuildError(f"tracebox requires unexpected shared libraries: {libraries}")
    if re.search(r"\((?:RPATH|RUNPATH)\)", output):
        raise BuildError("tracebox must not require a private runtime library path")
    return libraries


def verify_elf(path):
    data = path.read_bytes()
    if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
        raise BuildError("tracebox must be a little-endian ELF64 executable")
    fields = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    kind, machine, _, _, phoff, shoff, _, ehsize, phsize, phnum, shsize, shnum, _ = fields
    if kind != 3 or machine != 183 or ehsize != 64:
        raise BuildError("tracebox must be an ARM64 PIE executable")
    if not phnum or phsize != 56 or phoff + phsize * phnum > len(data):
        raise BuildError("Invalid ELF program header table")
    interpreter = None
    alignments = []
    for index in range(phnum):
        ptype, _, offset, _, _, size, _, alignment = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + index * phsize)
        if offset + size > len(data):
            raise BuildError("Truncated ELF segment")
        if ptype == 1:
            alignments.append(alignment)
        elif ptype == 3:
            interpreter = data[offset:offset + size]
    if interpreter != b"/system/bin/linker64\0":
        raise BuildError("Expected Android's /system/bin/linker64 interpreter")
    if not alignments or min(alignments) < 4096:
        raise BuildError("Invalid ELF load alignment")
    if not shnum or shsize != 64 or shoff + shsize * shnum > len(data):
        raise BuildError("Invalid ELF section header table")
    for index in range(shnum):
        if struct.unpack_from("<I", data, shoff + index * shsize + 4)[0] == 2:
            raise BuildError("Expected stripped tracebox (no SHT_SYMTAB)")
    if b"linux.perf\0" not in data or b"traced_perf\0" not in data:
        raise BuildError("tracebox is missing linux.perf / traced_perf markers")
    return {"architecture": "aarch64", "pie": True, "stripped": True,
            "interpreter": "/system/bin/linker64", "page_alignment": min(alignments),
            "needed_libraries": verify_dynamic_links(path),
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def verify_features(output):
    flags = output / "gen/build_config/perfetto_build_flags.h"
    text = flags.read_text()
    for feature in BUILD_FEATURES:
        if not re.search(r"^#define\s+PERFETTO_BUILDFLAG_DEFINE_" + feature +
                         r"\(\)\s+\(1\)\s*$", text, re.M):
            raise BuildError(f"Generated build flag is not enabled: {feature}")


def verify_dist(dist=None):
    dist = dist or ROOT / "dist"
    manifest = json.loads((dist / "build-info.json").read_text())
    actual = verify_elf(dist / "tracebox")
    if actual != manifest.get("artifact"):
        raise BuildError("tracebox does not match build-info.json")
    expected_sum = f"{actual['sha256']}  tracebox\n"
    if (dist / "SHA256SUMS").read_text() != expected_sum:
        raise BuildError("tracebox does not match SHA256SUMS")
    if manifest.get("gn_args") != read_args():
        raise BuildError("Artifact GN configuration differs from config/args.gn")
    validate_tag(manifest["source"]["version"])
    if not COMMIT_RE.fullmatch(manifest["source"]["commit"]):
        raise BuildError("Invalid source commit in build-info.json")
    print(f"Verified: Perfetto {manifest['source']['version']}; Android ARM64 PIE; "
          f"{actual['bytes']} bytes; SHA256 {actual['sha256']}", flush=True)
    return manifest


def publish_dist(binary, manifest):
    # A failed download/build/validation leaves the previous release intact.
    with tempfile.TemporaryDirectory(prefix=".dist-", dir=ROOT / "build") as tmp:
        stage = Path(tmp)
        shutil.copyfile(binary, stage / "tracebox")
        (stage / "tracebox").chmod(0o755)
        (stage / "build-info.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (stage / "SHA256SUMS").write_text(
            f"{manifest['artifact']['sha256']}  tracebox\n")
        verify_dist(stage)
        dist = ROOT / "dist"
        dist.mkdir(exist_ok=True)
        for name in ("tracebox", "build-info.json", "SHA256SUMS"):
            os.replace(stage / name, dist / name)


@contextlib.contextmanager
def build_lock():
    (ROOT / "build").mkdir(exist_ok=True)
    with (ROOT / "build/.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BuildError("Another build/verification is running in this checkout") from exc
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Perfetto release tag; default: sources.lock")
    parser.add_argument("--jobs", type=positive_jobs, help="Ninja parallel jobs; default: nproc")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--prepare-only", action="store_true", help="Fetch source and Android build dependencies")
    modes.add_argument("--verify-only", action="store_true", help="Verify dist without source downloads/toolchain")
    args = parser.parse_args(argv)
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise BuildError("The supported build host is Linux x86_64")
    with build_lock():
        if args.verify_only:
            verify_dist()
            return 0
        version = validate_tag(args.version if args.version is not None else
                               json.loads((ROOT / "sources.lock").read_text())["perfetto"])
        gn_args = read_args()
        jobs = args.jobs if args.jobs is not None else default_jobs()
        commit = resolve_commit(version)
        print(f"Perfetto: {version} ({commit})\nBuild jobs: {jobs}\n"
              "Toolchain: pinned by Perfetto tools/install-build-deps --android", flush=True)
        source = prepare_source(version, commit)
        run([sys.executable, source / "tools/install-build-deps", "--android", "--no-dev-tools"], source)
        if args.prepare_only:
            print(f"Prepared: {source}")
            return 0
        # args.gn is written BEFORE gn gen, never edited by hand inside sources/.
        output = source / "out/android_arm64_release"
        output.mkdir(parents=True, exist_ok=True)
        args_path = output / "args.gn"
        if not args_path.exists() or args_path.read_text() != gn_args:
            args_path.write_text(gn_args)
        run([source / "tools/gn", "gen", output, "--fail-on-unused-args"], source)
        run([source / "tools/ninja", "-C", output, "-j", jobs, "tracebox"], source)
        verify_features(output)
        # Confirm linkage, not just the GN input switch or strings in a binary.
        deps = run([source / "tools/gn", "desc", output,
                    "//src/tracebox:tracebox", "deps", "--all"], source, True)
        if "//src/profiling/perf:traced_perf_main" not in deps.split():
            raise BuildError("tracebox dependency graph does not include traced_perf_main")
        binary = output / "stripped/tracebox"
        manifest = {
            "source": {"repository": UPSTREAM, "version": version, "commit": commit},
            "gn_args": gn_args,
            "build_jobs": jobs,
            "toolchain": {
                "ndk": (source / "buildtools/ndk/source.properties").read_text().strip(),
                "install_build_deps_sha256": sha256(source / "tools/install-build-deps"),
                "gn": run([source / "tools/gn", "--version"], source, True).strip(),
                "ninja": run([source / "tools/ninja", "--version"], source, True).strip(),
            },
            "artifact": verify_elf(binary),
        }
        validate_source(source, version, commit)
        publish_dist(binary, manifest)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BuildError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
