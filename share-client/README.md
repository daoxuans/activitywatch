# ActivityWatch 汇总分享客户端

本目录位于 ActivityWatch 源码仓库的 `share-client/`，是基于本机 ActivityWatch 事件的独立客户端组件。最终桌面发行版以 Tauri 为架构；目前本组件仅随 Tauri 测试包分发，**尚未接入 Tauri 界面或自动调度**，也不会随 ActivityWatch 自动启动。原型源码仍保留在独立项目 `activitywatch-summary-client`。用于 Windows 单文件构建的入口是 [launcher.py](launcher.py)，使用者电脑不需要安装 Rust。

一台 Windows 电脑上的可见、主动开启的 ActivityWatch 汇总分享程序。它读取本机前台窗口、离开状态和浏览器事件，按北京时间汇总软件、网站域名和类别的使用时长，再将**汇总**发往电脑使用者确认过的 HTTPS 地址。

本项目目前只实现客户端。开发机没有运行 ActivityWatch，也没有配置云端接收地址；真实 Windows 采集、Cloudflare 接收、微信查看和记账功能尚未联调或实现。

## GitHub Windows 构建

仓库的 [Windows Tauri summary client build](../.github/workflows/windows-tauri-summary-build.yml) 在相关文件推送到 `master` 时运行，也可手动触发。在 GitHub Windows 构建机上测试本组件、生成 `aw-share.exe`，由构建机安装 Rust、Python、Node 等工具编译 ActivityWatch Tauri 版。本测试包包含窗口和离开状态采集器，不包含可选的 `aw-watcher-input`、`aw-notify`；域名仍需另装浏览器扩展。ZIP 和未签名的安装版 EXE 作为该次运行的 Artifacts 保存 14 天；开发电脑和使用者电脑都不必安装 Rust。构建版本从仓库版本号与提交号生成，不需要把上游标签推到 fork。测试包关闭上游自动更新，不推送版本标签、不发布 Release，也不预置上报地址或令牌。旧的 [Classic/Qt 测试工作流](../.github/workflows/windows-summary-build.yml) 保留作回退，不是本项目的最终桌面架构。

构建流程需要先提交到有权限的 GitHub 仓库；手动触发时工作流文件须存在于该仓库的默认分支。目前**尚未完成云端 Tauri 构建验证**。构建会对锁定的 Tauri 子项目应用一处明确的 [Windows 便携目录模块发现补丁](../scripts/patches/aw-tauri-windows-portable.patch)；若上游源码变化导致补丁不再适用，CI 会失败而非静默沿用。原有 Release 工作流各作业已限定为仅在官方仓库运行，合并上游变更后仍需复核这一限制。Tauri 安装包与 Classic/Qt 使用不同的安装身份和目录，但可能与**已有的 Tauri 版**冲突；首次试用宜先用便携 ZIP。包内的 `share-client/aw-share.exe` 需要电脑使用者单独开启并明确授权，当前不会自动启动或接入托盘。CI 会在临时安装目录启动隔离的 Tauri 服务，检查本机 API 和窗口／离开状态采集器注册；它不测试图形界面、真实前台时长、浏览器扩展或 HTTPS 接收端，首次部署仍须在使用者本人设备验证。

## 汇总口径与隐私边界

- 仅计算前台窗口与 ActivityWatch `not-afk` 的重叠时间。浏览器域名是浏览器前台时间的下钻视图，不与软件时间相加；浏览器有域名覆盖时按域名类别归类，而非按浏览器软件类别归类。
- “时长”是**键鼠未闲置期间的前台使用估算**，不能证明人一直盯着屏幕。无键鼠操作的听课/视频可能被低估；本版没有把有声播放自动当作观看。
- 上传类别合计、软件名称及各自时长、网站**域名/主机名**及各自时长；不上传窗口标题、完整网址、查询参数、文件路径或逐条活动时间线。默认类别为“未分类”，规则可由电脑使用者在本机配置。
- 浏览器扩展未安装、无事件、隐身等无法归属域名的浏览器时间单列为“网页未覆盖”，并标注覆盖不完整；不将“没采到”冒充“没有访问”。IP 地址、`localhost` 和非 HTTP(S) 页也不会列为域名。
- 如果窗口或离开状态 watcher 中途缺数据，汇总会标 `activity_coverage=partial`、`unobserved_seconds` 和最近同时有数据的 `last_observed_at`。`data_cutoff` 只是请求统计到的时间，不表示采集一直正常；两种 watcher 根本没有交集时拒绝生成报告。
- 默认不分享。开启时在电脑上展示接收地址和字段并要求手动确认；只能向当次确认的 HTTPS 地址上传。暂停后该时间段不会在恢复分享时补传。撤回会清除本客户端保存的授权区间，但不能删除已经送达云端的数据。
- 暂停或撤回本客户端**不会停止 ActivityWatch 在本机的记录**。本客户端不修改 ActivityWatch 的保留策略，也不清理其原始明细。

## 本机运行（Windows）

从源码运行需要 Python 3.11+；GitHub 构建的 Windows exe 自带 Python 运行时，使用者无需安装 Python 或 Rust。两种方式都需要已在**电脑使用者本人设备**运行的 ActivityWatch（默认本机 `127.0.0.1:5600`）。要获得域名时长，还需安装并启用相应浏览器的 ActivityWatch Web Watcher 扩展；否则软件时长仍可统计，网页覆盖会标为不完整。

在本仓库 `share-client/` 目录的 PowerShell 中先检查环境并运行测试：

```powershell
python -B -m unittest discover -s tests -v
python -m aw_share status
python -m aw_share local-preview --day today
```

如果 GitHub Tauri 构建流程生成了测试包，在使用者自己的 Windows 电脑下载、解压 ZIP 后，进入解压出的 `activitywatch` 目录；安装版则进入实际安装目录。在该目录的 PowerShell 中运行：

```powershell
.\share-client\aw-share.exe status
.\share-client\aw-share.exe local-preview --day today
```

下文的 `python -m aw_share` 命令均可换成 `.\share-client\aw-share.exe`（从 Tauri 安装或解压目录运行），子命令、交互确认和环境变量要求不变；例如使用者手动授权开启后，在可见终端运行 `.\share-client\aw-share.exe watch --interval 900 --catch-up-days 7`。该可见命令行程序不内置 ActivityWatch 本体或浏览器扩展（本 CI 的 Windows ZIP/安装包另外包含 ActivityWatch），也不内置接收地址、令牌或本机分类文件。构建产物与真实电脑、云端服务尚未联调；只有测试通过不代表已经能够在微信看到数据。

`local-preview` 会单独要求电脑使用者输入“我同意本机预览”，只在此终端展示当天汇总；**不启用分享、不需要云端地址或令牌、不上传**。输出标记 `local_only=true`、`authorized=false`，不能作为上报报文。它适合先在那台电脑上检查 ActivityWatch 是否正常记录。

待你的 HTTPS 接收端和**仅允许这台设备提交汇总**的令牌准备好后，在同一个 PowerShell 会话中配置；不要把令牌写入命令参数、仓库或分类文件：

```powershell
$env:AW_SHARE_ENDPOINT = "https://你的域名.example/api/reports"
$secret = Read-Host "设备上报令牌" -AsSecureString
$env:AW_SHARE_API_TOKEN = [System.Net.NetworkCredential]::new("", $secret).Password
python -m aw_share enable
```

`enable` 会在本机再次展示接收地址及上传范围，电脑使用者须输入“我同意分享”。这个示例中的域名只是占位符，当前项目**没有可用的真实云端地址或令牌**。换接收地址需要先 `pause`，再重新 `enable` 并确认。令牌只在当前 PowerShell 进程环境中，不会由客户端写到磁盘；重新打开终端须重新配置。

```powershell
python -m aw_share preview --day today
python -m aw_share send --day today
python -m aw_share watch --interval 900 --catch-up-days 7
```

`preview` 只在本机显示**已授权分享时段**的汇总；`send` 立即发一份；`watch` 在前台每 15 分钟发送当天快照，并在次日 00:15 后补发最近七天尚未成功送达的日报。这个命令所在终端需要保持运行；它不是隐蔽服务，也还未配置开机自启。网络失败时当前调用有限次重试，下一轮会从本机 ActivityWatch 重新计算仍在补发范围内、且属于已授权时间的汇总。超过补发窗口、原始事件不可用或程序未运行时，**不能保证自动补发**。认证失败会终止本轮后台上报，需先修复令牌。

```powershell
python -m aw_share pause
python -m aw_share status
python -m aw_share revoke
```

`pause` 停止后续分享，`revoke` 还会清除本地授权历史；已经开始的网络请求可能来不及撤销，远端数据删除也需要接收端支持。若要停止 ActivityWatch 自身的本机采集，应使用 ActivityWatch 的托盘控制，而不是这些命令。

## 类别规则

可复制 [categories.example.json](categories.example.json) 到电脑使用者自选的本地目录，按软件文件名或域名设类别。调用时把全局参数放在子命令之前，例如：

```powershell
python -m aw_share --categories .\categories.example.json preview --day today
```

软件规则是忽略大小写的精确文件名；域名规则也适用于其子域名。可选的 `browsers` 将扩展桶名中的浏览器标识映射到本机软件文件名，适用于默认列表以外的浏览器（示例中的 `browser.exe` 只在确认它确为 Yandex 浏览器时使用）。未配置的非浏览器软件、已覆盖却没有命中规则的域名归“未分类”。浏览器的未覆盖时长单列“网页未覆盖”。默认日报日界线是北京时间 `+08:00`；如果实际设备不在此时区，需在**所有**相关命令中一致使用 `--utc-offset +HH:MM`。

## 接收端最小协议

客户端使用验证证书的 HTTPS `POST`，拒绝重定向；请求带 `Authorization: Bearer <设备令牌>`、`Idempotency-Key: <report_id>` 和 JSON 正文：

```json
{
  "schema_version": 1,
  "device_id": "随机的本机设备标识",
  "kind": "current",
  "report_id": "本次汇总的确定性 SHA-256 标识",
  "summary": {
    "day": "2026-10-05",
    "data_cutoff": "2026-10-05T12:00:00+08:00",
    "authorized": true,
    "total_seconds": 3600,
    "applications": {"Editor.exe": 3600},
    "domains": {},
    "categories": {"学习办公": 3600},
    "category_applications": {"学习办公": {"Editor.exe": 3600}},
    "category_domains": {},
    "web_uncovered_seconds": 0,
    "website_coverage": "complete",
    "activity_coverage": "complete",
    "unobserved_seconds": 0,
    "last_observed_at": "2026-10-05T12:00:00+08:00"
  }
}
```

`kind` 为 `current`（当天快照）或 `daily`（结束后的日报）。同一份汇总的重试使用相同正文和幂等键；内容变化会生成新键。服务端应按 `(device_id, day, kind)` 保存最新快照、校验令牌及结构、拒绝旧 `data_cutoff` 覆盖新数据，并对相同 `report_id` 去重。客户端只在收到 2xx 后记本地送达记录。这里定义的是**待实现的云端契约**，不是已上线的 Cloudflare 接口。

## 验证范围与已知限制

自动测试使用合成的 ActivityWatch 事件、模拟传输，并在安装了测试依赖 `cryptography` 时运行本机真实 HTTPS 回环接收测试，验证汇总、脱敏、授权区间、跨零点读取、TLS、重试和失败口径；它们不证明校园网络、真实浏览器扩展或云端可用。ActivityWatch 某些版本按事件起点过滤查询，客户端额外向前读取一天再严格裁剪目标日；超过一天的跨界事件仍可能不完整。单台设备只读取与本机 ActivityWatch 身份匹配的桶，并要求恰好一个窗口桶和一个 AFK 桶；来源不明/其他电脑的浏览器桶不读取，关键源缺失或重复时会报“不可用”，不会伪造零时长。网页“完整”仅指**已识别浏览器**在有活动采集覆盖的时间内没有已知域名缺口，不保证所有浏览器或实际注意力都被识别。
