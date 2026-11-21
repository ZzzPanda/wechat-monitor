# 微信视频号保存工具

在 Apple Silicon Mac 上，用 Docker 长期运行已登录的 Linux 微信。给 `wechat-save` 一个或多个微信视频号分享链接，它会核对作者，通过普通播放器保存画面和原声。用本地管理页添加视频号、浏览已保存作品和播放视频。视频、登录卷和任务数据库都放在指定的 1 TB 外置盘。

## 一键部署

挂载指定的 1 TB 外置盘 `/Volumes/ssd1`。这台 M4 已有源码，直接运行一条命令即可启动：

```sh
/Volumes/ssd1/wechat-monitor/source/deploy.sh
```

首次用已登录的 GitHub CLI 安装，可以一条命令克隆源码到外盘并部署：

```sh
gh repo clone ZzzPanda/wechat-monitor /Volumes/ssd1/wechat-monitor/source && /Volumes/ssd1/wechat-monitor/source/deploy.sh
```

在已有的 Git 工作目录中也可以直接运行 `./deploy.sh`。

部署结束后，打开[视频号管理页](http://127.0.0.1:33023/)添加账号或查看历史视频。首次使用在[微信桌面](http://127.0.0.1:33021/)扫码。再次部署会识别现有容器及登录卷，并在它们已停止时启动。下载、录制或其他部署正在占用播放器时，稍后重试即可。

全新环境需要 macOS 命令行开发工具（含 Python 3.9+、Swift 和 Vision SDK）、Docker CLI 及 Compose 插件、支持 `setts/prescale` 的 FFmpeg 与 ffprobe（实测 8.1），以及 Google Chrome 或 Chromium。Docker 服务由脚本在外盘的 Lima 虚拟机中启动。脚本会准备固定版本的 Lima 和含 Docker 的 Ubuntu ARM64 基础盘，载入或构建播放器镜像，编译 OCR，并在需要时把 Python 依赖安装到外盘虚拟环境。本版校验这块物理外盘的卷 UUID。

## 管理监控名单

在[管理页](http://127.0.0.1:33023/)点击「添加视频号」，输入关键词并搜索。管理页使用外盘 Docker 中已登录微信的「搜一搜」查找视频号，最多显示首屏中 4 个匹配且能核验主页的账号。核对结果里的名称和微信主页链接，选中目标账号，再点「加入监控名单」。搜索和打开结果都不会自动添加账号。微信退出登录时，在[微信桌面](http://127.0.0.1:33021/)重新登录。需要实时查询时，等保存任务结束再搜索。

相同关键词在 10 分钟内再次搜索会复用上次核实的主页，仍需手动选择账号。点「重新从微信搜索」可以立即更新结果。首次搜索和没有匹配账号的查询都会访问微信。

首次部署的名单已有「数字生命卡兹克」。旧名单仍可使用，重新搜索并选择同一个账号会为它补上主页链接。管理页与保存工具共用外盘名单，`config/authors.txt` 仍然可用。

选中监控账号后，可以编辑本地备注、通过微信搜索更换绑定的主页，或移除监控。已有视频继续按原作者显示在「历史作者」里，也可以从那里重新加入监控。

## 手动拉取历史和定时采集

- **单个账号的历史作品**。选中已关联主页的视频号，点「拉取历史作品」。程序逐页核对作品并保存到外盘。单条作品出错后会继续处理下一条，同一轮只尝试一次。任务显示「部分完成」时可以重新拉取，已保存的作品会跳过，保留了完整录制的作品会从封装步骤恢复。外盘或微信运行环境不可用时，任务会停止并显示原因。
- **全部监控账号的增量**。在「采集任务」里设置首次运行的北京时间、间隔和开关。默认开启，间隔默认 60 分钟，最短 1 小时。例如从明天 09:30 起、间隔 1440 分钟，即每天 09:30 检查。每轮按当时的名单排队。旧名单中仅有昵称的记录会提示先关联主页。
- **增量起点**。每个账号第一次定时检查会记录最近最多 10 条已核实作品作为基线。后续检查结合发布时间和作品指纹保存新增内容。同一天发布的作品逐条比对。需要以前的视频时可手动拉取历史。作品使用公开分享页元数据和封面指纹去重，换分享短链也能识别。
- **定时增量优先**。所有账号共用一个微信播放器。定时任务到点时，正在拉历史的账号先处理完手上这条作品，再让出播放器。排队的增量任务全部结束后，原历史任务从断点继续，已保存的作品按指纹去重。微信主页顺序有变化时，程序会重新核对列表。
- **进度和停止**。页面显示下次运行时刻，以及每项任务发现、保存、跳过和失败的数量。历史任务等待增量时仍可点「停止」。点「停止」或将账号移除后，在途作品可能先完成，已保存的文件保留。Mac 休眠错过多次时，恢复后补一轮，之后继续按设定时刻运行。

定时采集、搜索和录制需要 M4 开机、指定外盘挂载、Docker 中的微信已登录。重启 Mac 后，重新运行一键部署命令恢复后台进程。配置、任务进度、作品索引和视频都存放在外盘。

## 用链接保存一个视频

保存作品前，在微信中打开作者主页并让作品卡片可见，也可以直接打开该作品。

```sh
./wechat-save save 'https://weixin.qq.com/sph/AaOturjvvg'
./wechat-save status 'https://weixin.qq.com/sph/AaOturjvvg'
```

`saved` 表示保存完成，`already_saved` 表示已保存同一作品，`verified` 表示本地文件校验通过。首次保存耗时约为作品播放时长。程序采集微信播放器可见画面，保留原声，也可能录到界面控件。[保存、断点恢复与测试说明](保存微信作品.md)

## 镜像与组件

| 组件 | 部署位置 | 用途 |
| --- | --- | --- |
| `local/wechat-monitor-player:arm64-v1` | 外盘 Lima 的 Docker 引擎 | Linux 微信、浏览器桌面、X11、PulseAudio 和录制用 FFmpeg |
| `local/wechat-monitor-dashboard:arm64-v1` | 同一 Docker 引擎 | 本机管理页，维护作者名单、浏览和播放外盘历史视频 |
| `wechat-save` | Mac，代码安装在外盘 | 作者核对、Apple Vision 中文 OCR、控制播放器、成片及去重 |
| `local/wx-channels-server:v261106` | 此机器此前的独立实验镜像 | 原项目为 `wx_channels_download`，记录其版本以便追溯；本次保存流程使用播放器和 `wechat-save` |

[Compose 配置](compose.yaml) 和 [Dockerfile](docker/Dockerfile) 可用于重新构建播放器。基础镜像按 digest 固定。外盘 `images/wechat-monitor-player-arm64-v1.tar` 是已构建镜像的离线副本，大小约 1.47 GB。部署时优先使用本机已有镜像，其次从校验通过的离线归档导入，最后才联网构建。镜像归档只包含应用层。上游发行包、离线归档的 SHA-256 和实验镜像标识见[镜像清单](deploy/images.lock.json)。上游桌面使用 [LinuxServer Weixin](https://docs.linuxserver.io/images/docker-weixin/)。

## 外盘布局

| 路径（相对于 `/Volumes/ssd1/wechat-monitor`） | 内容 |
| --- | --- |
| `source/` | 可独立使用或克隆的 Git 源码仓库 |
| `scripts/`、`wechat-save` | 部署后的下载工具及入口 |
| `media/微信视频号/` | 按作者保存的 MP4 |
| `state/wechat-pilot/` | 作品索引、链接去重与保存锁 |
| `state/monitoring/authors.txt`、`state/posters/` | 管理页使用的作者名单、视频帧缩略图和时长 |
| `state/monitoring/crawl.sqlite3` | 定时设置、任务队列及已发现作品的进度 |
| `images/`、`cache/` | 离线 Docker 镜像和基础环境缓存 |
| `lima/wx/` | 专用 Docker 虚拟机及登录卷所在的虚拟磁盘 |
| `runtime/`、`tmp/` | Lima、OCR、部署配置、外盘临时文件 |

升级代码后重新运行 `./deploy.sh` 即可安装下载工具。播放器镜像单独更新；部署默认保留健康的现有播放器。需要主动切换到仓库中的播放器镜像时，在保存任务结束后运行下方命令。它会重建播放器容器，继续挂载同一个命名卷，微信可能要求重新登录。

```sh
docker --host unix:///Volumes/ssd1/wechat-monitor/lima/wx/sock/docker.sock \
  compose -f /Volumes/ssd1/wechat-monitor/runtime/deployment/compose.yaml \
  up -d --no-build --force-recreate weixin
```

macOS 重启后，重新运行 `./deploy.sh` 会恢复 Docker 运行环境和桌面。微信容器使用 `restart: unless-stopped`。端口只监听本机回环地址。[Compose 启动参数](https://docs.docker.com/reference/cli/docker/compose/up/)
