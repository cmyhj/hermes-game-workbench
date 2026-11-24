# hermes-game-workbench

在 **Hermes Agent** 的 Web 控制台里运行的「游戏」页签（`wb-game`）：今日游戏脚本运行情况、
体力推算，以及卡池「即将到来」。

这是一个**独立可安装的 Hermes dashboard 插件**。仓库内不写死任何本机绝对路径、账号名、
凭据或真实日志；所有外部数据源的位置都由环境变量或插件目录下的 `config.json` 指定。

- 目标 Hermes 版本：**0.23.x**（插件 SDK 契约 `1.1.0`）
- 平台：Windows 11 优先（读取 Windows 本地脚本日志与注册表文件），Linux 可运行但只支持 MCP 数据线
- 许可：MIT（见 `LICENSE`）；第三方来源与许可见 `NOTICE`

---

## 1. 它做什么 / 不做什么

**做**

| 能力 | 说明 |
|---|---|
| **今日运行情况** | AUTO-MAS 只读 MCP（历史 / 详情 / 运行快照）+ M7A 本机 `runs` 日志 + ZZZ-OneDragon 原生日志，按账号/脚本汇总今日 04:00 CST 之后的结果 |
| **体力推算** | 从最近一条成功日志里读体力值；按「6 分钟恢复 1 点」推算到当前时刻，显示推算值而不是原始读数 |
| **卡池即将到来** | 只读 ChronoRail 导出的 `game-versions.json`，按「开池 → 关池 → 新版本」排序展示角色卡池 |
| **模块级 agent 对话** | 复用 vendored 的 `wb-agent-chat`，`moduleId=wb-game`，把当前游戏/脚本/账号作为上下文；仍走 Hermes 自己的 agent |

**不做（刻意的边界）**

- 不启动、停止、暂停或点击任何游戏脚本；不向 AUTO-MAS / M7A / 一条龙发写请求。
- 不读取或修改 AUTO-MAS 配置、脚本配置、日志文件；只读打开。
- 不展示密码、token、Cookie、Authorization、完整手机号（11 位手机号在输出前脱敏）。
- 没有读数就是 `unknown`，不用运行快照或别的账号的历史去填充；无法解析的日志如实报错。

---

## 2. 参考来源

本模块是「只读聚合展示」，**没有复制任何上游项目的代码**；下表列出设计、数据形状与
行为的参考对象。三个同系列模块的参考来源一并列出，方便对照：

| 模块 | 仓库 | 参考项目 | 地址 | 参考内容 |
|---|---|---|---|---|
| 项目 | [hermes-project-workbench](https://github.com/cmyhj/hermes-project-workbench) | **ProjectManager** | <https://github.com/AlicDanclic/ProjectManager> | `.pm` 项目数据模型（任务/里程碑/看板/甘特）、默认极简的页面组织；MIT |
| 学习 | [hermes-learning-workbench](https://github.com/cmyhj/hermes-learning-workbench) | **Shiroha Quiz** | <https://github.com/reiqr/shiroha-quiz> | 题库数据形状、题型枚举、多选/判断/多空/简答的判分语义；GPL-3.0（未复制代码） |
| 学习 | 同上 | **`/teach`（Hermes 教学技能）** | <https://hermes-agent.nousresearch.com/docs/guides/work-with-skills> | 课程工作区约定（`MISSION.md`/`RESOURCES.md`/`NOTES.md`/`lessons/`/`reference/`/`assets/`/`learning-records/`）与「子条目 `course/`」布局；调用方式：在对象对话里发 `/teach <主题>` |
| **游戏**（本仓库） | [hermes-game-workbench](https://github.com/cmyhj/hermes-game-workbench) | **AUTO-MAS** | <https://github.com/AUTO-MAS-Project/AUTO-MAS> | MCP 只读工具、历史记录字段与 `+04:00` 时间语义、脚本/账号配置结构；AGPL-3.0 |
| 游戏 | 本仓库 | **March7thAssistant (M7A)** | <https://github.com/moesnow/March7thAssistant> | `installs.json` 注册表、`logs/runs/*.json` 运行记录与 `开拓力` 日志行形状；GPL-3.0 |
| 游戏 | 本仓库 | **ZenlessZoneZero-OneDragon** | <https://github.com/OneDragon-Anything/ZenlessZoneZero-OneDragon> | 原生 `log.txt*` 的完成/失败行与 `剩余电量` 读数形状；GPL-3.0 |
| 游戏 | 本仓库 | **ChronoRail**（同作者） | <https://github.com/cmyhj/ChronoRail> | `game-versions.json` / `chronorail.json` 数据形状，「即将到来」的开池→关池→新版本展示顺序（参考 `UpcomingHero.tsx` 行为）；MIT |
| 宿主 | 三个模块 | **Hermes Agent** | <https://github.com/NousResearch/hermes-agent> | dashboard 插件框架、`/api/ws` agent 通道、静态资产隔离；MIT |

许可说明：AUTO-MAS（AGPL-3.0）、M7A（GPL-3.0）、OneDragon（GPL-3.0）都**允许**复用，
约束是 copyleft。本项目为避免把整个仓库变成 GPL/AGPL，选择了「只读接口 + 自行实现解析」，
没有复制它们的源码或资源。若将来要嵌入这些项目的代码，相应部分必须按其许可分发。

---

## 3. 安装

### 3.1 前置

1. 已安装 Hermes Agent，并能启动 Web 控制台：`hermes dashboard`（不是 `hermes serve` —— `serve` 是无头后端，没有浏览器 UI）。
2. 插件必须列进 `config.yaml` 的 `plugins.enabled`，否则后端 Python 不会被导入。
3. 数据源（可选，缺哪个就只显示哪个源不可达）：
   - AUTO-MAS 已启动，且其 MCP 端点在 `http://127.0.0.1:36163/mcp`（默认值，可改）。
   - M7A 独立版安装过（读 `%LOCALAPPDATA%\March7thAssistant\installs.json`）。
   - ZZZ-OneDragon 安装目录可读。
   - ChronoRail 的 `game-versions.json` / `chronorail.json` 可读。

### 3.2 放入插件目录

```bash
# Windows（Git Bash / MSYS）
HERMES_HOME="${HERMES_HOME:-$LOCALAPPDATA/hermes}"
mkdir -p "$HERMES_HOME/plugins/wb-game"
cp -r wb-game/. "$HERMES_HOME/plugins/wb-game/"
cp shared/wb-agent-chat/* "$HERMES_HOME/plugins/wb-game/dashboard/dist/vendor/"
```

```bash
# Linux / macOS
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/plugins/wb-game"
cp -r wb-game/. "$HERMES_HOME/plugins/wb-game/"
cp shared/wb-agent-chat/* "$HERMES_HOME/plugins/wb-game/dashboard/dist/vendor/"
```

目录结构：

```
$HERMES_HOME/plugins/wb-game/
  config.json                 # 可选，见 §4
  dashboard/
    manifest.json
    plugin_api.py
    dist/
      index.js
      style.css
      game-icons/
      vendor/wb-agent-chat.js
      vendor/wb-agent-chat.css
```

### 3.3 启用

```yaml
# config.yaml
plugins:
  enabled:
    - wb-game
```

然后重启 `hermes dashboard`（后端插件在进程启动时导入一次，不会热加载）。

---

## 4. 配置

优先级：**环境变量 > `wb-game/config.json` > 无默认值（报未配置）**。
参考 `config.example.json`：

```json
{
  "mcp_url": "http://127.0.0.1:36163/mcp",
  "chronorail_path": "<ChronoRail>/public/data/game-versions.json",
  "chronorail_catalog_path": "<ChronoRail>/public/data/chronorail.json",
  "auto_mas_script_config": "<AUTO-MAS>/config/legacy-json/ScriptConfig.json",
  "zzz_onedragon_root": "<ZZZ-OneDragon>"
}
```

| 环境变量 | `config.json` 字段 | 用途 |
|---|---|---|
| `WB_GAME_MCP_URL` | `mcp_url` | AUTO-MAS MCP 端点（默认 `http://127.0.0.1:36163/mcp`） |
| `WB_GAME_CHRONORAIL` | `chronorail_path` | ChronoRail `game-versions.json` |
| `WB_GAME_CHRONORAIL_CATALOG` | `chronorail_catalog_path` | ChronoRail `chronorail.json`（图标/颜色目录） |
| `WB_GAME_AUTO_MAS_SCRIPT_CONFIG` | `auto_mas_script_config` | AUTO-MAS `ScriptConfig.json`（只读账号显示名） |
| `WB_GAME_ZZZ_ONEDRAGON` | `zzz_onedragon_root` | ZZZ-OneDragon 安装根目录 |

未配置的数据源在 UI 里显示为「不可达 / 未配置」，不会影响其他数据线。

---

## 5. 接口

| 路由 | 说明 |
|---|---|
| `GET /api/plugins/wb-game/health` | MCP 连通性 + ChronoRail 文件可读性（含路径与错误原因） |
| `GET /api/plugins/wb-game/overview` | 今日运行情况 + 体力推算 + 即将到来卡池 + 各数据源状态 |

`overview` 有 25 秒内存缓存，避免每次刷新都打 MCP。

---

## 6. 安全边界

1. **只读白名单**：MCP 只允许 `search_history` / `history_detail` / `runtime_snapshot` 三个工具，
   其他调用直接拒绝。
2. **不碰凭据**：日志解析遇到 `password|passwd|token|cookie|authorization|secret|密码` 的行
   直接丢弃，不进入错误摘要。
3. **手机号脱敏**：输出前把 11 位手机号替换为 `[手机号已隐藏]`。
4. **不伪造**：没有当日记录/读数时返回 `unknown` 或「今日暂无运行记录」，不用快照或
   其他账号的数据补齐。
5. **不改上游**：不写 AUTO-MAS / M7A / 一条龙 / ChronoRail 的任何文件。

---

## 7. 测试

```bash
# 解析器 / 时间边界 / 来源解析回归
python -m pytest tests -q -p no:cacheprovider

# 浏览器契约（需要 Playwright + Chromium；没有则自动 skip）
python -m pytest tests/test_browser_contract.py -q -p no:cacheprovider
```

测试只使用 `tests/fixtures/` 里的合成/脱敏日志片段，不读真实安装目录，也不访问网络。

共享组件一致性：

```bash
python tools/sync-shared.py --check
```

---

## 8. 许可

本项目以 MIT 分发（`LICENSE`）。第三方来源、许可与「未复制代码」的边界见 `NOTICE`。
