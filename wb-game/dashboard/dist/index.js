/*
 * wb-game — 游戏模块：今日运行情况 + 体力推算 + 卡池即将到来
 *
 * ── 层级 ────────────────────────────────────────────────────────────
 * 主（primary）：选中脚本的今日运行时间线（04:00 起，含每条 status/message/时间）
 * 次（secondary）：左栏脚本列表（选中态唯一，显示今日状态点）
 * 三（tertiary）：体力卡（原始 → 推算）、卡池即将到来列表、数据源健康
 *
 * 纯 IIFE + React.createElement，不打包、无 JSX、无 import。
 * 入口：window.__HERMES_PLUGIN_SDK__，最后 window.__HERMES_PLUGINS__.register。
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useMemo = SDK.hooks.useMemo;
  var useCallback = SDK.hooks.useCallback;
  var fetchJSON = SDK.fetchJSON;
  var useRef = SDK.hooks.useRef;

  var BASE = "/api/plugins/wb-game";
  var NS = "wbg";
  var CHAT_VENDOR = "/dashboard-plugins/wb-game/dist/vendor/";
  function loadGameChat() {
    return new Promise(function (resolve, reject) {
      if (window.__WB_CHAT__) return resolve(window.__WB_CHAT__);
      if (!document.querySelector("link[data-wbg-chat-css]")) {
        var link = document.createElement("link"); link.rel = "stylesheet";
        link.href = CHAT_VENDOR + "wb-agent-chat.css"; link.setAttribute("data-wbg-chat-css", "1");
        document.head.appendChild(link);
      }
      window.__WB_CHAT_READY__ = resolve;
      if (document.querySelector("script[data-wbg-chat-js]")) return;
      var s = document.createElement("script"); s.src = CHAT_VENDOR + "wb-agent-chat.js"; s.async = true;
      s.setAttribute("data-wbg-chat-js", "1"); s.onerror = function () { reject(new Error("共享 Hermes 对话组件加载失败")); };
      document.head.appendChild(s);
    });
  }

  // 游戏图标：由 tools/sync-game-icons.py 从 ChronoRail 同步**官方 App Store 图标**
  // （已下载到 dist/game-icons/<id>.jpg）+ 清单 dist/game-icons.js。加载失败就退化成
  // 首字占位，不影响脚本列表本身。
  var ICONS_SRC = "/dashboard-plugins/wb-game/dist/game-icons.js";
  function loadGameIcons() {
    return new Promise(function (resolve) {
      if (window.WBG_GAME_ICONS) return resolve(window.WBG_GAME_ICONS);
      if (document.querySelector("script[data-wbg-icons-js]")) {
        return setTimeout(function () { resolve(window.WBG_GAME_ICONS || {}); }, 300);
      }
      window.__WBG_ICONS_READY__ = resolve;
      var s = document.createElement("script");
      s.src = ICONS_SRC; s.async = true;
      s.setAttribute("data-wbg-icons-js", "1");
      s.onerror = function () { resolve({}); };
      // 加载成功立即 resolve；没有这句只能等 5s 兜底定时器，图标会晚 5 秒才出现
      s.onload = function () { resolve(window.WBG_GAME_ICONS || {}); };
      document.head.appendChild(s);
      // 同步脚本缺失/被拦截时不能一直挂着
      setTimeout(function () { resolve(window.WBG_GAME_ICONS || {}); }, 5000);
    });
  }

  /** 游戏名归一化：全/半角冒号、空格、大小写都不该影响匹配。 */
  function normGameName(value) {
    return String(value || "").replace(/[：:]/g, ":").replace(/\s+/g, "").toLowerCase();
  }

  /** 按游戏名找官方图标（清单里带别名）；找不到返回 null，不伪造图标。 */
  function gameIconMeta(icons, gameName) {
    if (!icons) return null;
    var key = normGameName(gameName);
    if (!key) return null;
    var ids = Object.keys(icons);
    for (var i = 0; i < ids.length; i++) {
      var meta = icons[ids[i]];
      if (!meta || !meta.src) continue;
      if (normGameName(ids[i]) === key || normGameName(meta.name) === key) return meta;
      var aliases = meta.aliases || [];
      for (var j = 0; j < aliases.length; j++) {
        if (normGameName(aliases[j]) === key) return meta;
      }
    }
    return null;
  }

  function gameForScript(name) {
    if (/终末地|MaaEnd/i.test(name || "")) return "明日方舟：终末地";
    if (/星穹铁道|三月七|崩铁/.test(name || "")) return "崩坏：星穹铁道";
    if (/原神/.test(name || "")) return "原神";
    if (/绝区零|ZZZ/.test(name || "")) return "绝区零";
    if (/明日方舟/.test(name || "")) return "明日方舟";
    if (/异环/.test(name || "")) return "异环";
    if (/鸣潮/.test(name || "")) return "鸣潮";
    if (/1999/.test(name || "")) return "重返未来：1999";
    return name || "其他游戏";
  }

  // 状态色三档：成功 / 失败 / 未知
  var STATUS_COLORS = {
    success: "#2f8f5b",
    error: "#d33f3f",
    warning: "#ca8a04",
    running: "#2563eb",
    unknown: "#6b7280",
  };
  var STATUS_LABEL = { success: "成功", error: "失败", no_run: "今日未运行", unknown: "未知" };

  // 状态灯三态：有异常 → 红；有未运行/未知 → 灰；全部成功 → 绿。
  function lightOf(accounts, configured) {
    if (!configured) return null;
    var list = accounts && accounts.length ? accounts : [{}];
    var anyRun = false, anyOk = true;
    for (var i = 0; i < list.length; i++) {
      var s = list[i] && list[i].last_status;
      if (s === "error") return STATUS_COLORS.error;
      if (s === "success" || s === "warning") { anyRun = true; } else { anyOk = false; }
    }
    return anyRun && anyOk ? STATUS_COLORS.success : STATUS_COLORS.unknown;
  }

  // ---------------------------------------------------------------- 日期
  function parseDay(s) {
    if (!s || typeof s !== "string") return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
    if (!m) return null;
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  }

  function fmtTime(iso) {
    if (!iso) return "";
    // ISO 带时区，截取到分秒
    var s = iso.slice(0, 16).replace("T", " ");
    return s;
  }

  function fmtDays(d) {
    if (d === 0) return "今天";
    if (d === 1) return "明天";
    if (d === 2) return "后天";
    return d + "天后";
  }

  // ---------------------------------------------------------------- 小组件
  function StatusPill(props) {
    var s = props.status || "unknown";
    var color = STATUS_COLORS[s] || STATUS_COLORS.unknown;
    return h("span", { className: NS + "-pill" },
      h("i", { className: NS + "-dot", style: { background: color } }),
      STATUS_LABEL[s] || s);
  }

  function Empty(props) {
    return h("div", { className: NS + "-empty" },
      h("p", { className: NS + "-empty-title" }, props.title),
      props.hint ? h("p", { className: NS + "-empty-hint" }, props.hint) : null);
  }

  // ---------------------------------------------------------------- 体力（只保留推算值）
  function StaminaCard(props) {
    var s = props.stamina || {};
    if (!s || s.status !== "known") {
      return h("span", { className: NS + "-stamina-inline " + NS + "-stamina-unknown-text" },
        "体力 未知");
    }
    var max = s.max;
    return h("span", { className: NS + "-stamina-inline", title: "推算值（6 分钟恢复 1 点）" },
      "体力 ", h("b", { className: NS + "-stamina-val-est" }, String(s.estimated)),
      max != null ? " / " + max : "");
  }

  // ---------------------------------------------------------------- 账号卡（可折叠）
  // 收起时也能看到状态灯与体力推算值；展开后是今日运行情况 + 上次失败详情（若有）。
  function AccountCard(props) {
    var a = props.account || {};
    var records = a.records || [];
    var err = a.last_error;
    var body = h("div", { className: NS + "-acc-body" },
      h("div", { className: NS + "-acc-line" },
        h("span", { className: NS + "-acc-label" }, "今日运行情况"),
        h("span", { className: NS + "-acc-count" }, records.length ? records.length + " 条记录" : "暂无记录")),
      records.length ? h("ul", { className: NS + "-acc-records" }, records.map(function (r, i) {
        return h("li", { key: i }, fmtTime(r.time) + " · " + (STATUS_LABEL[r.status] || r.status) +
          (r.message ? " · " + r.message : ""));
      })) : h("p", { className: NS + "-acc-empty" }, "今日 04:00 起没有该账号的运行记录。"),
      err ? h("div", { className: NS + "-acc-err" },
        h("strong", null, "上次失败 · " + fmtTime(err.time)),
        err.message ? h("p", null, err.message) : null,
        (err.log_excerpt || []).length
          ? h("details", { className: NS + "-acc-evidence" },
              h("summary", null, "日志证据"), h("pre", null, err.log_excerpt.join("\n")))
          : null) : null);
    return h("details", { className: NS + "-account-card", open: props.open },
      h("summary", null,
        h("b", null, a.account || "未命名账号"),
        h("span", { className: NS + "-acc-sum-meta" },
          h(StatusPill, { status: a.last_status || "unknown" }),
          h(StaminaCard, { stamina: a.stamina || {} }))),
      body);
  }

  // ---------------------------------------------------------------- 卡池即将到来
  function UpcomingList(props) {
    var items = props.items || [];
    if (!items.length) {
      return h("div", { className: NS + "-card " + NS + "-upcoming-card" },
        h("div", { className: NS + "-card-head" },
          h("h3", { className: NS + "-card-title" }, "卡池即将到来")),
        h(Empty, { title: "暂无即将到来的卡池或版本", hint: "数据来自 ChronoRail 本地仓库。" }));
    }

    return h("div", { className: NS + "-card " + NS + "-upcoming-card" },
      h("div", { className: NS + "-card-head" },
        h("h3", { className: NS + "-card-title" }, "卡池即将到来"),
        h("span", { className: NS + "-badge " + NS + "-badge-muted" }, items.length + " 条")),
      h("div", { className: NS + "-upcoming-list" },
        items.map(function (it, i) {
          var isEnd = it.phase === "end";
          var daysText;
          if (isEnd) {
            daysText = it.days_until <= 0 ? "今天结束" : fmtDays(it.days_until) + "结束";
          } else {
            daysText = it.days_until <= 0 ? "今天开池" : fmtDays(it.days_until);
          }
          var urgent = it.days_until <= 3;
          return h("div", { key: i, className: NS + "-up-item" + (urgent ? " " + NS + "-up-urgent" : "") },
            h("div", { className: NS + "-up-main" },
              h("span", { className: NS + "-up-game" }, it.game_name),
              h("span", { className: NS + "-up-name" }, it.name),
              it.character ? h("span", { className: NS + "-up-char" }, it.character) : null,
              it.estimated ? h("span", { className: NS + "-tag " + NS + "-tag-est" }, "估算") : null),
            h("div", { className: NS + "-up-right" },
              h("span", { className: NS + "-up-days", style: { color: urgent ? STATUS_COLORS.error : null } }, daysText),
              h("span", { className: NS + "-up-date" },
                isEnd ? it.end_date.slice(5) : it.start_date.slice(5))));
        })));
  }

  // ---------------------------------------------------------------- 数据源健康
  function SourceHealth(props) {
    var sources = props.sources || {};
    var mcp = sources.mcp || {};
    var cr = sources.chronorail || {};
    return h("div", { className: NS + "-sources" },
      h("span", { className: NS + "-src " + (mcp.reachable ? NS + "-src-ok" : NS + "-src-err") },
        h("i", { className: NS + "-src-dot" }), "AUTO-MAS MCP" + (mcp.reachable ? " 在线" : " 离线")),
      h("span", { className: NS + "-src " + (cr.reachable ? NS + "-src-ok" : NS + "-src-err") },
        h("i", { className: NS + "-src-dot" }), "ChronoRail" + (cr.reachable ? " 在线" : " 缺失")));
  }

  // ---------------------------------------------------------------- 页面
  function GamePage() {
    var [data, setData] = useState(null);
    var [loadError, setLoadError] = useState(null);
    var [selected, setSelected] = useState(null);
    var [refreshing, setRefreshing] = useState(false);
    var [chatApi, setChatApi] = useState(null);
    var [chatOpen, setChatOpen] = useState(false);
    var [chatMobile, setChatMobile] = useState(false);
    var [chatBusy, setChatBusy] = useState(false);
    var [gamesOpen, setGamesOpen] = useState(false);
    var [icons, setIcons] = useState(null);
    var rootRef = useRef(null);
    useEffect(function () { loadGameChat().then(setChatApi).catch(function () {}); }, []);
    useEffect(function () { loadGameIcons().then(setIcons).catch(function () {}); }, []);

    function load() {
      setRefreshing(true);
      setLoadError(null);
      return fetchJSON(BASE + "/overview")
        .then(function (d) {
          if (!d || typeof d !== "object") {
            setLoadError("后端返回了空响应，插件后端可能需要重启才能加载新版 plugin_api.py。");
            return;
          }
          setData(d);
          // 默认选中第一个有记录的脚本，否则第一个
          var scripts = d.scripts || [];
          setSelected(function (prev) {
            if (prev) {
              for (var i = 0; i < scripts.length; i++) {
                if (scripts[i].script_name === prev) return prev;
              }
            }
            return scripts.length ? scripts[0].script_name : null;
          });
        })
        .catch(function (e) {
          setLoadError(String(e && e.message || e));
        })
        .then(function () { setRefreshing(false); });
    }

    useEffect(function () { load(); }, []);

    var current = useMemo(function () {
      if (selected && selected.indexOf("game:") === 0) return null;
      if (!data || !data.scripts) return null;
      for (var i = 0; i < data.scripts.length; i++) {
        if (data.scripts[i].script_name === selected) return data.scripts[i];
      }
      return data.scripts.length ? data.scripts[0] : null;
    }, [data, selected]);

    var games = useMemo(function () {
      var map = {};
      ((data && data.scripts) || []).forEach(function (s) {
        var game = gameForScript(s.script_name); (map[game] || (map[game] = [])).push(s);
      });
      ((data && data.upcoming) || []).forEach(function (item) {
        if (item.game_name) { var game = gameForScript(item.game_name); if (!map[game]) map[game] = []; }
      });
      return map;
    }, [data]);

    var scripts = (data && data.scripts) || [];
    var mcpOk = data && data.sources && data.sources.mcp && data.sources.mcp.reachable;
    var generatedAt = data && data.generated_at;

    // ---------- 加载中 ----------
    if (loadError && !data) {
      return h("div", { className: NS + "-root" },
        h(Empty, { title: "查询失败", hint: loadError + "（/api/plugins/wb-game/overview）" }));
    }
    if (!data) {
      return h("div", { className: NS + "-root" },
        h("div", { className: NS + "-hint" }, "加载游戏数据…"));
    }

    // ---------- 正常或 MCP 不可达（卡池与对话仍可用） ----------
    var selectedGame = selected && selected.indexOf("game:") === 0 ? selected.slice(5) : current ? gameForScript(current.script_name) : Object.keys(games)[0];
    var gameScripts = games[selectedGame] || [];
    var chatComponent = chatApi ? chatApi.makePanel(SDK) : null;
    var chatNode = chatComponent ? h(chatComponent, {
      key: "game-chat-" + (current ? current.script_name : selectedGame || "none"),
      moduleId: "wb-game", objectId: current ? current.script_name : selectedGame || "overview",
      objectName: (selectedGame || "游戏") + (current ? " · " + current.script_name : ""),
      className: NS + "-chat-panel", onBusy: setChatBusy,
      onCollapse: function () { setChatMobile(false); setChatOpen(false); },
      getContext: function () {
        return { module: "游戏运行面板（wb-game）", object: current ? current.script_name : selectedGame,
          facts: current ? ["游戏：" + gameForScript(current.script_name), "脚本：" + current.script_name,
            "账号：" + ((current.accounts || []).join("、") || "当前查询窗口没有账号记录"),
            "今日成功：" + (current.success_today ? "有运行记录" : "暂无已确认成功记录"),
            "面板查询只读；只有用户明确要求补跑时才执行，先核对账号与运行队列，避免重复提交"] : ["当前游戏：" + selectedGame],
          notes: ["该会话仅属于当前游戏/脚本，切换游戏或脚本会切换独立会话。", "可按用户明确授权安排指定账号重跑；不得自动重跑、扩大账号范围或修改定时计划。M7A 接管键鼠前先确认用户方便，并遵守 UAC 授权。"] };
      }
    }) : null;
    return h("div", { className: NS + "-root", ref: rootRef },
      // 页头
      h("header", { className: NS + "-head" },
        h("div", { className: NS + "-head-main" },
          h("h1", { className: NS + "-title" }, "游戏"),
          h("p", { className: NS + "-subtitle" },
            data.window && data.window.started === false
              ? "今日 04:00 统计窗口尚未开始 · 当前 " + fmtTime(data.window.end) +
                (generatedAt ? " · 最后查询 " + fmtTime(generatedAt) : "")
              : "查询窗口 " + fmtTime(data.window.start) + " → " + fmtTime(data.window.end) +
                (generatedAt ? " · 最后查询 " + fmtTime(generatedAt) : "")),
          h(SourceHealth, { sources: data.sources })),
        h("div", { className: NS + "-head-actions" },
          h("button", {
            className: NS + "-btn " + NS + "-btn-quiet",
            onClick: load,
            disabled: refreshing,
          }, refreshing ? "刷新中…" : "刷新"))),
      !mcpOk ? h(Empty, { title: "AUTO-MAS MCP 不可达",
        hint: ((data.sources.mcp && data.sources.mcp.error) || "查询失败") +
          "。运行状态未知；卡池和游戏助手仍可使用。" }) : null,

      // 移动端游戏列表可收起，不占满首屏
      h("button", { className: NS + "-game-toggle", "aria-expanded": gamesOpen,
        onClick: function () { setGamesOpen(!gamesOpen); } },
        (gamesOpen ? "收起游戏列表" : "选择游戏") + (selectedGame ? " · " + selectedGame : "")),
      // 左栏：脚本列表
      h("aside", { className: NS + "-side" + (gamesOpen ? " is-open" : "") },
        h("div", { className: NS + "-side-head" },
          h("h2", { className: NS + "-side-title" }, "游戏"),
          h("span", { className: NS + "-side-count" }, Object.keys(games).length)),
        Object.keys(games).length ? h("ul", { className: NS + "-scriptlist" },
          Object.keys(games).map(function (game) {
            var gameActive = selectedGame === game;
            var iconMeta = gameIconMeta(icons, game);
            return h("li", { key: game, className: NS + "-scriptitem" + (gameActive ? " " + NS + "-scriptitem-active" : ""),
              onClick: function () { setSelected(games[game].length ? games[game][0].script_name : "game:" + game); setGamesOpen(false); } },
              iconMeta ? h("span", {
                className: NS + "-game-icon",
                style: { backgroundColor: iconMeta.color || "transparent" },
                "aria-hidden": "true",
              },
                h("span", { className: NS + "-game-icon-fallback" }, String(iconMeta.name || game).slice(0, 1)),
                // 官方 512x512 商店图，本地文件；加载失败退回首字，不伪造图标
                h("img", {
                  className: NS + "-game-icon-img",
                  src: iconMeta.src, alt: "", loading: "lazy",
                  onError: function (ev) { ev.currentTarget.style.display = "none"; },
                })) : null,
              (function () {
                var configured = false, accounts = [];
                games[game].forEach(function (s) {
                  if (!s.configured) return;
                  configured = true;
                  accounts = accounts.concat(s.account_stats && s.account_stats.length
                    ? s.account_stats
                    : [{ last_status: s.success_today ? "success" : s.last_status }]);
                });
                var color = lightOf(accounts, configured);
                return color ? h("span", { className: NS + "-scriptdot", style: { background: color } }) : null;
              })(),
              h("span", { className: NS + "-scriptname" }, game), h("span", { className: NS + "-scriptnum" }, games[game].length));
          })) : null,
        gameScripts.length ? h("div", { className: NS + "-script-select" }, gameScripts.map(function (s) {
            var active = current && s.script_name === current.script_name;
            var dotColor = lightOf(s.account_stats && s.account_stats.length
              ? s.account_stats : [{ last_status: s.success_today ? "success" : s.last_status }], s.configured);
            return h("li", {
              key: s.script_name,
              className: NS + "-scriptitem" + (active ? " " + NS + "-scriptitem-active" : ""),
              onClick: function () { setSelected(s.script_name); setGamesOpen(false); },
              title: s.script_name + " · " + (s.accounts.join("、") || "无账号") +
                     " · " + s.record_count + " 条记录",
            },
              s.configured ? h("span", { className: NS + "-scriptdot", style: { background: dotColor } }) : null,
              h("span", { className: NS + "-scriptname" }, s.script_name),
              h("span", { className: NS + "-scriptnum" }, s.record_count));
          })) : null,
        scripts.length && !data.has_records ? h("p", { className: NS + "-sidehint" },
          data.window && data.window.started === false
            ? "今日 04:00 统计窗口尚未开始。"
            : "今日 04:00 至今暂无运行记录。") : null),

      // 右栏：详情
      h("section", { className: NS + "-main" },
        // 卡池即将到来：独立于账号卡片，固定在最上面
        h(UpcomingList, { items: (data.upcoming || []).filter(function (it) {
          return it.type === "banner" && it.character && gameForScript(it.game_name) === selectedGame;
        }) }),
        current ? h("div", { className: NS + "-detail" },
          h("div", { className: NS + "-detail-head" },
            h("h2", { className: NS + "-detail-title" }, selectedGame + " · " + current.script_name),
            h("div", { className: NS + "-detail-meta" },
              h(StatusPill, { status: current.success_today ? "success" : current.last_status }),
              current.accounts && current.accounts.length
                ? h("span", { className: NS + "-detail-accounts" }, current.accounts.join("、"))
                : null,
              h("span", { className: NS + "-detail-count" }, current.record_count + " 条记录"))),

          // 账号卡即运行详情：收起看灯与体力，展开看今日运行情况与上次失败。
          // （不再单独保留「今日运行情况」「上次失败详情」版块。）
          h("div", { className: NS + "-account-list" },
            (current.account_stats || []).length ? current.account_stats.map(function (a, i) {
              var mine = (current.records || []).filter(function (r) { return r.account === a.account; });
              return h(AccountCard, {
                key: current.script_name + "::" + a.account,
                account: Object.assign({}, a, {
                  records: a.records || mine.slice().reverse(),
                }),
                open: i === 0,
              });
            }) : h(AccountCard, { account: {
              account: current.source === "M7A 本地只读运行日志" ? "独立版 M7A" : "暂无账号记录",
              last_status: current.success_today ? "success" : current.last_status,
              stamina: current.stamina, records: current.records, last_error: current.last_error }, open: true }))
        ) : null,
        h("div", { className: NS + "-chat-actions" },
          h("button", { className: NS + "-btn " + NS + "-btn-quiet", onClick: function () { setChatOpen(!chatOpen); } }, chatOpen ? "收起对话" : "游戏助手" + (chatBusy ? " · 回复中" : ""))),
        chatOpen && chatNode ? h("div", { className: NS + "-chat-desktop" }, chatNode) : null,
        h("div", { className: NS + "-mobile-chat-bar" },
          h("button", { className: NS + "-btn", onClick: function () { setChatMobile(!chatMobile); } }, chatMobile ? "收起游戏助手" : "游戏助手" + (chatBusy ? " · 回复中" : ""))),
        chatMobile && chatNode ? h("div", { className: NS + "-mobile-chat-overlay" }, chatNode) : null));
  }

  window.__HERMES_PLUGINS__.register("wb-game", GamePage);
})();
