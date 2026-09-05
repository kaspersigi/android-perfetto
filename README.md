# android-perfetto

从指定 [Perfetto release tag](https://github.com/google/perfetto/releases) 构建
Android ARM64 Release `tracebox`，保留上游 tracebox 可用的采集服务，显式开启
`linux.perf`、Zlib/Zstd 压缩等功能。不依赖 AOSP、Soong 或本地 Android 源码树。

## 一键构建

主机：Linux x86_64，Python 3.11+。Ubuntu 安装主机依赖：

```sh
sudo apt install python3 make git curl ca-certificates tar xz-utils unzip binutils
make release
```

默认版本在 [sources.lock](sources.lock) 中配置为 `v58.2`；默认并行数为
`nproc`。首次构建需要联网下载源码、工具链及上游依赖/测试数据，下载量和
磁盘占用较大。后续会复用上游安装脚本的缓存，但仍需联网核验源 tag。

```sh
make release JOBS=4
python3 scripts/build-tracebox.py --jobs 4

# 临时选择另一版本；不会修改 sources.lock。
python3 scripts/build-tracebox.py --version v58.2 --jobs 4

make prepare  # 只准备源码和 Android 构建依赖
make verify   # 不下载、不编译，验证 dist
make test     # 运行本仓库的离线单元测试
```

长期升级时只需修改 `sources.lock` 的 `perfetto` 字段，再运行 `make release`。
脚本先解析官方 tag 对应的确切 commit，再浅克隆/复用该版本源码；如 tag 与
缓存不一致或源码被修改，会停止，不会 reset、覆盖你的源码改动或偷偷使用旧版本。
未来上游版本若改变 GN 参数、构建布局或依赖，仍可能需要适配。

产物：

- `dist/tracebox`：从上游 `out/android_arm64_release/stripped/tracebox` 导出。
- `dist/build-info.json`：实际版本、commit、GN 参数、工具链和产物 SHA256。
- `dist/SHA256SUMS`：二进制校验值。

这是 Android ARM64 的已 strip PIE，使用系统 `linker64` 和 Android 系统库，
**不是 android-memleak 那种全静态程序**。下载或构建失败不会替换上次成功产物。
构建验证还会检查 GN 生成的 profiling 开关和 `traced_perf_main` 的链接依赖。

## 自定义 GN 参数与工具链

统一编辑 [config/args.gn](config/args.gn)，不再手动修改下载源码中的 `args.gn`。
脚本在 `gn gen` **之前**写入参数，开启 `--fail-on-unused-args`，避免失效参数
被静默忽略。配置采用单行字面量赋值；脚本校验本项目承诺的 ARM64、API 32、
Release 和采集功能参数，额外参数仍由 GN 检查。

完整功能说明见 [构建配置与功能边界](docs/build-configuration.md)。
“全功能”指 **上游 tracebox 的完整采集能力**，不代表每个 Android 设备都能
使用所有数据源，也不代表把 Perfetto UI、trace_processor、heapprofd 打进同一文件。

工具链遵循上游 [独立构建流程](https://perfetto.dev/docs/contributing/build-instructions)：
`tools/install-build-deps --android --no-dev-tools` → `tools/gn` → `tools/ninja`。
`--no-dev-tools` 仅跳过开发工具（如 Python 开发虚拟环境），保留构建依赖。
v58.2 固定使用 NDK r26c；GN 配置还与该 NDK 的 Clang runtime 布局绑定，
因此不强行复用 android-memleak 的 r27d，也不使用 Actions runner 上的默认 NDK。

## 项目结构

```text
sources.lock                上游 release tag（JSON）
config/args.gn              本项目统一 GN 配置
Makefile                    release / prepare / verify / test
scripts/build-tracebox.py    拉取、安装依赖、构建、验证与导出
scripts/publish-release.sh   上传草稿、回读验证、正式发布
tests/                      本仓库脚本的离线测试
docs/                       功能配置说明
.github/workflows/          tag 发布工作流
sources/                    下载的上游源码及工具链（忽略）
build/                      构建互斥锁和导出暂存（忽略）
dist/                       最终构建输出（忽略）
```

GN/Ninja 输出留在上游源码的 `out/` 下，以兼容官方工具的路径约定；整个
`sources/` 不进 Git。结构与 android-memleak 对齐，不为 GN 项目增加无用的
CMake、AOSP 或补丁目录。当前构建无需修改上游源码。

## GitHub Actions 发布

仅推送项目 `v*` tag 时触发：Ubuntu 26.04，Ninja **4 并行**，构建 Release。
普通提交和 PR 不触发。工作流先执行脚本测试、构建与验证，再发布：

- GitHub Release **只上传原始可执行文件 `tracebox`**，不打压缩包。
- `build-info.json`、`SHA256SUMS` 只随内部 Actions artifact 保留 1 天。
- 构建 job 仅有读取权限；发布 job 才有 `contents: write`。
- 发布先创建草稿，下载回读确认字节一致后才公开。不会覆盖已公开的 Release；
  失败草稿可通过重新运行同一次工作流恢复。
- 使用仓库自动提供的 `GITHUB_TOKEN`，无需 Android 签名密钥或额外 secrets。

项目 tag（例如 `v1.0.0`）与上游版本（例如 `v58.2`）独立。发布前先提交代码
和配置，再创建、推送项目 tag。Ubuntu 26.04 runner 使用
[GitHub 官方镜像](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2604-Readme.md)。

下载到设备后需要 `chmod 0755 tracebox`。实际采集方式、权限及功能边界见文档；
CI 编译成功不等于已在实机验证 perf 采样或堆分析。
