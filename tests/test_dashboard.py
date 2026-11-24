import datetime as dt
import importlib.util
import pathlib
import json
import shutil
import tempfile
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("wb_game_api", ROOT / "wb-game" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


@pytest.fixture
def local_tmp_path():
    path = pathlib.Path(tempfile.mkdtemp(prefix=".wb-game-test-", dir=ROOT / "tests"))
    try:
        yield path
    finally:
        shutil.rmtree(path)


def test_stamina_parser_accepts_realistic_punctuation_and_spaces():
    now = dt.datetime(2026, 11, 22, 12, tzinfo=api.CST)
    boundary = dt.datetime(2026, 11, 22, 4, tzinfo=api.CST)
    record = dt.datetime(2026, 11, 22, 11, tzinfo=api.CST)
    cases = [
        ("原神", "原粹树脂: 179, 浓缩树脂: 0", 179, 200),
        ("明日方舟", "理智: 120 / 210", 120, 210),
        ("三月七小助手", "开拓力: 40 / 300", 40, 300),
        ("星穹铁道模拟器", "开拓力：40/300", 40, 300),
    ]
    for script, line, value, cap in cases:
        out = api._parse_stamina("prefix\n" + line, record, script, now, boundary)
        assert out["status"] == "known"
        assert out["raw"] == value
        assert out["max"] == cap


def test_last_measurement_wins_and_unknown_is_truthful():
    now = dt.datetime(2026, 11, 22, 12, tzinfo=api.CST)
    boundary = dt.datetime(2026, 11, 22, 4, tzinfo=api.CST)
    record = dt.datetime(2026, 11, 22, 11, tzinfo=api.CST)
    out = api._parse_stamina("开拓力：65/300\n开拓力：40/300", record,
                             "星穹铁道模拟器", now, boundary)
    assert out["raw"] == 40
    assert out["estimated"] == 50
    assert api._parse_stamina("no reading", record, "鸣潮", now, boundary)["status"] == "unknown"


def test_local_day_boundary_and_offset_are_respected():
    history = {"data": {"bucket": {"account": {"index": [
        {"id": "before", "time": "2026-11-21T23:59:00+04:00"},
        {"id": "at", "time": "2026-11-22T00:00:00+04:00"},
    ]}}}}
    start = dt.datetime(2026, 11, 22, 4, tzinfo=api.CST)
    end = dt.datetime(2026, 11, 22, 5, tzinfo=api.CST)
    assert [r["id"] for r in api._flatten_window(history, start, end)] == ["at"]


def test_result_status_keeps_warning_running_unknown_distinct():
    assert api._effective_status({"status": "running"}) == "running"
    assert api._effective_status({"status": "error"}) == "error"
    assert api._effective_status({"status": "success", "script_name": "原神",
                                  "message": "每日奖励未领取"}) == "warning"
    assert api._effective_status({"status": "mystery"}) == "unknown"


def test_auto_mas_config_builds_two_hsr_script_rosters_without_counting_metadata(local_tmp_path, monkeypatch):
    # Mirrors the observed ScriptConfig instances + SubConfigsInfo.UserData shape.
    hsr_id, src_id = "hsr-instance", "src-instance"
    config = {
        "instances": [{"uid": hsr_id, "type": "HSRConfig"},
                     {"uid": src_id, "type": "SrcConfig"},
                     {"uid": "general", "type": "GeneralConfig"}],
        hsr_id: {"SubConfigsInfo": {"UserData": {
            "account-a": {"Info": {"Name": "星穹铁道官服189", "Status": True},
                          "Game": {"Account": "secret-account", "Password": "secret"}},
            "account-disabled": {"Info": {"Name": "disabled", "Status": False}},
        }}},
        src_id: {"SubConfigsInfo": {"UserData": {
            "account-b": {"Info": {"Name": "崩铁模拟器测试账号", "Status": True}},
        }}},
        "general": {"SubConfigsInfo": {"UserData": {
            "unlinked": {"Info": {"Name": "no-account-metadata", "Status": True}},
        }}},
    }
    path = local_tmp_path / "ScriptConfig.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(api, "AUTO_MAS_SCRIPT_CONFIG", path)
    roster = api._configured_auto_mas_rosters()
    assert roster == {"星穹铁道": ["星穹铁道官服189"],
                      "星穹铁道模拟器": ["崩铁模拟器测试账号"]}
    assert "secret" not in json.dumps(roster)


def test_same_minute_success_wins_over_error_retry():
    failure = {"id": "e", "time_cst": "2026-11-22T09:10:02+08:00", "status": "error"}
    success = {"id": "s", "time_cst": "2026-11-22T09:10:58+08:00", "status": "success"}
    assert api._same_minute_success([failure, success]) == success
    assert api._same_minute_success([success, failure]) == success


def test_script_aggregate_does_not_hide_failed_or_unverified_accounts():
    status, success_today = api._aggregate_account_outcomes([
        {"last_status": "success", "success_today": True},
        {"last_status": "error", "success_today": False},
    ])
    assert (status, success_today) == ("error", False)
    assert api._aggregate_account_outcomes([
        {"last_status": "success", "success_today": True},
        {"last_status": "unknown", "success_today": False},
    ]) == ("unknown", False)


def test_local_log_timestamp_uses_local_timezone_when_requested():
    record = dt.datetime(2026, 11, 22, 9, tzinfo=api.CST)
    parsed, _ = api._parse_log_timestamp("2026-11-22 08:48:38.470", record, api.CST)
    assert parsed == dt.datetime(2026, 11, 22, 8, 48, 38, tzinfo=api.CST)


def test_maa_switchtheme_only_exception_and_genshin_unclaimed_warning():
    record = {"status": "error", "script_name": "明日方舟"}
    log = (ROOT / "tests" / "fixtures" / "maa_switchtheme_only_real_format.log").read_text(encoding="utf-8")
    assert api._effective_status(record, log) == "success"
    assert api._effective_status(record, log + '\n[TaskChainError] {"taskchain":"Fight"}') == "error"
    # AUTO-MAS 记录的真实界面日志（中文「任务出错: 更换主题」）也必须豁免
    ui_log = (ROOT / "tests" / "fixtures" / "maa_automas_switchtheme_ui_log.log").read_text(encoding="utf-8")
    assert api._effective_status(record, ui_log) == "success"
    assert api._effective_status(record, ui_log + "\n[ERR] 任务出错: 刷理智") == "error"
    genshin_log = (ROOT / "tests" / "fixtures" / "genshin_daily_reward_real_format.log").read_text(encoding="utf-8")
    assert api._effective_status({"status": "success", "script_name": "原神"}, genshin_log) == "warning"


def test_native_zzz_log_fixture_drives_status_and_stamina(local_tmp_path, monkeypatch):
    root = local_tmp_path / "ZZZ-Onedragon"
    log_dir = root / ".log"
    log_dir.mkdir(parents=True)
    fixture = ROOT / "tests" / "fixtures" / "zzz_onedragon_real_format.log"
    (log_dir / "log.txt").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(api, "ZZZ_ONEDRAGON_ROOT", root)
    now = dt.datetime(2026, 11, 22, 9, tzinfo=api.CST)
    boundary = dt.datetime(2026, 11, 22, 4, tzinfo=api.CST)
    out = api._read_zzz_onedragon(now, boundary)
    assert out["source"] == "ZZZ-Onedragon 本地原生日志"
    assert out["last_status"] == "success"
    assert "执行成功 返回状态 全部结束" in out["records"][0]["log_excerpt"][0]
    assert all("SMTP" not in line for line in out["records"][0]["log_excerpt"])
    assert out["stamina"]["status"] == "known"
    assert out["stamina"]["raw"] == 20
    assert out["account_stats"] == []  # a profile ID is not an account identity
    assert out["accounts"] == []


def test_configured_zzz_without_native_run_is_unknown_without_placeholder_account(local_tmp_path, monkeypatch):
    root = local_tmp_path / "ZZZ-Onedragon"
    profile = root / "config" / "01"
    profile.mkdir(parents=True)
    (profile / "game_account.yml").write_text("account: opaque-account-key\n", encoding="utf-8")
    monkeypatch.setattr(api, "ZZZ_ONEDRAGON_ROOT", root)
    monkeypatch.setattr(api, "AUTO_MAS_SCRIPT_CONFIG", local_tmp_path / "missing.json")
    now = dt.datetime(2026, 11, 22, 9, tzinfo=api.CST)
    boundary = dt.datetime(2026, 11, 22, 4, tzinfo=api.CST)
    out = api._read_zzz_onedragon(now, boundary)
    assert out["configured"] is True
    assert out["last_status"] == "unknown"
    assert out["accounts"] == []


def test_account_names_and_error_excerpts_redact_full_phone_numbers():
    assert "13800138000" not in api._safe_account_name("account-13800138000")
    excerpt = api._error_excerpt("错误: phone=13800138000\npassword: private-value")
    assert all("13800138000" not in line and "private-value" not in line for line in excerpt)


def test_account_card_records_and_last_error_prefer_today_then_history():
    ui_log = (ROOT / "tests" / "fixtures" / "maa_automas_switchtheme_ui_log.log").read_text(encoding="utf-8")
    today = [{"id": "a1", "time_cst": "2026-11-22T09:00:00+08:00", "username": "acc-1",
              "script_name": "明日方舟", "status": "error", "message": "MAA 部分任务执行失败"}]
    # 仅更换主题失败 -> 该条在今日运行情况里记为成功，不再产生「上次失败」
    recs = api._account_records(today, lambda r: api._effective_status(r, ui_log))
    assert recs[0]["status"] == "success"
    assert api._account_last_error(today, [], lambda r: api._effective_status(r, ui_log),
                                   lambda r: ui_log) is None

    real_fail = [{"id": "a2", "time_cst": "2026-11-22T10:30:00+08:00", "username": "acc-1",
                  "script_name": "明日方舟", "status": "error", "message": "MAA 部分任务执行失败"}]
    detail = api._account_last_error(real_fail, [], lambda r: "error",
                                     lambda r: "[ERR] TaskChainError SwitchTheme")
    assert detail["time"] == "2026-11-22T10:30:00+08:00"
    assert detail["log_excerpt"] == ["[ERR] TaskChainError SwitchTheme"]

    # 今日无失败时回退到近 30 天最近一次失败
    history = [{"id": "h1", "time_cst": "2026-11-20T09:00:00+08:00", "username": "acc-1",
                "script_name": "明日方舟", "status": "error", "message": "启动失败"}]
    old = api._account_last_error([], history, lambda r: "error", lambda r: "")
    assert old["time"] == "2026-11-20T09:00:00+08:00"
    # 回退同样走豁免：仅更换主题失败的历史记录不算失败
    theme_only = [{"id": "h2", "time_cst": "2026-11-21T09:00:00+08:00", "username": "acc-1",
                   "script_name": "明日方舟", "status": "error", "message": "MAA 部分任务执行失败"}]
    assert api._account_last_error([], theme_only,
                                   lambda r: api._effective_status(r, ui_log), lambda r: ui_log) is None


def test_m7a_metadata_only_runs_do_not_invent_account_identity(local_tmp_path, monkeypatch):
    local = local_tmp_path / "local"
    app = local / "March7thAssistant"
    root = local_tmp_path / "m7a"
    runs = root / "logs" / "runs"
    runs.mkdir(parents=True)
    app.mkdir(parents=True)
    (app / "installs.json").write_text(json.dumps({"installs": [{"path": str(root)}]}), encoding="utf-8")
    run = {"status": "success", "exit_code": 0,
           "started_at": "2026-11-22T09:00:00+08:00", "ended_at": "2026-11-22T09:01:00+08:00",
           "task": "routine"}
    (runs / "20261122-test.json").write_text(json.dumps(run), encoding="utf-8")
    (runs / "20261122-test.log").write_text("routine completed\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    now = dt.datetime(2026, 11, 22, 10, tzinfo=api.CST)
    boundary = dt.datetime(2026, 11, 22, 4, tzinfo=api.CST)
    out = api._read_m7a_instance(now, boundary)
    assert out["success_today"] is True
    assert out["accounts"] == []
    assert out["account_stats"] == []


def test_runtime_snapshot_without_account_identity_is_not_an_account_result():
    class Client:
        def call(self, name, args):
            assert name == "get_task_runtime_snapshot_api_dispatch_runtime_snapshot_get"
            return {"tasks": [{"taskId": "opaque", "scriptId": "script",
                               "scripts": [{"scriptId": "script-a", "scriptType": "HSRConfig"}]}]}
    state = api._snapshot_running_accounts(Client())
    assert state == {"reachable": True, "pairs": set(), "active_tasks": 1}


def test_chronorail_catalog_exposes_icon_and_color_and_degrades_truthfully(local_tmp_path, monkeypatch):
    path = local_tmp_path / "chronorail.json"
    path.write_text(json.dumps({"games": [{"id": "genshin", "name": "原神",
        "icon": "genshin", "color": "#4a90d9"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(api, "CHRONORAIL_CATALOG_PATH", path)
    assert api._load_chronorail_catalog() == [{"id": "genshin", "name": "原神",
        "icon": "genshin", "color": "#4a90d9"}]
    monkeypatch.setattr(api, "CHRONORAIL_CATALOG_PATH", local_tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError):
        api._load_chronorail_catalog()


def test_unconfigured_data_sources_report_clear_errors(monkeypatch):
    """仓库里不写死本机路径；缺配置时必须报「未配置」，不能猜路径或静默返回空。"""
    monkeypatch.setattr(api, "CHRONORAIL_PATH", None)
    monkeypatch.setattr(api, "CHRONORAIL_CATALOG_PATH", None)
    with pytest.raises(FileNotFoundError, match="未配置"):
        api._load_chronorail()
    with pytest.raises(FileNotFoundError, match="未配置"):
        api._load_chronorail_catalog()


def test_config_file_and_env_override_paths(tmp_path, monkeypatch):
    """环境变量优先于 config.json；两者都没有则为 None。"""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"chronorail_path": "from-config.json"}', encoding="utf-8")
    monkeypatch.setattr(api, "_CONFIG", {"chronorail_path": "from-config.json"})
    monkeypatch.delenv("WB_GAME_CHRONORAIL", raising=False)
    assert api._path_setting("WB_GAME_CHRONORAIL", "chronorail_path") == pathlib.Path("from-config.json")
    monkeypatch.setenv("WB_GAME_CHRONORAIL", "from-env.json")
    assert api._path_setting("WB_GAME_CHRONORAIL", "chronorail_path") == pathlib.Path("from-env.json")
    monkeypatch.delenv("WB_GAME_CHRONORAIL")
    assert api._path_setting("WB_GAME_NO_SUCH_ENV", "no_such_key") is None
