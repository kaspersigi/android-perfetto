# Tracebox 功能配置（Perfetto v58.2）

本项目选择“只发布完整 tracebox”，不额外发布 heapprofd 或 trace_processor。
依据为上游版本的 [GN 参数](https://github.com/google/perfetto/blob/v58.2/gn/perfetto.gni)、
[tracebox 构建目标](https://github.com/google/perfetto/blob/v58.2/src/tracebox/BUILD.gn)
及 [子命令实现](https://github.com/google/perfetto/blob/v58.2/src/tracebox/tracebox.cc)。

## 本项目显式配置

| GN 参数 | 值 | 作用 |
|---|---|---|
| `target_os` / `target_cpu` | `android` / `arm64` | Android ARM64 独立构建 |
| `is_debug` | `false` | 优化 Release，不是 Debug |
| `android_api_level` | `32` | Android 12L / API 32 起；不是“Android 32” |
| `enable_perfetto_platform_services` | `true` | traced 服务和命令行客户端 |
| `enable_perfetto_ipc` | `true` | 采集服务、producer、consumer 通信 |
| `enable_perfetto_traced_probes` | `true` | ftrace、进程、系统统计等探针 |
| `enable_perfetto_traced_relay` | `true` | 跨设备转发 |
| `enable_perfetto_traced_perf` | `true` | linux.perf 计数/采样/调用栈 producer |
| `enable_perfetto_heapprofd` | `true` | 允许构建上游 heapprofd 相关目标，不会自动放入 tracebox |
| `enable_perfetto_zlib` / `enable_perfetto_zstd` | `true` | 两种 trace 压缩格式 |
| `enable_perfetto_re2` | `true` | 上游 standalone 默认使用的正则库 |

多数功能在这组平台/API 参数下原本就默认开启；显式列出便于审阅、升级和验证。
开启压缩支持不代表每次采集自动压缩，仍需在采集配置中选择。
其他运行机制（watchdog、锁、task runner 等）保持上游默认，不为了“全功能”
修改算法开关或添加 Debug 开销。

## 单文件中实际包含什么

v58.2 的 applet 表包含：`traced`、`traced_probes`、`traced_relay`、
`traced_perf`、`perfetto`、`trigger_perfetto`、`websocket_bridge`。
不含 `heapprofd`。无 applet 参数的自动启动模式会启动 traced、traced_probes
和 traced_perf；relay 和 websocket bridge 需要单独调用，不会默认暴露网络服务。

GN 开关决定“编译进去哪些能力”，采集配置（pbtxt）决定“这一次采什么”。例如：

- `linux.ftrace`：调度、频率、内核事件；具体事件取决于设备内核。
- `linux.process_stats` / `linux.sys_stats`：进程和系统统计。
- `linux.perf`：perf 计数、CPU 采样与调用栈。
- Android 专用数据源：可能依赖系统服务、平台库、属性或外部 producer。

不能通过全部打开 GN 开关绕过 SELinux、perf_event 权限、内核支持、应用
profileable/debuggable 限制，也不能把系统独有的 producer 自动复制进单文件。
构建验证和运行时数据源验证必须分开。

## 为什么没有把所有 enable_* 都设为 true

| 其他开关/组件 | 不作为本项目“全功能”的原因 |
|---|---|
| trace_processor 的 SQLite、JSON、HTTP、Winscope 等 | 离线分析功能，不是 tracebox applet |
| `enable_perfetto_llvm_symbolizer` / ETM importer | 分析器功能，有额外平台/工具链约束 |
| `enable_perfetto_grpc` | 主要用于 BigTrace，引入大型依赖，不增强本项目采集 |
| `enable_perfetto_ui` / `enable_perfetto_site` | Web UI 和网站是单独构建产物 |
| tests / benchmarks / fuzzers / Java SDK | 测试、开发或 SDK 目标，不会进入 tracebox |
| `enable_perfetto_pcre2` | 不把平台默认 regex 后端与 standalone 的 RE2 机械叠加 |
| heapprofd | 独立 daemon，Android allocator hook/client 还依赖系统集成 |

尤其是 Native 堆采样：上游 `enable_perfetto_heapprofd=true` 只是令对应构建
目标可达。要使用独立 heapprofd，还要处理客户端、socket、系统服务和权限的
配合，不是把这个值设为 true 就完成了。本轮按选择不增加其构建或发布。

## 查看上游所有可用参数

成功准备并生成构建目录后，可只读查看当前版本的参数及说明：

```sh
cd sources/perfetto-v58.2
tools/gn args out/android_arm64_release --list
tools/gn args out/android_arm64_release --list=enable_perfetto_traced_perf
tools/gn desc out/android_arm64_release //src/tracebox:tracebox deps --all
```

长期配置始终修改仓库的 `config/args.gn`，不要编辑缓存输出目录。升级版本后
参数是否仍有效由 `gn gen --fail-on-unused-args`、生成 build flags 和链接图检查
共同验证；Android 实机上的实际数据源注册、采集内容需要另行测试。
