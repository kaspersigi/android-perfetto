# android-perfetto

Build a Release `tracebox` for Android ARM64 from a specified
[Perfetto release tag](https://github.com/google/perfetto/releases). The build
retains upstream tracebox collection services and explicitly enables
`linux.perf` and Zlib/Zstd compression. It requires no AOSP checkout, Soong,
or local Android source tree.

## Quick build

Build host: Linux x86_64 with Python 3.11+. Install the Ubuntu host dependencies:

```sh
sudo apt install python3 make git curl ca-certificates tar xz-utils unzip binutils
make release
```

[sources.lock](sources.lock) defaults to `v58.2`; parallelism defaults to `nproc`.
The first build downloads sources, toolchains, and upstream dependencies/test
data, requiring substantial network traffic and disk space. Later builds reuse
the upstream installer's cache but still need network access to verify the tag.

```sh
make release JOBS=4
python3 scripts/build-tracebox.py --jobs 4

# Select a version for this invocation without editing sources.lock.
python3 scripts/build-tracebox.py --version v58.2 --jobs 4

make prepare  # Prepare sources and Android build dependencies only.
make verify   # Verify dist without downloading or compiling.
make test     # Run this repository's offline unit tests.
```

For a persistent version change, edit the `perfetto` field in `sources.lock`
and run `make release`. The script resolves the official tag to an exact commit
before shallow-cloning or reusing sources. A tag/cache mismatch or modified
source tree stops the build; the script does not reset local changes or
silently use an older version. Future changes to upstream GN options, build
layout, or dependencies may require adaptation.

Outputs:

- `dist/tracebox`: exported from upstream's
  `out/android_arm64_release/stripped/tracebox`.
- `dist/build-info.json`: resolved version and commit, GN arguments, toolchain
  information, and output SHA-256.
- `dist/SHA256SUMS`: executable checksum.

The output is a stripped Android ARM64 PIE using the system `linker64` and
Android system libraries. **It is not a fully static executable like
android-memleak.** Download or build failures leave the last successful outputs
intact. Validation also checks GN-generated profiling options and the link
dependencies of `traced_perf_main`.

## GN configuration and toolchain

Edit [config/args.gn](config/args.gn), rather than `args.gn` in downloaded sources.
The script writes the arguments **before** `gn gen` and enables
`--fail-on-unused-args`, preventing obsolete options from being silently ignored.
Configuration uses single-line literal assignments. The script validates the
required ARM64, API 32, Release, and collection options; GN checks additional
arguments.

See [build configuration and feature limits](docs/build-configuration.md).
"Full functionality" refers to **upstream tracebox's collection capabilities**.
It does not mean every Android device supports every data source, or that
Perfetto UI, trace_processor, and heapprofd are bundled into the same executable.

The toolchain follows upstream's
[standalone build process](https://perfetto.dev/docs/contributing/build-instructions):
`tools/install-build-deps --android --no-dev-tools` → `tools/gn` → `tools/ninja`.
`--no-dev-tools` skips development tools, such as the Python development virtual
environment, while retaining build dependencies. v58.2 pins NDK r26c, and its GN
configuration depends on that NDK's Clang runtime layout. The build therefore
uses upstream's toolchain rather than another project's NDK or the Actions
runner's default NDK.

## Project layout

```text
sources.lock                Upstream release tag (JSON)
config/args.gn              Project GN configuration
Makefile                    release / prepare / verify / test
scripts/build-tracebox.py    Fetch, install dependencies, build, verify, export
scripts/publish-release.sh   Upload draft, download and verify, publish
tests/                      Offline tests for repository scripts
docs/                       Feature and configuration documentation
.github/workflows/          Tag-triggered release workflow
sources/                    Downloaded upstream sources and toolchains (ignored)
build/                      Build lock and export staging (ignored)
dist/                       Final build outputs (ignored)
```

GN/Ninja outputs stay in the upstream source tree's `out/` directory to match
official tool path conventions. All of `sources/` is excluded from Git.
The layout follows android-memleak where applicable, without adding unused
CMake, AOSP, or patch directories. The current build needs no upstream source
modifications.

## GitHub Actions releases

Pushing a project `v*` tag triggers a Release build on Ubuntu 26.04 with
**4 parallel Ninja jobs**. Ordinary commits and pull requests do not trigger it.
The workflow runs script tests, builds, and validates before publication:

- GitHub Releases upload **only the raw `tracebox` executable**, without an
  archive wrapper.
- `build-info.json` and `SHA256SUMS` travel in a temporary Actions artifact.
  The artifact is deleted after publication and asset digest verification;
  its one-day retention is a fallback if publication or cleanup fails.
- The build job has read-only access. The publish job has `contents: write`
  for releases and `actions: write` for artifact cleanup.
- Publication starts with a draft and verifies downloaded bytes before making
  it public. Published releases are not overwritten. Failed drafts can be
  recovered by rerunning the workflow while artifacts are available; after
  cleanup, rebuild to rerun.
- The workflow uses the automatically provided `GITHUB_TOKEN`; no Android
  signing keys or extra secrets are required.

Project tags, such as `v1.0.0`, are independent of upstream versions, such as
`v58.2`. Commit code and configuration before creating and pushing a project
tag. The Ubuntu 26.04 runner uses
[GitHub's official image](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2604-Readme.md).

After downloading the executable to a device, run `chmod 0755 tracebox`.
See the documentation for collection methods, permissions, and feature limits.
A successful CI build does not establish that perf sampling or heap analysis
has been validated on a physical device.

## License

Unless an individual file states otherwise, repository-owned build scripts,
configuration, tests, and documentation are licensed under the Apache License
2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Upstream Perfetto and downloaded third-party dependencies retain their own
licenses and attribution notices. For the default version in
[sources.lock](sources.lock), see the [Perfetto v58.2 license](https://github.com/google/perfetto/blob/v58.2/LICENSE),
including its file-specific exceptions. When changing the upstream version,
consult the license files for that release and its dependencies. The generated
`tracebox` incorporates upstream code; the repository's top-level license does
not replace the applicable component licenses or notices.
