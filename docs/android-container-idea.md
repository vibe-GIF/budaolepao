# 后续设想：Android 容器运行后端

**状态：仅为设想，未实现、未验证，不属于当前交付范围。当前主方案仍是 Windows + MuMu。**

## 想法

后续可研究在 Android 容器中安装并运行测试 APK，由同一套网页和 MCP 控制任务、查看状态，
并通过 Android 的测试定位能力验证位置相关功能。届时可把设备连接、App 启动、截图、
输入和定位操作抽象为运行后端，保留现有 MuMu 实现。

## 候选与限制

[Waydroid](https://docs.waydro.id/) 在 GNU/Linux 上通过容器运行完整 Android 系统，
是一个可以研究的候选。它依赖 Linux，不能直接替换当前 Windows 下的 MuMu 可执行程序。
在 Windows 环境中还需评估 Linux 运行环境、图形加速、网络及 APK 的 ABI 兼容性。
[官方虚拟机说明](https://docs.waydro.id/faq/get-waydroid-to-work-through-a-vm)也列出了图形相关限制。

Android 的标准测试定位接口会把位置标为 mock，应用能够通过 `Location.isMock()` 识别。
因此，能够启动 APK 或改变测试位置，并不能证明该 App 接受位置数据或认可任何记录。
参考 [Android LocationManager 文档](https://developer.android.com/reference/android/location/LocationManager#setTestProviderLocation(java.lang.String,%20android.location.Location))。

## 在进一步实施前需要验证

- 目标 APK 是否能安装、启动并完成必要的人工操作。
- 图形、架构、系统服务和测试定位接口是否兼容。
- 是否能稳定获取截图、执行输入并报告可验证的设备状态。
- 相比 MuMu，部署、维护和资源开销是否有实际优势。

本次 PR 不安装容器、不提供容器控制入口，也不声称已具备定位兼容性或平台结果验证能力。
