"""wb-game — 游戏模块后端（今日运行情况 + 体力推算 + 卡池即将到来）。

挂载点：``/api/plugins/wb-game/``

三条数据线：
1. **AUTO-MAS MCP（只读）**：通过 Streamable HTTP 调用
   ``search_history_api_history_search_post`` / ``get_history_detail_api_history_detail_post``，
   运行时间线严格筛选今日 04:00 CST 至当前时刻；最近错误与成功体力读数从近 30 天历史中读取。
2. **体力推算**：6 分钟恢复 1 点，实时值 = 读取值 + floor((now - 读取时刻)/360s)，
   上限 max；早于今日 04:00 的读数仍按公式推算但标记偏旧；近 30 天内未找到成功读取则显示 unknown。
3. **ChronoRail 本地文件（只读）**：ChronoRail 仓库导出的 ``game-versions.json``
   （路径由 ``WB_GAME_CHRONORAIL`` 指定），实现其「即将到来」的
   开池 → 关池 → 新版本 展示顺序（参考 ``UpcomingHero.tsx`` 的行为，未复制代码）。

安全约束：
* 只调用 MCP 白名单内的三个只读工具，绝不启动/停止游戏任务、不读邮件或凭证。
* 历史时间戳带 +04:00 偏移，必须 ``astimezone(UTC+8)``，不当作北京时间。
* 当天可能一条记录都没有，此时如实返回「今日暂无运行记录」，不编造。
* ChronoRail 文件缺失时如实报错，不猜测替代路径。
* 20–30 秒内存缓存，避免每次刷新都打 MCP。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

router = APIRouter()

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
MCP_URL = os.environ.get("WB_GAME_MCP_URL") or "http://127.0.0.1:36163/mcp"
CST = _dt.timezone(_dt.timedelta(hours=8))        # 北京时间
SERVER_TZ = _dt.timezone(_dt.timedelta(hours=4))  # AUTO-MAS 服务器时区（历史时间戳 +04:00）

# 本机路径不在仓库里写死。配置来源优先级：
#   1. 环境变量（WB_GAME_*）
#   2. 插件目录的 config.json（wb-game/config.json，见 config.example.json）
# 两个都没有时对应数据源报「未配置」，而不是猜一个路径。
_PLUGIN_DIR = Path(__file__).resolve().parent
_CONFIG_CANDIDATES = (_PLUGIN_DIR.parent / "config.json", _PLUGIN_DIR / "config.json")


def _load_plugin_config() -> Dict[str, Any]:
    for candidate in _CONFIG_CANDIDATES:
        if candidate.is_file():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            return loaded if isinstance(loaded, dict) else {}
    return {}


_CONFIG = _load_plugin_config()


def _path_setting(env_name: str, key: str) -> Optional[Path]:
    raw = os.environ.get(env_name) or _CONFIG.get(key)
    return Path(str(raw)).expanduser() if raw else None


CHRONORAIL_PATH = _path_setting("WB_GAME_CHRONORAIL", "chronorail_path")
CHRONORAIL_CATALOG_PATH = _path_setting(
    "WB_GAME_CHRONORAIL_CATALOG", "chronorail_catalog_path"
)
AUTO_MAS_SCRIPT_CONFIG = _path_setting(
    "WB_GAME_AUTO_MAS_SCRIPT_CONFIG", "auto_mas_script_config"
)
ZZZ_ONEDRAGON_ROOT = _path_setting("WB_GAME_ZZZ_ONEDRAGON", "zzz_onedragon_root")

# MCP 只读白名单：只允许这三个工具
READ_ONLY = {
    "search_history_api_history_search_post",
    "get_history_detail_api_history_detail_post",
    "get_task_runtime_snapshot_api_dispatch_runtime_snapshot_get",
}

# 缓存：25 秒 TTL，避免每次刷新都打 MCP
_CACHE_TTL = 25.0
_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}


# --------------------------------------------------------------------------
# MCP 客户端（只读 JSON-RPC，白名单内的工具才允许调用）
# --------------------------------------------------------------------------
class _MCPClient:
    def __init__(self):
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.n = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "hermes-wb-game", "version": "1.0"},
        })
        self.rpc("notifications/initialized", notification=True)

    def rpc(self, method, params=None, notification=False):
        self.n += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self.n
        if params is not None:
            payload["params"] = params
        req = urllib.request.Request(
            MCP_URL, data=json.dumps(payload).encode(), headers=self.headers
        )
        with self.opener.open(req, timeout=12) as response:
            sid = response.headers.get("mcp-session-id")
            if sid:
                self.headers["Mcp-Session-Id"] = sid
            body = response.read().decode()
        if not body:
            return {}
        if not body.lstrip().startswith("{"):
            candidates = [
                line[5:].strip() for line in body.splitlines() if line.startswith("data:")
            ]
            messages = [json.loads(x) for x in candidates if x and x != "[DONE]"]
            result = next(
                (x for x in messages if x.get("id") == payload.get("id")), None
            )
            if result is None:
                raise RuntimeError("MCP returned no matching JSON-RPC response")
        else:
            result = json.loads(body)
        if "error" in result:
            raise RuntimeError(str(result["error"]))
        return result.get("result", {})

    def call(self, name, arguments):
        if name not in READ_ONLY:
            raise ValueError("wb-game permits read-only tools only")
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(str(result))
        value = result.get("structuredContent")
        if value is None:
            value = json.loads(
                "\n".join(
                    c.get("text", "")
                    for c in result.get("content", [])
                    if c.get("type") == "text"
                )
            )
        if value.get("status") == "error" or value.get("code", 200) not in (1, 200):
            raise RuntimeError(
                f"Business failure: {value.get('code')} {value.get('message')}"
            )
        return value

    def close(self):
        try:
            self.opener.open(
                urllib.request.Request(MCP_URL, headers=self.headers, method="DELETE"),
                timeout=3,
            ).close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 时间工具
# --------------------------------------------------------------------------
def _local_time(value: str) -> _dt.datetime:
    """把历史时间戳（带偏移）转成 CST。"""
    parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("History timestamp has no UTC offset; do not guess timezone")
    return parsed.astimezone(CST)


def _parse_date(s: str) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def _flatten_window(
    history: Dict[str, Any], window_start: _dt.datetime, window_end: _dt.datetime
) -> List[Dict[str, Any]]:
    """把 search_history 返回的 structure 摊平，只保留窗口内的记录。

    结构：``data -> {bucket: {account: {"index": [record]}}}``。
    历史时间戳带 +04:00，必须 astimezone(CST) 再比较。
    """
    records: List[Dict[str, Any]] = []
    for group in history.get("data", {}).values():
        if not isinstance(group, dict):
            continue
        for account_data in group.values():
            if not isinstance(account_data, dict):
                continue
            for record in account_data.get("index", []):
                if not isinstance(record, dict):
                    continue
                try:
                    when = _local_time(record["time"])
                except (KeyError, ValueError, TypeError):
                    continue
                if when < window_start or when > window_end:
                    continue
                record = dict(record)
                record["time_cst"] = when.isoformat(timespec="seconds")
                records.append(record)
    # 用户手工误加的星穹铁道模拟器「新账号」已删除；历史桶里仍有同名记录，忽略。
    records = [
        r
        for r in records
        if not (
            (r.get("username") == "新账号" and r.get("script_name") == "星穹铁道模拟器")
            or r.get("script_name") == "三月七小助手"
        )
    ]
    # Standalone March7thAssistant is read from its own install logs below; never
    # represent AUTO-MAS records named after the standalone tool as its account.
    # ZZZ daily-task truth must come from ZZZ-Onedragon's own log, not AUTO-MAS status.
    records = [r for r in records if r.get("script_name") != "绝区零一条龙"]
    records.sort(key=lambda r: (r["time_cst"], str(r.get("id", ""))))
    return records


# --------------------------------------------------------------------------
# 体力解析 + 推算
# --------------------------------------------------------------------------
# 每条规则：pattern 搜日志行，group(1) 是当前值；max 可为 None（从 pattern 的 group(2) 取）
STAMINA_RULES: Dict[str, Dict[str, Any]] = {
    "原神": {
        "label": "原粹树脂",
        "pattern": re.compile(r"原粹树脂[：:]\s*(\d+)(?:[，,]\s*浓缩树脂[：:]\s*(\d+))?"),
        "max": 200,
    },
    "明日方舟": {
        "label": "理智",
        "pattern": re.compile(r"理智[：:]\s*(\d+)\s*/\s*(\d+)"),
        "max": None,  # 从 pattern 的 /N 取
    },
    "明日方舟：终末地": {
        "label": "理智",
        "pattern": re.compile(r"当前理智\s*(\d+)\s*/\s*(\d+)"),
        "max": None,
    },
    "三月七小助手": {
        "label": "开拓力",
        "pattern": re.compile(r"开拓力[：:]\s*(\d+)\s*/\s*(\d+)"),
        "max": None,
    },
    "星穹铁道模拟器": {
        "label": "开拓力",
        "pattern": re.compile(r"开拓力[：:]\s*(\d+)\s*/\s*(\d+)"),
        "max": None,
    },
    "绝区零一条龙": {
        "label": "电量",
        "pattern": re.compile(r"剩余电量\s*(\d+)"),
        "max": 240,
    },
    "异环": {
        "label": "体力",
        "pattern": re.compile(r"当前体力\s*(\d+)"),
        "max": None,  # 日志未给上限
    },
}

# 日志行首时间戳正则
_LOG_FULL_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")
_LOG_TIME_ONLY_RE = re.compile(r"\[(\d{2}:\d{2}:\d{2})")


def _safe_account_name(value: Any) -> str:
    return re.sub(r"(?<!\d)1\d{10}(?!\d)", "[手机号已隐藏]", str(value or ""))


def _parse_log_timestamp(
    line: str, record_time_cst: _dt.datetime, source_tz: _dt.tzinfo = SERVER_TZ
) -> Tuple[_dt.datetime, str]:
    """从日志行解析读取时刻，返回 (CST 时间, 说明)。

    三种行首时间戳：
    * 完整日期时间 ``2026-11-21 14:53:38.246`` → 按服务器时区(+04:00)解释，转 CST
    * 仅时分秒 ``[08:33:26.734]`` → 取与记录时间最接近的那一天
    * 无时间戳 → 用记录时间，标注「时刻为记录时间近似」
    """
    m = _LOG_FULL_TS_RE.search(line)
    if m:
        dt_naive = _dt.datetime.strptime(
            f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S"
        )
        return dt_naive.replace(tzinfo=source_tz).astimezone(CST), "精确"
    m = _LOG_TIME_ONLY_RE.search(line)
    if m:
        h, mi, s = map(int, m.group(1).split(":"))
        record_server = record_time_cst.astimezone(source_tz)
        best_diff: Optional[float] = None
        best_dt: Optional[_dt.datetime] = None
        for offset in (0, -1, 1, -2, 2):
            candidate_date = record_server.date() + _dt.timedelta(days=offset)
            candidate_server = _dt.datetime.combine(
                candidate_date, _dt.time(h, mi, s), tzinfo=source_tz
            )
            diff = abs((candidate_server - record_server).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_dt = candidate_server
        if best_dt is not None:
            return best_dt.astimezone(CST), "仅时分秒，取记录日期附近"
    return record_time_cst, "时刻为记录时间近似"


def _parse_stamina(
    log_content: str,
    record_time_cst: _dt.datetime,
    script_name: str,
    now_cst: _dt.datetime,
    today_04: _dt.datetime,
    source_tz: _dt.tzinfo = SERVER_TZ,
) -> Dict[str, Any]:
    """从日志解析体力读数并推算实时值。

    返回：
    ``{"status": "known"/"unknown", "raw", "estimated", "read_time", "max", "stale", "ts_note", "label"}``
    """
    rule = STAMINA_RULES.get(script_name)
    if not rule:
        return {"status": "unknown", "reason": "该脚本日志不含体力读数"}

    if not log_content:
        return {"status": "unknown", "reason": "日志为空或不可用"}

    lines = log_content.splitlines()
    # 从后往前找最后一条匹配的行（最近一次读数）
    for line in reversed(lines):
        m = rule["pattern"].search(line)
        if not m:
            continue
        raw = int(m.group(1))
        max_val = rule["max"]
        if max_val is None and m.lastindex >= 2 and m.group(2):
            max_val = int(m.group(2))
        read_time_cst, ts_note = _parse_log_timestamp(line, record_time_cst, source_tz)

        # 推算：6 分钟恢复 1 点
        elapsed = (now_cst - read_time_cst).total_seconds()
        if elapsed < 0:
            elapsed = 0
        recovered = int(elapsed // 360)
        estimated = raw + recovered
        if max_val is not None:
            estimated = min(estimated, max_val)

        # stale：读取时刻不在今日 04:00 之后
        stale = read_time_cst < today_04
        # 读取早于今日 04:00 时仍按公式推算，但标记读数偏旧；结果封顶，不冒充实测。

        return {
            "status": "known",
            "label": rule["label"],
            "raw": raw,
            "estimated": estimated,
            "read_time": read_time_cst.isoformat(timespec="seconds"),
            "max": max_val,
            "stale": stale,
            "ts_note": ts_note,
        }

    return {"status": "unknown", "reason": "日志中未找到体力读数"}


def _error_excerpt(log_content: str, max_lines: int = 8) -> List[str]:
    """从日志中提取错误相关的行，供前端展示「上次失败的原文错误」。"""
    if not log_content:
        return []
    lines = [l.strip() for l in log_content.splitlines() if l.strip()
             and not re.search(r"(?i)(password|passwd|token|cookie|authorization|secret|密码)", l)]
    error_pat = re.compile(
        r"(?i)(error|失败|异常|traceback|exception|fatal|err|无法|不足)"
    )
    error_lines = [l for l in lines if error_pat.search(l) and len(l) < 500
                   ]
    if error_lines:
        chosen = error_lines[-max_lines:]
    else:
        chosen = lines[-max_lines:] if lines else []
    return [re.sub(r"(?<!\d)1\d{10}(?!\d)", "[手机号已隐藏]", line) for line in chosen]


def _daily_reward_excerpt(log_content: str) -> List[str]:
    return [re.sub(r"(?<!\d)1\d{10}(?!\d)", "[手机号已隐藏]", line.strip())
            for line in (log_content or "").splitlines()
            if re.search(r"每日奖励|未领取", line) and
            not re.search(r"(?i)(password|passwd|token|cookie|authorization|secret|密码)", line)][:8]


_MAA_CHAIN_NAMES = {
    "开始唤醒": "StartUp", "刷理智": "Fight", "基建换班": "Infrast", "自动公招": "Recruit",
    "信用购物": "Mall", "领取奖励": "Award", "仓库识别": "Depot", "更换主题": "SwitchTheme",
    "关闭游戏": "CloseDown", "启动游戏": "StartUp",
}


def _maa_failed_chains(detail_log: str) -> set:
    """MAA 失败子任务集合：两种真实格式都认。

    * ``TaskChainError {"taskchain":"SwitchTheme"}``（原生日志）
    * ``任务出错: 更换主题``（MAA 界面日志，被 AUTO-MAS 收进 detail log）
    """
    chains: set = set()
    for line in detail_log.splitlines():
        if "TaskChainError" in line:
            chains.update(re.findall(r'["\']taskchain["\']\s*:\s*["\']([^"\']+)', line))
        m = re.search(r"任务出错\s*[:：]\s*([^\s,，。]+)", line)
        if m:
            name = m.group(1).strip()
            chains.add(_MAA_CHAIN_NAMES.get(name, name))
    return chains


def _effective_status(record: Dict[str, Any], detail_log: str = "") -> str:
    raw = str(record.get("status", "")).lower()
    if re.search(r"更新(?:成功|完成)|已更新到", str(record.get("message", ""))):
        return "unknown"  # updater success is not a daily task completion
    if raw in {"running", "queued", "waiting", "pending"}:
        return "running"
    if raw in {"error", "failed", "failure"}:
        # 用户口径（异环189）：异环「异象家具」领取失败是脚本 UI 适配问题，
        # 无论 status 是 error 都按成功处理，与补跑脚本的 EXEMPT_FAILURE_PATTERNS 一致。
        if record.get("script_name") == "异环" and "异象家具" in str(record.get("message", "")):
            return "success"
        if record.get("script_name") == "明日方舟":
            failed_chains = _maa_failed_chains(detail_log)
            # 仅「更换主题」失败时按成功处理（用户口径）：该子任务失败不影响日常收货。
            if failed_chains and failed_chains <= {"SwitchTheme", "更换主题"}:
                return "success"
        return "error"
    if raw in {"success", "succeeded", "completed"}:
        if record.get("script_name") == "原神" and re.search(r"每日奖励.{0,8}未领取", str(record.get("message", "")) + "\n" + detail_log):
            return "warning"
        return "success"
    return "unknown"


def _same_minute_success(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A terminal success in the latest minute wins over same-minute retries."""
    if not records:
        return None
    ordered = sorted(records, key=lambda r: (str(r.get("time_cst", "")), str(r.get("id", ""))))
    latest = ordered[-1]
    minute = str(latest.get("time_cst", ""))[:16]
    same_minute = [r for r in ordered if str(r.get("time_cst", ""))[:16] == minute]
    successful = [r for r in same_minute if _effective_status(r) in ("success", "warning")]
    return successful[-1] if successful else latest


def _account_records(records: List[Dict[str, Any]], effective) -> List[Dict[str, Any]]:
    """账号卡展开后展示的今日运行情况（最新在前）。"""
    out: List[Dict[str, Any]] = []
    for r in sorted(records, key=lambda x: (str(x.get("time_cst", "")), str(x.get("id", ""))), reverse=True):
        status = effective(r)
        item = {"time": r.get("time_cst"), "status": status,
                "message": _safe_account_name(r.get("message", ""))}
        if status == "warning":
            item["message"] = item["message"] or "每日奖励未领取"
        out.append(item)
    return out


def _account_last_error(today: List[Dict[str, Any]], history: List[Dict[str, Any]],
                        effective, detail_log) -> Optional[Dict[str, Any]]:
    """该账号最近一次真实失败（豁免后的状态，不把仅更换主题失败算进来）。

    优先取今日（04:00 起）的失败；今日没有失败时回退到近 30 天的最近一次失败。
    """
    failed = [r for r in today if effective(r) == "error"]
    if not failed:
        # 回退到近 30 天时同样走豁免口径：仅更换主题失败不能算失败。
        # 先按原始状态筛出失败记录，再逐个走豁免判定，避免对每条历史都取日志。
        failed = [r for r in history
                  if str(r.get("status", "")).lower() in {"error", "failed", "failure"}
                  and effective(r) == "error"]
    if not failed:
        return None
    err = sorted(failed, key=lambda x: (str(x.get("time_cst", "")), str(x.get("id", ""))))[-1]
    try:
        excerpt = _error_excerpt(detail_log(err))
    except Exception:
        excerpt = []
    return {"time": err.get("time_cst"), "message": _safe_account_name(err.get("message", "")),
            "log_excerpt": excerpt}


def _aggregate_account_outcomes(account_stats: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[bool]]:
    """Aggregate script UI state without letting one account mask another."""
    if not account_stats:
        return None, None
    statuses = [str(a.get("last_status", "unknown")) for a in account_stats]
    if "running" in statuses:
        status = "running"
    elif "error" in statuses:
        status = "error"
    elif "unknown" in statuses:
        status = "unknown"
    elif "warning" in statuses:
        status = "warning"
    else:
        status = "success"
    success_today = all(a.get("success_today") and a.get("last_status") in ("success", "warning")
                        for a in account_stats)
    return status, bool(success_today)


_AUTO_MAS_TYPES = {
    "BetterGIConfig": "原神",
    "MaaEndConfig": "明日方舟：终末地",
    "MaaConfig": "明日方舟",
    "HSRConfig": "星穹铁道",
    "SrcConfig": "星穹铁道模拟器",
    "M9AConfig": "1999",
    "OkNteConfig": "异环",
    "OkwwConfig": "鸣潮",
}


def _configured_auto_mas_rosters() -> Dict[str, List[str]]:
    """Read configured AUTO-MAS script/account identities without touching secrets.

    Only Info.Name and Info.Status are used. ZZZ is intentionally omitted here:
    its current status and stamina are read from ZZZ-Onedragon's native logs.
    """
    if AUTO_MAS_SCRIPT_CONFIG is None or not AUTO_MAS_SCRIPT_CONFIG.is_file():
        return {}
    try:
        config = json.loads(AUTO_MAS_SCRIPT_CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    types = {str(x.get("uid")): str(x.get("type")) for x in config.get("instances", [])
             if isinstance(x, dict) and x.get("uid") and x.get("type")}
    rosters: Dict[str, List[str]] = {}
    for instance_id, script_type in types.items():
        script_name = _AUTO_MAS_TYPES.get(script_type)
        if not script_name or script_type == "ZzzOdConfig":
            continue
        group = config.get(instance_id, {})
        user_data = (group.get("SubConfigsInfo") or {}).get("UserData", {})
        if not isinstance(user_data, dict):
            continue
        names = []
        for account in user_data.values():
            if not isinstance(account, dict):
                continue
            info = account.get("Info") or {}
            name = str(info.get("Name") or "").strip()
            if name and info.get("Status") is not False:
                names.append(_safe_account_name(name))
        if names:
            rosters.setdefault(script_name, []).extend(names)
    return {name: sorted(set(accounts)) for name, accounts in rosters.items()}


def _snapshot_running_accounts(client: _MCPClient) -> Dict[str, Any]:
    """Read runtime snapshot and count only explicitly identified running accounts."""
    try:
        snapshot = client.call("get_task_runtime_snapshot_api_dispatch_runtime_snapshot_get", {})
    except Exception:
        return {"reachable": False, "pairs": set(), "active_tasks": 0}
    tasks = snapshot.get("tasks", {}) if isinstance(snapshot, dict) else {}
    pairs = set()
    active_tasks = len(tasks) if isinstance(tasks, (list, dict)) else 0
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            script = value.get("script_name") or value.get("scriptName")
            account = value.get("username") or value.get("account_name") or value.get("accountName")
            status = str(value.get("status", "")).lower()
            if script and account and status in {"running", "queued", "waiting", "pending"}:
                pairs.add((_safe_account_name(script), _safe_account_name(account),
                           "running" if status == "running" else "queued"))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(tasks)
    return {"reachable": True, "pairs": pairs, "active_tasks": active_tasks}


def _zzz_account_label(profile: str) -> Optional[str]:
    """Resolve a OneDragon profile to an AUTO-MAS display name without exposing IDs."""
    if ZZZ_ONEDRAGON_ROOT is None or AUTO_MAS_SCRIPT_CONFIG is None:
        return None
    game_account_file = ZZZ_ONEDRAGON_ROOT / "config" / profile / "game_account.yml"
    if not game_account_file.is_file() or not AUTO_MAS_SCRIPT_CONFIG.is_file():
        return None
    try:
        text = game_account_file.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"(?m)^account\s*:\s*['\"]?([^'\"\s#]+)", text)
        if not match:
            return None
        account_id = match.group(1)
        config = json.loads(AUTO_MAS_SCRIPT_CONFIG.read_text(encoding="utf-8-sig"))
        for instance in config.get("instances", []):
            if not isinstance(instance, dict) or instance.get("type") != "ZzzOdConfig":
                continue
            group = config.get(str(instance.get("uid")), {})
            user_data = (group.get("SubConfigsInfo") or {}).get("UserData", {})
            for account in user_data.values() if isinstance(user_data, dict) else []:
                if not isinstance(account, dict):
                    continue
                configured_id = str((account.get("Game") or {}).get("Account", ""))
                name = str((account.get("Info") or {}).get("Name", ""))
                if account_id and configured_id == account_id and name:
                    return _safe_account_name(name)
    except (OSError, ValueError, TypeError):
        return None
    return None


def _read_zzz_onedragon(now_cst: _dt.datetime, today_04: _dt.datetime) -> Optional[Dict[str, Any]]:
    """Read ZZZ status and stamina only from its own local OneDragon logs."""
    if ZZZ_ONEDRAGON_ROOT is None:
        return None
    log_dir = ZZZ_ONEDRAGON_ROOT / ".log"
    candidates = sorted(log_dir.glob("log.txt*"), key=lambda p: p.stat().st_mtime, reverse=True)
    def unknown_if_configured() -> Optional[Dict[str, Any]]:
        if not (ZZZ_ONEDRAGON_ROOT / "config" / "01" / "game_account.yml").is_file():
            return None
        account = _zzz_account_label("01")
        info = {"status": "unknown", "reason": "原生一条龙日志没有可解析的完成记录"}
        return {"script_name": "绝区零一条龙", "source": "ZZZ-Onedragon 本地原生日志",
                "accounts": [account] if account else [],
                "account_stats": ([{"account": account, "success_today": False,
                    "last_status": "unknown", "record_count": 0, "stamina": info}] if account else []),
                "record_count": 0, "success_today": False, "last_status": "unknown",
                "last_error": None, "records": [], "stamina": info, "configured": True}
    if not candidates:
        return unknown_if_configured()
    try:
        content = candidates[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return unknown_if_configured()
    # Keep only the latest complete top-level daily run; subtask failures that
    # later finish successfully are retained as detail, not promoted to failure.
    lines = content.splitlines()
    end = next((i for i in range(len(lines) - 1, -1, -1)
                if "指令[ 一条龙 ] 执行成功 返回状态 全部结束" in lines[i]
                or "指令[ 一条龙 ] 执行失败" in lines[i]), None)
    if end is None:
        return unknown_if_configured()
    start = 0
    for i in range(end - 1, -1, -1):
        if ("指令[ 一条龙 ] 执行成功 返回状态" in lines[i]
                or "指令[ 一条龙 ] 执行失败 返回状态" in lines[i]):
            start = i + 1
            break
    run_lines = lines[start:end + 1]
    success = "执行成功 返回状态 全部结束" in lines[end]
    ts_match = _LOG_TIME_ONLY_RE.search(lines[end])
    record_time = now_cst
    if ts_match:
        h, mi, sec = map(int, ts_match.group(1).split(":"))
        local_day = now_cst.date()
        record_time = _dt.datetime.combine(local_day, _dt.time(h, mi, sec), tzinfo=CST)
        if record_time - now_cst > _dt.timedelta(hours=12):
            record_time -= _dt.timedelta(days=1)
        elif now_cst - record_time > _dt.timedelta(hours=12):
            record_time += _dt.timedelta(days=1)
    account = _zzz_account_label("01")
    records = [{"time": record_time.isoformat(timespec="seconds"),
                "status": "success" if success else "error",
                "message": "原生一条龙执行完成" if success else "原生一条龙执行失败",
                "account": account or "",
                "log_excerpt": ([lines[end].strip()] if success else _error_excerpt("\n".join(run_lines)))}]
    stamina = _parse_stamina("\n".join(run_lines), record_time, "绝区零一条龙", now_cst, today_04,
                             source_tz=CST)
    # Older OneDragon builds log the same supported measurement as 「剩余电量 N」.
    if stamina.get("status") != "known":
        match = next((re.search(r"剩余电量\s*(\d+)", line) for line in reversed(run_lines)
                      if re.search(r"剩余电量\s*(\d+)", line)), None)
        if match:
            raw = int(match.group(1))
            stamina = {"status": "known", "label": "电量", "raw": raw,
                       "estimated": min(240, raw + max(0, int((now_cst-record_time).total_seconds() // 360))),
                       "max": 240, "read_time": record_time.isoformat(timespec="seconds"),
                       "stale": record_time < today_04, "ts_note": "原生日志记录时刻"}
    return {"script_name": "绝区零一条龙", "source": "ZZZ-Onedragon 本地原生日志",
            "accounts": [account] if account else [], "account_stats": ([{"account": account,
                "success_today": record_time >= today_04 and success,
                "last_status": "success" if success else "error",
                "record_count": int(record_time >= today_04), "stamina": stamina}] if account else []),
            "record_count": int(record_time >= today_04),
            "success_today": record_time >= today_04 and success,
            "last_status": "success" if success else "error",
            "last_error": None if success else {"time": record_time.isoformat(timespec="seconds"),
                "message": "原生一条龙执行失败", "account": account or "",
                "log_excerpt": _error_excerpt("\n".join(run_lines))},
            "records": records if record_time >= today_04 else [], "stamina": stamina,
            "configured": True}


def _read_m7a_instance(now_cst: _dt.datetime, today_04: _dt.datetime) -> Optional[Dict[str, Any]]:
    """Read M7A installation registry and recent local run evidence; never invoke it."""
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    registry = local / "March7thAssistant" / "installs.json"
    if not registry.is_file():
        return None
    try:
        installs = json.loads(registry.read_text(encoding="utf-8-sig")).get("installs", [])
    except Exception:
        return {"script_name": "三月七小助手（独立版）", "accounts": [], "account_stats": [],
                "last_status": "unknown", "success_today": False, "records": [],
                "stamina": {"status": "unknown", "reason": "独立版安装登记不可读取"}, "record_count": 0,
                "configured": True}
    for entry in installs[:1]:
        root = Path(str(entry.get("path", "")))
        logs = root / "logs"
        if not root.is_dir():
            continue
        runs = sorted(logs.glob("runs/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
        records = []
        stamina = {"status": "unknown", "reason": "独立版近期日志没有可解析的开拓力读数"}
        for run_path in runs:
            try:
                run = json.loads(run_path.read_text(encoding="utf-8-sig"))
                stamp = run.get("ended_at") or run.get("started_at")
                if not stamp:
                    continue
                when = _dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    continue
                when = when.astimezone(CST)
                status = "running" if run.get("status") in ("running", "active") else (
                    "success" if run.get("exit_code") == 0 else "error" if run.get("exit_code") not in (None, 0) else "unknown")
                records.append({"time": when.isoformat(timespec="seconds"), "status": status,
                                "message": str(run.get("task", "M7A 运行记录")), "account": ""})
                log_path = run_path.with_suffix(".log")
                if stamina.get("status") != "known" and log_path.is_file():
                    stamina = _parse_stamina(log_path.read_text(encoding="utf-8", errors="replace"),
                                             when, "三月七小助手", now_cst, today_04, source_tz=CST)
            except Exception:
                continue
        records.sort(key=lambda r: r["time"])
        daily = [r for r in records if _local_time(r["time"]) >= today_04]
        latest = daily[-1] if daily else None
        return {"script_name": "三月七小助手（独立版）", "source": "M7A 本地只读运行日志",
                "accounts": [], "account_stats": [], "record_count": len(daily),
                "success_today": any(r["status"] == "success" for r in daily),
                "last_status": latest["status"] if latest and daily else "unknown",
                "last_error": None, "records": daily, "stamina": stamina,
                "instance_path": str(root), "configured": True}
    return None


# --------------------------------------------------------------------------
# ChronoRail 即将到来
# --------------------------------------------------------------------------
def _load_chronorail() -> Dict[str, Any]:
    if CHRONORAIL_PATH is None or not CHRONORAIL_PATH.is_file():
        raise FileNotFoundError(
            "ChronoRail 数据文件未配置或不存在：请设置 WB_GAME_CHRONORAIL 环境变量，"
            "或在 wb-game/config.json 里填写 chronorail_path"
        )
    return json.loads(CHRONORAIL_PATH.read_text(encoding="utf-8"))


def _load_chronorail_catalog() -> List[Dict[str, str]]:
    """只读载入 ChronoRail 的游戏图标/颜色目录；失败由调用方报告。"""
    if CHRONORAIL_CATALOG_PATH is None or not CHRONORAIL_CATALOG_PATH.is_file():
        raise FileNotFoundError(
            "ChronoRail 游戏目录未配置或不存在：请设置 WB_GAME_CHRONORAIL_CATALOG 环境变量，"
            "或在 wb-game/config.json 里填写 chronorail_catalog_path"
        )
    payload = json.loads(CHRONORAIL_CATALOG_PATH.read_text(encoding="utf-8"))
    games = payload.get("games")
    if not isinstance(games, list):
        raise ValueError("ChronoRail 游戏目录 games 必须是数组")
    return [{key: str(game[key]) for key in ("id", "name", "icon", "color")}
            for game in games if isinstance(game, dict)
            and all(isinstance(game.get(key), str) and game[key] for key in ("id", "name", "icon", "color"))]


def _is_banner_estimated(de: Dict[str, Any], banner_idx: int, field: str) -> bool:
    """检查某个 banner 字段是否在 dateEstimation.pending 列表中。"""
    if not de or not de.get("pending"):
        return False
    fields = de.get("fields", [])
    return f"banners.{banner_idx}.{field}" in fields


def _compute_upcoming() -> List[Dict[str, Any]]:
    """照抄 UpcomingHero.tsx 的「即将到来」算法。

    1. 先取 startDate >= 今天的最早一条（开池，phase=start）
    2. 否则取 startDate <= 今天 <= endDate 中最早结束的一条（关池，phase=end）
    3. 否则取 startDate > 今天的最早版本（type=version）
    按 daysUntil 排序。
    """
    data = _load_chronorail()
    today = _dt.date.today()
    today_str = today.isoformat()

    items: List[Dict[str, Any]] = []
    for game_id, game in data.get("games", {}).items():
        if not isinstance(game, dict):
            continue
        game_name = game.get("gameName", game_id)

        # 收集所有 banner（带版本和索引信息，用于估算标记）
        banners_with_meta: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], int]] = []
        for v in game.get("versions", []):
            if not isinstance(v, dict):
                continue
            v_de = v.get("dateEstimation") or {}
            for i, b in enumerate(v.get("banners", [])):
                if isinstance(b, dict):
                    banners_with_meta.append((b, v, v_de, i))

        # 1. 未来开池
        future = [
            (b, v, de, i)
            for b, v, de, i in banners_with_meta
            if b.get("startDate", "") and b["startDate"] >= today_str
        ]
        future.sort(key=lambda x: x[0].get("startDate", ""))
        if future:
            b, v, de, i = future[0]
            days = (_parse_date(b["startDate"]) - today).days
            if days is None:
                continue
            est = _is_banner_estimated(de, i, "startDate") or _is_banner_estimated(
                de, i, "endDate"
            )
            items.append({
                "game_id": game_id,
                "game_name": game_name,
                "type": "banner",
                "phase": "start",
                "name": b.get("name", ""),
                "character": b.get("character", ""),
                "start_date": b.get("startDate", ""),
                "end_date": b.get("endDate", ""),
                "days_until": days,
                "estimated": est,
            })
            continue

        # 2. 进行中，最早结束
        ongoing = [
            (b, v, de, i)
            for b, v, de, i in banners_with_meta
            if b.get("startDate", "")
            and b.get("endDate", "")
            and b["startDate"] <= today_str <= b["endDate"]
        ]
        if ongoing:
            ongoing.sort(key=lambda x: x[0].get("endDate", ""))
            b, v, de, i = ongoing[0]
            days = (_parse_date(b["endDate"]) - today).days
            if days is None:
                continue
            est = _is_banner_estimated(de, i, "endDate")
            items.append({
                "game_id": game_id,
                "game_name": game_name,
                "type": "banner",
                "phase": "end",
                "name": b.get("name", ""),
                "character": b.get("character", ""),
                "start_date": b.get("startDate", ""),
                "end_date": b.get("endDate", ""),
                "days_until": days,
                "estimated": est,
            })
            continue

        # 3. 未来版本
        future_versions = [
            v
            for v in game.get("versions", [])
            if isinstance(v, dict)
            and v.get("startDate", "")
            and v["startDate"] > today_str
        ]
        future_versions.sort(key=lambda v: v.get("startDate", ""))
        if future_versions:
            v = future_versions[0]
            days = (_parse_date(v["startDate"]) - today).days
            if days is None:
                continue
            est = bool((v.get("dateEstimation") or {}).get("pending"))
            items.append({
                "game_id": game_id,
                "game_name": game_name,
                "type": "version",
                "phase": None,
                "name": f"v{v.get('version', '')}",
                "character": "",
                "start_date": v.get("startDate", ""),
                "end_date": v.get("endDate", ""),
                "days_until": days,
                "estimated": est,
            })
            continue

    items.sort(key=lambda x: x["days_until"])
    return items


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
def _cache_get() -> Optional[Dict[str, Any]]:
    with _CACHE_LOCK:
        now = time.time()
        if _CACHE["data"] is not None and now - float(_CACHE["at"]) < _CACHE_TTL:
            return _CACHE["data"]
    return None


def _cache_set(data: Dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE["at"] = time.time()
        _CACHE["data"] = data


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@router.get("/health")
def health() -> Dict[str, Any]:
    mcp_ok = False
    mcp_error: Optional[str] = None
    try:
        client = _MCPClient()
        mcp_ok = True
        client.close()
    except Exception as e:
        mcp_error = str(e)

    cr_ok = CHRONORAIL_PATH is not None and CHRONORAIL_PATH.is_file()
    cr_meta: Dict[str, Any] = {
        "reachable": cr_ok,
        "path": str(CHRONORAIL_PATH) if CHRONORAIL_PATH else None,
    }
    if cr_ok:
        try:
            data = _load_chronorail()
            cr_meta["fetched_at"] = data.get("fetchedAt")
            cr_meta["game_count"] = len(data.get("games", {}))
        except Exception as e:
            cr_meta["error"] = str(e)
    else:
        cr_meta["error"] = (
            f"ChronoRail 数据文件不存在：{CHRONORAIL_PATH}"
            if CHRONORAIL_PATH
            else "ChronoRail 数据文件未配置：设置 WB_GAME_CHRONORAIL 或 config.json 的 chronorail_path"
        )

    return {
        "ok": True,
        "plugin": "wb-game",
        "mcp": {"reachable": mcp_ok, "error": mcp_error, "url": MCP_URL},
        "chronorail": cr_meta,
    }


@router.get("/overview")
def overview() -> Dict[str, Any]:
    cached = _cache_get()
    if cached is not None:
        return cached

    now_cst = _dt.datetime.now(CST)
    today_04 = _dt.datetime.combine(now_cst.date(), _dt.time(4, 0), tzinfo=CST)

    result: Dict[str, Any] = {
        "generated_at": now_cst.isoformat(timespec="seconds"),
        "window": {
            "start": today_04.isoformat(timespec="seconds"),
            "end": now_cst.isoformat(timespec="seconds"),
            "started": now_cst >= today_04,
        },
        "sources": {},
        "scripts": [],
        "upcoming": [],
        "has_records": False,
    }

    # ---- 1. MCP 运行记录 ----
    mcp_ok = False
    mcp_error: Optional[str] = None
    records: List[Dict[str, Any]] = []
    history_records: List[Dict[str, Any]] = []
    client: Optional[_MCPClient] = None
    try:
        client = _MCPClient()
        history_start = _dt.datetime.combine(
            now_cst.date() - _dt.timedelta(days=30), _dt.time.min, tzinfo=CST
        )
        history = client.call(
            "search_history_api_history_search_post",
            {
                "mode": "DAILY",
                "start_date": history_start.date().isoformat(),
                "end_date": now_cst.date().isoformat(),
            },
        )
        # 运行状态严格限定在今日04:00起；体力与上次错误从近30天记录中取最近值。
        history_records = _flatten_window(history, history_start, now_cst)
        records = _flatten_window(history, today_04, now_cst)
        mcp_ok = True
    except Exception as e:
        mcp_error = str(e)

    result["sources"]["mcp"] = {
        "reachable": mcp_ok,
        "error": mcp_error,
        "url": MCP_URL,
    }

    # Configuration is roster evidence, not execution evidence. Keep it even
    # when MCP is unavailable so configured scripts can show an unknown light.
    configured_rosters = _configured_auto_mas_rosters()
    snapshot_state = (_snapshot_running_accounts(client) if client is not None
                      else {"reachable": False, "pairs": set(), "active_tasks": 0})
    snapshot_pairs = snapshot_state["pairs"]
    result["sources"]["runtime_snapshot"] = {
        "reachable": snapshot_state["reachable"],
        "active_tasks": snapshot_state["active_tasks"],
        "account_mapped_running": len(snapshot_pairs),
        "note": "快照有任务但缺少可映射账号时不计入账号结果",
    }

    if mcp_ok:
        result["has_records"] = bool(records)
        detail_cache: Dict[str, str] = {}

        def detail_log(record: Dict[str, Any]) -> str:
            rid = str(record.get("id", ""))
            if not rid or rid.startswith("snapshot:"):
                return ""
            if rid not in detail_cache:
                try:
                    detail = client.call("get_history_detail_api_history_detail_post", {"recordId": rid})
                    detail_cache[rid] = (detail.get("data", {}) or {}).get("log_content", "") or ""
                except Exception:
                    detail_cache[rid] = ""
            return detail_cache[rid]

        def effective(record: Dict[str, Any]) -> str:
            log_content = ""
            raw_status = str(record.get("status", "")).lower()
            if ((record.get("script_name") == "明日方舟" and raw_status in {"error", "failed", "failure"})
                    or (record.get("script_name") == "原神" and raw_status in {"success", "succeeded", "completed"})):
                log_content = detail_log(record)
            return _effective_status(record, log_content)
        # 分别保留今日记录（UI统计）和近30天记录（上次错误/最近成功体力读数）。
        by_script: Dict[str, List[Dict[str, Any]]] = {}
        for r in records:
            sname = r.get("script_name", "")
            if sname:
                by_script.setdefault(sname, []).append(r)
        by_history_script: Dict[str, List[Dict[str, Any]]] = {}
        for r in history_records:
            sname = r.get("script_name", "")
            if sname:
                by_history_script.setdefault(sname, []).append(r)

        scripts: List[Dict[str, Any]] = []
        for sname in sorted(set(by_script) | set(by_history_script) | set(configured_rosters)):
            recs = by_script.get(sname, [])
            history_recs = by_history_script.get(sname, [])
            recs.sort(key=lambda r: (r["time_cst"], str(r.get("id", ""))))
            history_recs.sort(key=lambda r: (r["time_cst"], str(r.get("id", ""))))
            accounts = sorted(set(configured_rosters.get(sname, [])) |
                              {_safe_account_name(r.get("username", "")) for r in history_recs if r.get("username")})
            live_for_script = {(name, account, state) for name, account, state in snapshot_pairs
                               if name == sname}
            for _, account_name, state in live_for_script:
                if account_name not in accounts:
                    accounts.append(account_name)
                safe_account = _safe_account_name(account_name)
                recs.append({"time_cst": now_cst.isoformat(timespec="seconds"),
                             "id": "snapshot:" + safe_account, "username": safe_account,
                             "script_name": sname, "status": state,
                             "message": "AUTO-MAS 实时运行快照"})
            recs.sort(key=lambda r: (r["time_cst"], str(r.get("id", ""))))
            accounts.sort()

            # 体力推算：取近30天最近一条成功记录；找不到成功日志才显示未知。
            stamina_info: Dict[str, Any] = {"status": "unknown", "reason": "近30天未找到成功读取记录"}
            succ_recs = [r for r in history_recs if r.get("status") == "success"]
            for reading in reversed(succ_recs):
                try:
                    stamina_info = _parse_stamina(detail_log(reading), _local_time(reading["time"]),
                                                  sname, now_cst, today_04)
                except Exception as e:
                    stamina_info = {"status": "unknown", "reason": f"体力日志读取失败：{e}"}
                if stamina_info.get("status") == "known":
                    break

            # 若今日没有成功，展示近30天最近一次失败；今日记录仍严格限于04:00起。
            last_error: Optional[Dict[str, Any]] = None
            if not any(effective(r) in ("success", "warning") for r in recs):
                err_recs = [r for r in history_recs
                            if str(r.get("status", "")).lower() in {"error", "failed", "failure"}
                            and effective(r) == "error"]
                if err_recs:
                    last_err = err_recs[-1]
                    err_detail: Dict[str, Any] = {
                        "time": last_err["time_cst"],
                        "message": last_err.get("message", ""),
                        "account": last_err.get("username", ""),
                    }
                    # 尝试取日志摘录
                    try:
                        detail = client.call(
                            "get_history_detail_api_history_detail_post",
                            {"recordId": last_err["id"]},
                        )
                        log_content = (detail.get("data", {}) or {}).get(
                            "log_content", ""
                        ) or ""
                        err_detail["log_excerpt"] = _error_excerpt(log_content)
                    except Exception:
                        err_detail["log_excerpt"] = []
                    last_error = err_detail

            # Per-instance rollup: account identity remains scoped to this script,
            # so the same phone suffix/name in another script is never merged.
            account_stats = []
            instance_names = accounts
            for account_name in instance_names:
                acct_today = [r for r in recs if _safe_account_name(r.get("username")) == account_name]
                acct_history = [r for r in history_recs if _safe_account_name(r.get("username")) == account_name]
                acct_success = [r for r in acct_today if effective(r) in ("success", "warning")]
                acct_latest = _same_minute_success(acct_today)
                acct_status = effective(acct_latest) if acct_latest else "unknown"
                acct_stamina = {"status": "unknown", "reason": "近30天未找到可解析的成功读数"}
                acct_readings = [r for r in acct_history if r.get("status") == "success"]
                for reading in reversed(acct_readings):
                    try:
                        acct_stamina = _parse_stamina(detail_log(reading), _local_time(reading["time"]),
                                                      sname, now_cst, today_04)
                    except Exception as e:
                        acct_stamina = {"status": "unknown", "reason": f"只读日志不可用：{e}"}
                    if acct_stamina.get("status") == "known":
                        break
                account_stats.append({
                    "account": account_name,
                    "success_today": bool(acct_success),
                    "last_status": acct_status,
                    "record_count": len(acct_today),
                    "stamina": acct_stamina,
                    "records": _account_records(acct_today, effective),
                    "last_error": _account_last_error(acct_today, acct_history, effective, detail_log),
                })

            last_status, success_today = _aggregate_account_outcomes(account_stats)
            if last_status is None:
                success_today = any(effective(r) in ("success", "warning") for r in recs)
                last_status = effective(_same_minute_success(recs) or recs[-1]) if recs else "unknown"
            if last_status == "error" and last_error is None:
                failed = [r for r in recs if effective(r) == "error"]
                if failed:
                    err = failed[-1]
                    log_content = detail_log(err)
                    last_error = {"time": err["time_cst"],
                                  "message": _safe_account_name(err.get("message", "")),
                                  "account": _safe_account_name(err.get("username", "")),
                                  "log_excerpt": _error_excerpt(log_content)}
            record_list = []
            for r in recs:
                status = effective(r)
                message = _safe_account_name(r.get("message", ""))
                entry = {"time": r["time_cst"], "status": status,
                         "message": message, "account": _safe_account_name(r.get("username", ""))}
                if status == "warning" and sname == "原神":
                    entry["message"] = "每日奖励未领取"
                    entry["log_excerpt"] = _daily_reward_excerpt(detail_log(r))
                record_list.append(entry)

            scripts.append(
                {
                    "script_name": sname,
                    "accounts": accounts,
                    "record_count": len(recs),
                    "success_today": success_today,
                    "last_status": last_status,
                    "last_error": last_error,
                    "records": record_list,
                    "stamina": stamina_info,
                    "account_stats": account_stats,
                    "configured": sname in configured_rosters,
                }
            )

        result["scripts"] = scripts
    else:
        result["scripts"] = [{
            "script_name": sname, "accounts": roster, "record_count": 0,
            "success_today": False, "last_status": "unknown", "last_error": None,
            "records": [], "stamina": {"status": "unknown", "reason": "AUTO-MAS MCP 不可达"},
            "account_stats": [{"account": account, "success_today": False,
                "last_status": "unknown", "record_count": 0,
                "stamina": {"status": "unknown", "reason": "AUTO-MAS MCP 不可达"}}
                for account in roster], "configured": True,
        } for sname, roster in configured_rosters.items()]

    # Standalone March7thAssistant is an independent source, never an AUTO-MAS account.
    try:
        m7a = _read_m7a_instance(now_cst, today_04)
        if m7a:
            result["scripts"].append(m7a)
            result["sources"]["m7a"] = {"reachable": True, "label": "独立版三月七小助手只读日志"}
    except Exception as e:
        result["sources"]["m7a"] = {"reachable": False, "error": str(e)}

    # ZZZ's local OneDragon logs are authoritative for its native task and
    # stamina outcomes. AUTO-MAS ZZZ rows are excluded by _flatten_window.
    try:
        zzz = _read_zzz_onedragon(now_cst, today_04)
        if zzz:
            result["scripts"].append(zzz)
            result["sources"]["zzz_onedragon"] = {
                "reachable": True, "label": "ZZZ-Onedragon 本地原生日志"}
        else:
            result["sources"]["zzz_onedragon"] = {
                "reachable": False, "error": "没有可解析的原生一条龙日志"}
    except Exception as e:
        result["sources"]["zzz_onedragon"] = {"reachable": False, "error": str(e)}

    # ---- 2. ChronoRail 即将到来 + 图标目录 ----
    cr_source: Dict[str, Any] = {
        "path": str(CHRONORAIL_PATH) if CHRONORAIL_PATH else None
    }
    try:
        upcoming = _compute_upcoming()
        result["upcoming"] = upcoming
        cr_source.update({"reachable": True, "count": len(upcoming)})
    except Exception as e:
        cr_source.update({"reachable": False, "error": str(e)})
        result["upcoming"] = []
    try:
        cr_source["games"] = _load_chronorail_catalog()
        cr_source["catalog_reachable"] = True
        cr_source["catalog_path"] = str(CHRONORAIL_CATALOG_PATH)
    except Exception as e:
        cr_source["games"] = []
        cr_source["catalog_reachable"] = False
        cr_source["catalog_path"] = (
            str(CHRONORAIL_CATALOG_PATH) if CHRONORAIL_CATALOG_PATH else None
        )
        cr_source["catalog_error"] = str(e)
    result["sources"]["chronorail"] = cr_source

    if client is not None:
        client.close()

    result["has_records"] = result["has_records"] or any(
        bool(script.get("records")) for script in result["scripts"]
    )

    _cache_set(result)
    return result
