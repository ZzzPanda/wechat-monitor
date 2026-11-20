# 微信视频号保存工具

在 Apple Silicon Mac 上，用 Docker 长期运行已登录的 Linux 微信。给 `wechat-save` 一个或多个微信视频号分享链接，它会核对作者，通过普通播放器保存画面和原声。视频、登录卷、下载缓存及任务数据库都放在指定的 1 TB 外置盘。

## 一键部署

在这台 M4 上挂载 `/Volumes/ssd1`，进入本仓库运行：

```sh
./deploy.sh
```

看到「微信桌面已就绪」后，打开 [微信桌面](http://127.0.0.1:33021/)。第一次使用在微信桌面扫码。再次部署会识别现有容器及登录卷，并在它们已停止时启动。下载、录制或其他部署正在占用播放器时，稍后重试即可。

全新环境需要 macOS 命令行开发工具（含 Python 3.9+、Swift 和 Vision SDK）、Docker CLI 及 Compose 插件、支持 `setts/prescale` 的 FFmpeg 与 ffprobe（实测 8.1），以及 Google Chrome 或 Chromium。Docker 服务由脚本在外盘的 Lima 虚拟机中启动。脚本会准备固定版本的 Lima 和含 Docker 的 Ubuntu ARM64 基础盘，载入或构建播放器镜像，编译 OCR，并在需要时把 Python 依赖安装到外盘虚拟环境。本版校验这块物理外盘的卷 UUID。

## 保存一个视频

在容器的微信中打开作者主页，让目标作品卡片可见，也可以直接打开这条作品。将作者显示名逐行写入外盘 `/Volumes/ssd1/wechat-monitor/config/authors.txt`，首次部署会放入「数字生命卡兹克」作为初始值。

```sh
./wechat-save save 'https://weixin.qq.com/sph/AaOturjvvg'
./wechat-save status 'https://weixin.qq.com/sph/AaOturjvvg'
```

`saved` 表示保存完成，`already_saved` 表示已保存同一作品，`verified` 表示本地文件校验通过。首次保存耗时约为作品播放时长。程序采集微信播放器可见画面，保留原声，也可能录到界面控件。当前命令处理已知链接；定期发现作者的新作需要另接作品列表采集。[保存、断点恢复与测试说明](保存微信作品.md)

## 镜像与组件

| 组件 | 部署位置 | 用途 |
| --- | --- | --- |
| `local/wechat-monitor-player:arm64-v1` | 外盘 Lima 的 Docker 引擎 | Linux 微信、浏览器桌面、X11、PulseAudio 和录制用 FFmpeg |
| `wechat-save` | Mac，代码安装在外盘 | 作者核对、Apple Vision 中文 OCR、控制播放器、成片及去重 |
| `local/wx-channels-server:v261106` | 此机器此前的独立实验镜像 | 原项目为 `wx_channels_download`，记录其版本以便追溯；本次保存流程使用上面两项 |

[Compose 配置](compose.yaml) 和 [Dockerfile](docker/Dockerfile) 可用于重新构建播放器。基础镜像按 digest 固定。外盘 `images/wechat-monitor-player-arm64-v1.tar` 是已构建镜像的离线副本，大小约 1.47 GB。部署时优先使用本机已有镜像，其次从校验通过的离线归档导入，最后才联网构建。镜像归档只包含应用层。上游发行包、离线归档的 SHA-256 和实验镜像标识见[镜像清单](deploy/images.lock.json)。上游桌面使用 [LinuxServer Weixin](https://docs.linuxserver.io/images/docker-weixin/)。

## 外盘布局

| 路径（相对于 `/Volumes/ssd1/wechat-monitor`） | 内容 |
| --- | --- |
| `source/` | 可独立使用或克隆的 Git 源码仓库 |
| `scripts/`、`wechat-save` | 部署后的下载工具及入口 |
| `media/微信视频号/` | 按作者保存的 MP4 |
| `state/wechat-pilot/` | 作品索引、链接去重与保存锁 |
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
