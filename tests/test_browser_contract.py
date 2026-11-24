"""Headless browser contract for the shipped wb-game dashboard bundle."""

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY_DIST = ROOT / "wb-game" / "dashboard" / "dist"


@pytest.fixture
def browser_page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<div id="app"></div>')
        harness = r"""
(() => {
  let component, state = [], hook = 0, root;
  const React = { createElement(type, props, ...children) {
    return { type, props: props || {}, children: children.flat() };
  }};
  function renderNode(value) {
    if (value == null || value === false || value === true) return document.createTextNode("");
    if (Array.isArray(value)) { const f=document.createDocumentFragment(); value.forEach(v=>f.appendChild(renderNode(v))); return f; }
    if (typeof value === "string" || typeof value === "number") return document.createTextNode(String(value));
    if (typeof value.type === "function") return renderNode(value.type({...value.props, children:value.children}));
    const el=document.createElement(value.type);
    for (const [k,v] of Object.entries(value.props || {})) {
      if (k === "key" || k === "ref" || v == null) continue;
      if (k === "className") el.className=v;
      else if (k === "style") Object.assign(el.style,v);
      else if (k === "disabled") el.disabled=!!v;
      else if (k === "dangerouslySetInnerHTML") { el.innerHTML = (v && v.__html) || ""; }
      else if (/^on[A-Z]/.test(k)) el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === "src" || k === "alt" || k === "loading") el.setAttribute(k, v);
      else if (k === "title") el.title=v;
      else if (k === "type") el.type=v;
      else if (k.startsWith("aria-")) el.setAttribute(k,v);
    }
    value.children.forEach(child=>el.appendChild(renderNode(child)));
    return el;
  }
  let effectsFlushed = false;
  const pendingEffects = [];
  function flushEffects() {
    if (effectsFlushed) return;
    effectsFlushed = true;
    const fs = pendingEffects.splice(0);
    fs.forEach(f => { try { f(); } catch (e) { /* 影子环境里 effect 抛错不该中断渲染 */ } });
  }
  function rerender() { hook=0; root.replaceChildren(renderNode(component())); queueMicrotask(flushEffects); }
  const hooks = {
    useState(initial) {
      const i=hook++;
      if (!(i in state)) state[i] = i===0 ? window.__WB_GAME_DATA__ : (i===4 ? window.__WB_CHAT_API__ : initial);
      return [state[i], value=>{
        const next = typeof value==="function" ? value(state[i]) : value;
        if (Object.is(next, state[i])) return;
        state[i]=next; queueMicrotask(rerender);
      }];
    },
    useEffect(fn) { if (!effectsFlushed) pendingEffects.push(fn); },
    useMemo(fn) { hook++; return fn(); }, useCallback(fn) { hook++; return fn; }, useRef() { hook++; return {}; }
  };
  window.__HERMES_PLUGIN_SDK__={React,hooks,fetchJSON:()=>Promise.resolve(window.__WB_GAME_DATA__)};
  window.__HERMES_PLUGINS__={register(id,fn){ if(id==="wb-game") component=fn; }};
  window.__WB_CHAT__={makePanel(){ return function Chat(){return React.createElement("div",{className:"fake-chat"},"Game assistant ready");}; }};
  window.__WB_CHAT_API__=window.__WB_CHAT__;
  window.__wbRender=()=>{root=document.getElementById("app");state=[];effectsFlushed=false;rerender();};
})();
"""
        page.add_script_tag(content=harness)
        page.add_style_tag(path=str(DEPLOY_DIST / "style.css"))
        # 游戏图标模块：真实部署副本，让 loadGameIcons() 直接命中已载入的那份
        page.add_script_tag(path=str(DEPLOY_DIST / "game-icons.js"))
        page.add_script_tag(path=str(DEPLOY_DIST / "index.js"))
        yield page
        browser.close()


def fixture_data(scripts, reachable=True):
    return {
        "generated_at": "2026-11-22T10:00:00+08:00",
        "window": {"start": "2026-11-22T04:00:00+08:00", "end": "2026-11-22T10:00:00+08:00", "started": True},
        "sources": {"mcp": {"reachable": reachable, "error": "read-only fixture"},
                    "chronorail": {"reachable": True}},
        "scripts": scripts,
        "has_records": False,
        "upcoming": [{"game_id": "fiction", "game_name": "无自动化脚本游戏", "type": "banner",
                      "phase": "start", "name": "未来角色卡池", "character": "未来角色",
                      "start_date": "2026-11-25", "end_date": "2026-12-10",
                      "days_until": 3, "estimated": False}],
    }


def mount(page, data):
    page.evaluate("data => window.__WB_GAME_DATA__=data", data)
    page.evaluate("window.__wbRender()")
    # 让影子 harness 里的 effect 微任务链（载入对话/图标模块 → setState → 重渲染）跑完
    page.wait_for_timeout(150)


def test_configured_script_keeps_light_and_unconfigured_history_has_none(browser_page):
    data = fixture_data([
        {"script_name": "星穹铁道", "configured": True, "accounts": ["星穹铁道B服"],
         "account_stats": [{"account": "星穹铁道B服", "last_status": "unknown", "stamina": {"status": "unknown"}}],
         "records": [], "record_count": 0, "last_status": "unknown", "success_today": False,
         "stamina": {"status": "unknown"}},
        {"script_name": "旧历史脚本", "configured": False, "accounts": [], "account_stats": [],
         "records": [], "record_count": 0, "last_status": "unknown", "success_today": False,
         "stamina": {"status": "unknown"}},
    ])
    mount(browser_page, data)
    assert browser_page.locator(".wbg-script-select .wbg-scriptdot").count() == 1
    # One game-level light and one selected-script light; the history-only game has none.
    assert browser_page.locator(".wbg-scriptlist .wbg-scriptdot").count() == 1
    assert browser_page.locator(".wbg-scriptlist").get_by_text("无自动化脚本游戏", exact=True).count() == 1
    assert browser_page.locator(".wbg-mobile-chat-bar button").count() == 1


def test_no_scripts_and_mcp_failure_keep_banners_and_chat_without_status_placeholders(browser_page):
    mount(browser_page, fixture_data([], reachable=False))
    assert browser_page.locator(".wbg-side .wbg-scriptdot").count() == 0
    assert browser_page.locator(".wbg-script-select .wbg-scriptdot").count() == 0
    assert browser_page.get_by_text("当前没有可核实的账号记录").count() == 0
    assert browser_page.get_by_text("选择左侧脚本查看详情").count() == 0
    assert browser_page.get_by_text("今日 04:00 至今暂无运行记录。").count() == 0
    assert browser_page.locator(".wbg-scriptlist").get_by_text("无自动化脚本游戏", exact=True).count() == 1
    button = browser_page.get_by_role("button", name="游戏助手")
    assert browser_page.locator(".wbg-mobile-chat-bar button").count() == 1
    button.first.click()
    assert browser_page.locator(".fake-chat").inner_text() == "Game assistant ready"


def _script(name, statuses, configured=True):
    return {"script_name": name, "configured": configured, "accounts": [f"{name}{i}" for i in range(len(statuses))],
            "account_stats": [{"account": f"{name}{i}", "last_status": s, "success_today": s in ("success", "warning"),
                               "stamina": {"status": "unknown"}} for i, s in enumerate(statuses)],
            "records": [], "record_count": 0, "last_status": statuses[0] if statuses else "unknown",
            "success_today": bool(statuses) and all(s in ("success", "warning") for s in statuses),
            "stamina": {"status": "unknown"}}


def _dot_color(page, selector):
    return page.locator(selector).first.evaluate("el => getComputedStyle(el).backgroundColor")


def test_game_light_is_red_on_any_account_error_gray_on_pending_green_when_all_ok(browser_page):
    data = fixture_data([_script("红游戏", ["success", "error"]), _script("灰游戏", ["success", "unknown"]),
                          _script("绿游戏", ["success", "warning"])])
    mount(browser_page, data)
    colors = {name: _dot_color(browser_page, f'.wbg-scriptlist li:has-text("{name}") .wbg-scriptdot')
              for name in ("红游戏", "灰游戏", "绿游戏")}
    # 三个游戏各一盏灯：异常红、未运行灰、全成功绿
    assert set(colors.values()) == {colors["红游戏"], colors["灰游戏"], colors["绿游戏"]}
    assert len(set(colors.values())) == 3, colors
    red, gray, green = colors["红游戏"], colors["灰游戏"], colors["绿游戏"]
    def rgb(c):
        parts = c[c.index("(") + 1:c.index(")")].split(",")
        return tuple(int(float(p)) for p in parts[:3])
    assert rgb(red)[0] > rgb(red)[1] and rgb(red)[0] > rgb(red)[2]
    assert rgb(green)[1] > rgb(green)[0]
    assert max(rgb(gray)) - min(rgb(gray)) < 30


def test_each_account_has_collapsible_card_and_stamina_keeps_only_estimate(browser_page):
    script = _script("星穹铁道", ["success", "error"])
    script["account_stats"][0]["stamina"] = {"status": "known", "raw": 12, "estimated": 20, "max": 240}
    mount(browser_page, fixture_data([script]))
    cards = browser_page.locator(".wbg-account-card")
    assert cards.count() == 2
    assert cards.first.evaluate("el => el.tagName.toLowerCase()") == "details"
    # 折叠卡片：默认收起，展开后才显示账号明细
    assert cards.nth(1).evaluate("el => el.open") is False
    cards.evaluate_all("els => els.forEach(el => { el.open = true; })")
    text = browser_page.locator(".wbg-account-list").inner_text()
    assert "原始值" not in text and "推算口径" not in text
    assert "20" in text  # 只保留推算值


def test_collapsed_account_card_shows_light_and_stamina_expanded_shows_runs_and_failure(browser_page):
    script = _script("明日方舟", ["error"])
    account = script["account_stats"][0]
    account["stamina"] = {"status": "known", "raw": 6, "estimated": 10, "max": 240}
    account["records"] = [{"time": "2026-11-22T09:00:00+08:00", "status": "success",
                           "message": "MAA 日常执行成功"}]
    account["last_error"] = {"time": "2026-11-22T09:30:00+08:00", "message": "MAA 部分任务执行失败",
                             "log_excerpt": ["[ERR] 任务出错: 刷理智"]}
    mount(browser_page, fixture_data([script]))
    card = browser_page.locator(".wbg-account-card").first

    # 收起时状态灯和体力推算值都在 summary 里
    assert card.evaluate("el => el.open") is False
    summary = card.locator("summary").first
    assert summary.locator(".wbg-pill").count() == 1
    assert summary.locator(".wbg-stamina-inline").count() == 1
    assert summary.locator(".wbg-stamina-inline").inner_text().strip().startswith("体力")
    assert summary.inner_text().count("今日运行情况") == 0

    # 展开后是今日运行情况 + 上次失败详情，且不再有独立版块
    card.evaluate("el => el.open = true")
    body = card.locator(".wbg-acc-body")
    assert "今日运行情况" in body.inner_text()
    assert "上次失败" in body.inner_text()
    assert browser_page.locator(".wbg-err-card").count() == 0
    assert browser_page.locator(".wbg-timeline-card").count() == 0
    assert browser_page.get_by_text("上次失败详情", exact=False).count() == 0


def test_upcoming_card_content_uses_larger_font():
    css = (ROOT / "wb-game" / "dashboard" / "dist" / "style.css").read_text(encoding="utf-8")
    for selector, size in ((".wbg-up-game", "15px"), (".wbg-up-name", "15px"),
                           (".wbg-up-days", "16px")):
        rule = css.split(selector + " {", 1)[1].split("}", 1)[0]
        assert "font-size: " + size in rule, (selector, rule)


def test_upcoming_banners_sit_above_account_cards(browser_page):
    script = _script("无自动化脚本游戏", [])
    script["configured"] = False
    mount(browser_page, fixture_data([script]))
    order = browser_page.evaluate(
        """() => {
            const main = document.querySelector('.wbg-main');
            const nodes = [...main.querySelectorAll('.wbg-upcoming-card, .wbg-account-list')];
            return nodes.map(n => n.className).join('|');
        }"""
    )
    assert "wbg-upcoming-card" in order.split("|")[0], order


def test_deployed_mobile_navigation_chat_and_content_at_three_widths(browser_page):
    data = fixture_data([_script("星穹铁道", ["success"])])
    mount(browser_page, data)
    for width in (390, 720, 1440):
        browser_page.set_viewport_size({"width": width, "height": 900})
        if width <= 720:
            toggle = browser_page.locator(".wbg-game-toggle")
            assert toggle.is_visible(), f"toggle hidden at {width}px"
            side = browser_page.locator(".wbg-side")
            assert not side.is_visible(), f"navigation should start collapsed at {width}px"
            toggle.click()
            assert side.is_visible(), f"navigation did not open at {width}px"
            assert browser_page.locator(".wbg-detail").is_visible(), "正文应保持可见"
            toggle.click()
            assert not side.is_visible(), f"navigation did not collapse at {width}px"
            entry = browser_page.locator(".wbg-mobile-chat-bar")
            assert entry.is_visible()
            box = entry.bounding_box()
            assert box and box["y"] + box["height"] >= 800, f"chat entry not bottom anchored: {box}"
            entry.locator("button").click()
            panel = browser_page.locator(".wbg-mobile-chat-overlay")
            assert panel.is_visible()
            assert panel.bounding_box()["y"] > 0, "chat sheet must not cover the entire screen"
            browser_page.locator(".wbg-mobile-chat-bar button").click()
            browser_page.screenshot(path=str(ROOT / ".scratch" / f"wb-game-{width}.png"), full_page=True)
        else:
            assert browser_page.locator(".wbg-side").is_visible()
            assert browser_page.locator(".wbg-detail").is_visible()
            browser_page.screenshot(path=str(ROOT / ".scratch" / f"wb-game-{width}.png"), full_page=True)


def test_game_list_renders_official_icon_images(browser_page):
    data = fixture_data([])
    data["scripts"] = [_script("原神", ["success"]), _script("绝区零", ["success"])]
    mount(browser_page, data)

    icons = browser_page.locator(".wbg-scriptlist .wbg-scriptitem .wbg-game-icon")
    assert icons.count() == 2, "图标来自本地清单，不该只依赖后端 catalog 覆盖的那几个游戏"

    first = icons.first
    img = first.locator("img.wbg-game-icon-img")
    assert img.count() == 1, "图标必须是官方图片，不是内联 SVG"
    assert img.get_attribute("src") == "/dashboard-plugins/wb-game/dist/game-icons/genshin.jpg"
    assert img.get_attribute("alt") == ""
    assert first.locator("svg").count() == 0, "不该再渲染手绘 SVG"
    assert first.evaluate("el => el.getAttribute('aria-hidden')") == "true"
    assert first.evaluate("el => el.style.backgroundColor") == "rgb(74, 144, 217)"
    # 首字占位在图片下面，图片加载失败时才露出来
    assert first.locator(".wbg-game-icon-fallback").inner_text() == "原"

    # 后端 catalog 不可达时也必须有图标 —— 这正是「有的根本没有」的根因
    data["sources"]["chronorail"].update({"catalog_reachable": False, "games": []})
    mount(browser_page, data)
    assert browser_page.locator(".wbg-scriptlist .wbg-game-icon").count() == 2
    # 脚本列表本身照常（2 个脚本 + 1 个无自动化脚本分组）
    assert browser_page.locator(".wbg-scriptlist .wbg-scriptitem").count() == 3
