# DeepSeek Harness NAS for Synology DSM 7

把 [EIGHTfs/DeepSeekHarness-NAS](https://github.com/EIGHTfs/DeepSeekHarness-NAS) 的最新版本封装成群晖 DSM 7 套件（SPK），由 GitHub Actions 定时构建并发布。

DeepSeek Harness 的群晖原生封装，无需 Docker；修正了按版本隔离数据目录导致升级后配置丢失的问题。

## 特性

- 不依赖 Docker，直接运行上游原生二进制
- 跟随上游 Release 自动构建，通常在上游发版后数小时内产出新包
- 同时提供 x86_64 与 ARM 两种架构
- 构建过程固定时间戳，同版本产物可重现

## 安装

### 手动安装

在 [Releases](../../releases) 下载对应架构的安装包，然后到套件中心 →「手动安装」：

| 架构 | 文件 |
| --- | --- |
| x86_64 机型 | `DeepSeekHarness-NAS_<version>_x86_64.spk` |
| ARM 机型 | `DeepSeekHarness-NAS_<version>_armv8.spk` |

若不确定机型对应的架构，可在 DSM「控制面板 → 信息中心」查看，或参考 [群晖机型列表](https://kb.synology.com/DSM/tutorial/What_kind_of_CPU_does_my_NAS_have)。

### 通过套件源安装

把本仓库的 Release 汇总到自建套件源后，即可在套件中心直接搜索安装。仓库本身不提供在线套件源。

## 配置

配置与对话记录集中保存在套件的 `var/.dsh` 目录，跨版本升级会保留。

套件数据目录：`/var/packages/DeepSeekHarness-NAS/var`

## 构建

```bash
python build.py
```

产物输出到 `dist/`。构建流程：

1. 查询上游最新 Release
2. 按架构匹配对应资产并下载
3. 组装 `package.tgz` 与 `INFO`，打成 SPK
4. 生成 `dist/manifest.json` 与发布说明

仓库结构：

```
build.py               构建脚本
spk_builder.py         SPK 封装实现
dsh_patcher.py         解包上游 SPK、修正配置持久化与迁移逻辑后重打包
app.json               套件元数据与上游资产匹配规则
.github/workflows/     自动构建与发布
```

本仓库不维护套件框架文件，`INFO`、`conf`、`scripts`、UI 等均沿用上游 SPK 原样，只在重打包时修补安装脚本与启动脚本。

最低 DSM 版本：7.0-40851

## 免责声明

本项目是社区维护的封装脚本，与上游项目及群晖科技均无隶属关系。软件本身版权归上游作者所有，使用前请阅读上游项目的许可证。
