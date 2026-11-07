# Crying Obsidian Launcher

> 哭泣的黑曜石启动器

一个用 Python 写的 Minecraft Java 版第三方启动器（MVP）。核心下载/安装/启动逻辑基于 [`minecraft-launcher-lib`](https://pypi.org/project/minecraft-launcher-lib/)，界面使用 PySide6。

目前只支持**离线账号**，不做微软正版登录。

## 功能

- 版本列表、版本信息查看
- 下载并安装游戏：client.jar、libraries、assets、Mojang Java 运行时，带 SHA1 校验和安装后完整性检查
- 支持 Mojang 官方源和 BMCLAPI 镜像源，镜像失败时自动回退官方源
- 离线账号：用户名生成固定 UUID
- 自动检测本机 Java，也可手动添加任意 Java 路径；版本需要的 Java 版本不满足时会提示
- 拼接并执行启动命令，实时显示游戏日志
- 多游戏目录管理：一个游戏目录下可以有多个实例
- 实例隔离：每个实例使用独立游戏根目录（mods/config/saves 互不影响）
- 自动识别实例使用的加载器（Forge / NeoForge / Fabric / Quilt / LiteLoader / OptiFine / 原版）
- 安装可取消，超时可控，已下载的文件会跳过，不会重复下载

## 环境要求

- Python **3.10 - 3.13**（推荐 3.12）
- 依赖见 `requirements.txt`：
  - `minecraft-launcher-lib==8.0`
  - `requests>=2.31,<3`
  - `PySide6>=6.5,<7`
- 首次安装游戏约需 800 MB 磁盘空间（1.20.1 为例，不含存档）

## 安装

```bash
# 进入项目根目录（能看到 mc_launcher 文件夹的那一层）
python3.12 -m venv .venv
```

Windows：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS / Linux：

```bash
.venv/bin/python -m pip install -r requirements.txt
```

如果电脑里有多个 Python，**创建 venv 时用哪个，之后就一直用哪个**。推荐直接调用 venv 里的解释器，不依赖激活脚本。

## 运行

图形界面：

```powershell
# Windows
.\.venv\Scripts\python.exe -m mc_launcher gui
```

```bash
# macOS / Linux
.venv/bin/python -m mc_launcher gui
```

国内网络可以在界面「设置」页把下载源切换为 BMCLAPI，无需额外配置。

命令行：

```bash
python -m mc_launcher versions              # 查看版本列表
python -m mc_launcher info 1.20.1           # 查看版本元数据
python -m mc_launcher install 1.20.1        # 下载安装
python -m mc_launcher login Steve           # 生成离线账号信息（UUID）
python -m mc_launcher java                  # 列出本机 Java
python -m mc_launcher java --mc-version 1.20.1   # 查看会选用哪个 Java
python -m mc_launcher launch 1.20.1 --username Steve   # 直接启动
python -m mc_launcher launch 1.20.1 --username Steve --dry-run   # 只打印启动命令
python -m mc_launcher gui                   # 打开图形界面
```

`launch` 还支持 `--java`、`--memory`（默认 2048 MB）、`--jvm-arg`（可重复）、`--server` / `--port`、`--game-dir`。

镜像参数也可以放在子命令前后：`--mirror bmclapi`。

## 界面说明

| 页面 | 作用 |
| --- | --- |
| 启动 | 显示默认实例和默认账号，一个「启动游戏」按钮 |
| 版本管理 | 左侧按游戏目录分组显示实例；右侧编辑名称、版本、加载器、Java、内存、附加参数、隔离方式；支持设为默认、启动此实例、删除 |
| 下载 | 版本列表，选择版本后下载并创建实例，可指定实例名 |
| 账号管理 | 添加/删除多个离线账号，设置默认账号 |
| 设置 | Java 列表与手动添加、镜像源切换、全局内存、实例隔离开关、日志显示开关 |

添加游戏目录时，启动器会扫描该目录 `versions/` 下的所有版本并自动创建实例，只写启动器配置，不修改游戏文件。

## 实例隔离

每个游戏目录有三种隔离设置：

- 跟随全局（默认）
- 强制开启
- 强制关闭

强制设置优先于全局。最终生效时：

- **开启**：实例的游戏根目录为 `<游戏目录>/instances/<实例名>/`，不存在时自动创建
- **关闭**：所有实例共享 `<游戏目录>` 本身

添加新游戏目录时，启动器会按磁盘现状判断一次：目录里已有 `instances/` 文件夹就设为强制开启，否则强制关闭。之后你在界面上手动改过，就不会再被自动覆盖。

切换隔离方式不会迁移已有的 mods / saves，只影响后续启动。

## 数据位置

| 内容 | 路径 |
| --- | --- |
| 启动器配置（实例、账号、设置） | `~/.mc-launcher/settings.json` |
| 版本元数据缓存 | `~/.mc-launcher/meta/` |
| 游戏目录 | 默认取官方启动器的 `.minecraft` 路径，也可在界面里自行添加 |

Windows 上 `~` 是 `C:\Users\<用户名>`。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `MC_MIRROR` | `mojang`（默认）或 `bmclapi` |
| `MC_LAUNCHER_HOME` | 覆盖启动器数据目录 |
| `MC_GAME_DIR` | 覆盖默认游戏目录 |
| `MC_BMCLAPI_BASE` | 覆盖 BMCLAPI 主站地址 |
| `MC_MIRROR_RETRIES` | 镜像请求重试次数，默认 5 |
| `MC_MIRROR_TIMEOUT` | 单次请求超时秒数，默认 30 |
| `MC_MIRROR_FALLBACK` | 设为 `0` 可关闭镜像失败后回退官方源 |

## 项目结构

```
mc_launcher/
├── __main__.py    # python -m mc_launcher 入口
├── cli.py         # 命令行参数与子命令
├── config.py      # 路径与常量
├── version.py     # 版本清单、元数据缓存、本地版本继承合并、加载器识别
├── install.py     # 下载安装、进度回调、SHA1 与 assets 完整性校验
├── mirror.py      # BMCLAPI 镜像重写、超时、重试、取消
├── auth.py        # 离线账号与 UUID
├── java.py        # 本机 Java 检测与版本选择
├── launch.py      # 启动命令构建与子进程运行
├── settings.py    # 启动器配置、游戏目录、实例、隔离规则
└── ui.py          # PySide6 图形界面
```

## 常见问题

**提示 `No Java 17+ found`**
版本需要的 Java 没找到。到设置页手动添加 JDK 目录或 `java.exe`，或者在下载安装时让启动器安装 Mojang 官方运行时。

**安装卡在 `Download Assets 100%`**
进度按文件数计算，100% 时可能还剩最后一个文件。进度条会显示已用秒数；网络太慢可以点「取消安装」，之后重新点下载会跳过已下载的文件继续。

**非原版（Forge / Fabric / 整合包）启动报 `Invalid version id`**
旧版本的问题，已修复。这类版本只存在于本地 `versions/` 目录，现在启动时会优先读取本地 JSON 并合并 `inheritsFrom`。

**整合包启动后没有 mod**
检查实例的隔离设置。如果 mods 放在共享的游戏目录里，把所属游戏目录设为「强制关闭」；如果 mods 在 `instances/<实例名>/mods`，保持开启。

**BMCLAPI 偶发 SSL 错误**
镜像会 302 到多个 CDN 节点，个别节点不稳定。启动器会自动重试并回退官方源。

## 已知限制

- 只支持离线账号，没有微软 OAuth
- 只内置原版的下载源映射，Forge / Fabric 的 maven 源未走镜像
- 不做模组管理、整合包导入、皮肤、服务器列表
- 切换 Java 或隔离方式不会迁移已有文件

## 说明

仅供学习和个人使用。Minecraft 是 Mojang 的商标，本项目与其无关。使用第三方启动器时请遵守 Minecraft 最终用户许可协议。

项目名取自游戏里的「哭泣的黑曜石」，与 Mojang 无关联。Python 包名仍然是 `mc_launcher`，运行时命令保持不变。
