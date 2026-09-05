import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("builder", PROJECT / "scripts/build-tracebox.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def elf_fixture():
    data = bytearray(512)
    data[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<HHIQQQIHHHHHH", data, 16,
                     3, 183, 1, 0, 64, 384, 0, 64, 56, 2, 64, 1, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0, 0, 512, 512, 4096)
    interpreter = b"/system/bin/linker64\0"
    struct.pack_into("<IIQQQQQQ", data, 120, 3, 4, 200, 200, 0,
                     len(interpreter), len(interpreter), 1)
    data[200:200 + len(interpreter)] = interpreter
    markers = b"linux.perf\0traced_perf\0"
    data[250:250 + len(markers)] = markers
    return data


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        shutil.copyfile(PROJECT / "config/args.gn", self.root / "config/args.gn")
        (self.root / "sources.lock").write_text('{"perfetto": "v58.2"}\n')
        patcher = mock.patch.object(builder, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        links = mock.patch.object(builder, "verify_dynamic_links", return_value=["libc.so"])
        links.start()
        self.addCleanup(links.stop)
        self.binary = self.root / "tracebox"
        self.binary.write_bytes(elf_fixture())

    def test_release_tags(self):
        for version in ("v58.2", "v59.0", "v1.2.3"):
            self.assertEqual(builder.validate_tag(version), version)
        for version in ("latest", "main", "../v58.2", "--help", "v58.2/x", "v58.2-rc1", None):
            with self.subTest(version=version), self.assertRaises(builder.BuildError):
                builder.validate_tag(version)

    def test_jobs(self):
        self.assertEqual(builder.positive_jobs("4"), 4)
        for value in ("0", "-1", "four", "1.5"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                builder.positive_jobs(value)

    def test_default_uses_nproc(self):
        with mock.patch.object(builder.subprocess, "check_output", return_value="12\n") as command:
            self.assertEqual(builder.default_jobs(), 12)
            command.assert_called_once_with(["nproc"], text=True)

    def test_annotated_and_lightweight_tags(self):
        ref = "refs/tags/v58.2"
        for lines, expected in ((f"{'a'*40}\t{ref}\n{'b'*40}\t{ref}^{{}}\n", "b"*40),
                                (f"{'a'*40}\t{ref}\n", "a"*40)):
            with mock.patch.object(builder, "run", return_value=lines):
                self.assertEqual(builder.resolve_commit("v58.2"), expected)

    def test_missing_or_invalid_tag_ref(self):
        for lines in ("", "bad refs/tags/v58.2", "a"*40 + " refs/heads/v58.2"):
            with mock.patch.object(builder, "run", return_value=lines), self.assertRaises(builder.BuildError):
                builder.resolve_commit("v58.2")

    def test_default_gn_config(self):
        self.assertIn("enable_perfetto_traced_perf = true", builder.read_args())

    def test_bad_gn_configs(self):
        original = builder.read_args()
        for content in (original.replace("is_debug = false", "is_debug = true"),
                        original.replace("android_api_level = 32", "android_api_level = 21"),
                        original.replace("enable_perfetto_traced_perf = true", ""),
                        original + '\ntarget_cpu = "x64"\n',
                        original + '\nimport("elsewhere.gni")\n'):
            (self.root / "config/args.gn").write_text(content)
            with self.subTest(content=content), self.assertRaises(builder.BuildError):
                builder.read_args()

    def test_extra_literal_gn_argument(self):
        config = self.root / "config/args.gn"
        config.write_text(config.read_text() + "\nis_lto = true\n")
        self.assertIn("is_lto = true", builder.read_args())

    def test_elf_valid(self):
        artifact = builder.verify_elf(self.binary)
        self.assertTrue(artifact["pie"])
        self.assertEqual(artifact["architecture"], "aarch64")
        self.assertEqual(artifact["sha256"], builder.sha256(self.binary))

    def test_elf_rejects_truncation(self):
        data = elf_fixture()
        for size in (0, 20, 63, 120, 511):
            self.binary.write_bytes(data[:size])
            with self.subTest(size=size), self.assertRaises(builder.BuildError):
                builder.verify_elf(self.binary)

    def test_elf_rejects_wrong_architecture_type_and_layout(self):
        for offset, fmt, value in ((4, "B", 1), (5, "B", 2), (16, "H", 2),
                                   (18, "H", 62), (54, "H", 10), (56, "H", 0),
                                   (60, "H", 0), (112, "Q", 1024), (388, "I", 2)):
            data = elf_fixture()
            struct.pack_into("<" + fmt, data, offset, value)
            self.binary.write_bytes(data)
            with self.subTest(offset=offset), self.assertRaises(builder.BuildError):
                builder.verify_elf(self.binary)

    def test_elf_requires_android_loader_and_perf(self):
        for position in (200, 250, 261):
            data = elf_fixture()
            data[position] = ord("x")
            self.binary.write_bytes(data)
            with self.subTest(position=position), self.assertRaises(builder.BuildError):
                builder.verify_elf(self.binary)

    def test_generated_features(self):
        header = self.root / "gen/build_config/perfetto_build_flags.h"
        header.parent.mkdir(parents=True)
        flags = "".join(f"#define PERFETTO_BUILDFLAG_DEFINE_{name}() (1)\n"
                        for name in builder.BUILD_FEATURES)
        header.write_text(flags)
        builder.verify_features(self.root)
        header.write_text(flags.replace("(1)", "(0)", 1))
        with self.assertRaises(builder.BuildError):
            builder.verify_features(self.root)

    def manifest(self):
        return {"source": {"version": "v58.2", "commit": "a"*40},
                "gn_args": builder.read_args(), "artifact": builder.verify_elf(self.binary)}

    def test_export_and_verify(self):
        (self.root / "build").mkdir()
        builder.publish_dist(self.binary, self.manifest())
        self.assertEqual(builder.verify_dist()["artifact"]["sha256"], builder.sha256(self.binary))
        self.assertEqual((self.root / "dist/tracebox").stat().st_mode & 0o777, 0o755)

    def test_tampering_is_detected(self):
        (self.root / "build").mkdir()
        for name in ("tracebox", "SHA256SUMS", "build-info.json"):
            builder.publish_dist(self.binary, self.manifest())
            path = self.root / "dist" / name
            if name == "build-info.json":
                manifest = self.manifest()
                manifest["gn_args"] = ""
                path.write_text(json.dumps(manifest))
            else:
                path.write_bytes(path.read_bytes() + b"tamper")
            with self.subTest(name=name), self.assertRaises(builder.BuildError):
                builder.verify_dist()

    def test_failed_export_preserves_previous_outputs(self):
        (self.root / "build").mkdir()
        good = self.manifest()
        builder.publish_dist(self.binary, good)
        self.binary.write_bytes(b"broken")
        with self.assertRaises(builder.BuildError):
            builder.publish_dist(self.binary, good)
        self.assertEqual(builder.verify_dist(), good)

    def test_source_refuses_modified_or_changed_checkout(self):
        (self.root / ".git").mkdir()
        good = [builder.UPSTREAM, "a"*40, "a"*40, ""]
        with mock.patch.object(builder, "run", side_effect=good):
            builder.validate_source(self.root, "v58.2", "a"*40)
        for values in ([builder.UPSTREAM, "b"*40, "a"*40],
                       ["https://example.com/other.git", "a"*40, "a"*40],
                       [builder.UPSTREAM, "a"*40, "a"*40, " M BUILD.gn"]):
            with mock.patch.object(builder, "run", side_effect=values), self.assertRaises(builder.BuildError):
                builder.validate_source(self.root, "v58.2", "a"*40)

    def test_failed_clone_leaves_no_prepared_source(self):
        with mock.patch.object(builder, "run", side_effect=subprocess.CalledProcessError(1, "git")):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.prepare_source("v58.2", "a"*40)
        self.assertEqual(list((self.root / "sources").iterdir()), [])

    def test_build_lock_rejects_concurrent_writer(self):
        with builder.build_lock(), self.assertRaises(builder.BuildError):
            with builder.build_lock():
                pass

    def test_verify_mode_does_not_download(self):
        with mock.patch.object(builder, "verify_dist") as verify, mock.patch.object(builder, "run") as command:
            self.assertEqual(builder.main(["--verify-only"]), 0)
            verify.assert_called_once_with()
            command.assert_not_called()

    def test_prepare_mode_forwards_version_and_skips_build(self):
        source = self.root / "source"
        with mock.patch.object(builder, "resolve_commit", return_value="a"*40) as resolve, \
             mock.patch.object(builder, "prepare_source", return_value=source), \
             mock.patch.object(builder, "run") as command, \
             mock.patch.object(builder, "default_jobs") as default:
            self.assertEqual(builder.main(["--version", "v59.0", "--jobs", "4", "--prepare-only"]), 0)
            resolve.assert_called_once_with("v59.0")
            default.assert_not_called()
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0][-2:], ["--android", "--no-dev-tools"])

    def test_build_pipeline_orders_configuration_and_exports_only_verified_output(self):
        source = self.root / "source"
        output = source / "out/android_arm64_release"
        (output / "stripped").mkdir(parents=True)
        shutil.copyfile(self.binary, output / "stripped/tracebox")
        (source / "buildtools/ndk").mkdir(parents=True)
        (source / "buildtools/ndk/source.properties").write_text("Pkg.Revision = 26.2.11394342\n")
        (source / "tools").mkdir()
        (source / "tools/install-build-deps").write_text("# fixture\n")
        (output / "gen/build_config").mkdir(parents=True)
        (output / "gen/build_config/perfetto_build_flags.h").write_text("".join(
            f"#define PERFETTO_BUILDFLAG_DEFINE_{name}() (1)\n" for name in builder.BUILD_FEATURES))
        commands = []

        def fake_run(command, *_args, **_kwargs):
            commands.append([str(value) for value in command])
            if "gen" in command:
                self.assertEqual((output / "args.gn").read_text(), builder.read_args())
                self.assertIn("--fail-on-unused-args", command)
            if "desc" in command:
                return "//src/profiling/perf:traced_perf_main\n"
            return "test-tool-version\n"

        with mock.patch.object(builder, "resolve_commit", return_value="a"*40), \
             mock.patch.object(builder, "prepare_source", return_value=source), \
             mock.patch.object(builder, "validate_source"), \
             mock.patch.object(builder, "run", side_effect=fake_run):
            self.assertEqual(builder.main(["--jobs", "4"]), 0)
        self.assertEqual(commands[2][-3:], ["-j", "4", "tracebox"])
        self.assertEqual(builder.verify_dist()["build_jobs"], 4)
        self.assertEqual(sorted(path.name for path in (self.root / "dist").iterdir()),
                         ["SHA256SUMS", "build-info.json", "tracebox"])


class DynamicLinkTests(unittest.TestCase):
    def test_android_system_libraries(self):
        output = " 0x1 (NEEDED) Shared library: [libc.so]\n 0x1 (NEEDED) Shared library: [liblog.so]\n"
        with mock.patch.object(builder.subprocess, "check_output", return_value=output):
            self.assertEqual(builder.verify_dynamic_links(Path("tracebox")), ["libc.so", "liblog.so"])

    def test_private_library_or_missing_dependencies(self):
        for output in ("", "0x1 (NEEDED) Shared library: [libperfetto.so]",
                       "0x1 (NEEDED) Shared library: [libc++_shared.so]",
                       "0x1 (NEEDED) Shared library: [libc.so]\n0xf (RUNPATH) Library runpath: [/private]"):
            with self.subTest(output=output), \
                 mock.patch.object(builder.subprocess, "check_output", return_value=output), \
                 self.assertRaises(builder.BuildError):
                builder.verify_dynamic_links(Path("tracebox"))


class ReleaseTests(unittest.TestCase):
    def test_publish_scenarios_with_fake_gh(self):
        # No credentials or network are used. Record exactly whether the draft
        # is made public; upload failures must never reach `release edit`.
        fake_gh = '''#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
scenario = os.environ['SCENARIO']
with open('calls', 'a') as log:
    log.write(' '.join(args) + '\\n')
action = args[1]
if action == 'view':
    if '--json' not in args:
        sys.exit(1 if scenario == 'new' else 0)
    if 'isDraft' in args:
        print('false' if scenario == 'published' else 'true')
    else:
        print('extra,tracebox' if scenario == 'extra' else 'tracebox')
elif action in ('create', 'upload'):
    sys.exit(1 if scenario == 'upload-failure' else 0)
elif action == 'download':
    if scenario == 'download-failure':
        sys.exit(1)
    destination = pathlib.Path(args[args.index('--dir') + 1]) / 'tracebox'
    destination.write_bytes(b'bad' if scenario == 'corrupt' else pathlib.Path('dist/tracebox').read_bytes())
'''
        for scenario in ("new", "draft", "published", "extra", "corrupt", "upload-failure", "download-failure"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "scripts").mkdir()
                (root / "dist").mkdir()
                (root / "dist/tracebox").write_bytes(b"test release binary")
                shutil.copyfile(PROJECT / "scripts/publish-release.sh", root / "scripts/publish-release.sh")
                gh = root / "gh"
                gh.write_text(fake_gh)
                gh.chmod(0o755)
                env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                           GH_TOKEN="fake", GH_REPO="test/test", GITHUB_REF_NAME="v1.0.0",
                           RUNNER_TEMP=tmp, SCENARIO=scenario)
                result = subprocess.run(["bash", root / "scripts/publish-release.sh"], env=env,
                                        capture_output=True, text=True)
                calls = (root / "calls").read_text()
                success = scenario in ("new", "draft")
                self.assertEqual(result.returncode == 0, success, result.stderr)
                self.assertEqual("release edit v1.0.0 --draft=false" in calls, success)
                if scenario == "published":
                    self.assertNotIn("release upload", calls)
                    self.assertNotIn("release create", calls)
                if scenario == "new":
                    self.assertIn("--draft --verify-tag", calls)


if __name__ == "__main__":
    unittest.main()
