"""C61: CLI 친절한 오류, 설정 입력 검증, doctor 판정, 마법사 계정 질문, 답글 확인 문구."""

from __future__ import annotations

import socket
from datetime import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from nsm import doctor
from nsm.adapters import drivers
from nsm.adapters.platforms.aside_common import reply_probe
from nsm.cli import app
from nsm.config.schema import AppConfig, Settings
from nsm.config.secrets import MemorySecretStore
from nsm.config.store import ConfigInvalidError, load_config, parse_windows, save_config
from nsm.config.wizard import wizard
from tests.fakes import FIXTURES
from tests.unit.test_config_and_wizard import NEW_ANSWERS, ScriptedIO

runner = CliRunner()
LOCKED = "IO Error: File is already open in C:\\x\\python.exe (PID 1234)"


def _sample() -> dict:
    return yaml.safe_load((FIXTURES / "personas" / "sample.yaml").read_text("utf-8"))


# ---------------------------------------------------------------- 1. CLI 친절한 오류
@pytest.mark.parametrize(
    "args",
    [["run"], ["run", "--once", "--job", "collect"], ["index", "--stats"], ["ask", "보호 모드"]],
)
def test_missing_config_is_one_line_exit_1(nsm_home, monkeypatch, args) -> None:
    import nsm.runtime as runtime

    monkeypatch.setattr(runtime, "_setup_logging", lambda: None)
    r = runner.invoke(app, args)
    assert r.exit_code == 1
    assert "설정 파일이 없습니다" in r.output and "nsm init" in r.output
    assert "Traceback" not in r.output and not isinstance(r.exception, ValueError)


def _with_config(nsm_home: Path, app_config) -> None:
    save_config(app_config, nsm_home / "config.yaml")


def test_second_run_db_lock_is_friendly(nsm_home, monkeypatch, app_config) -> None:
    import nsm.runtime as runtime

    _with_config(nsm_home, app_config)
    monkeypatch.setattr(runtime, "_setup_logging", lambda: None)
    monkeypatch.setattr(runtime, "port_in_use", lambda port, host="127.0.0.1": False)

    def locked(path):
        raise OSError(LOCKED)  # duckdb.IOException 대신 같은 문구의 가짜 예외

    monkeypatch.setattr(runtime, "Database", locked)
    port = app_config.settings.dashboard_port
    for args in (["run"], ["run", "--once", "--job", "health"]):
        r = runner.invoke(app, args)
        assert r.exit_code == 1, r.output
        assert "이미 켜져 있는 것 같습니다" in r.output
        assert f"http://127.0.0.1:{port}" in r.output and "먼저 그 창을 끄세요" in r.output


def test_port_in_use_is_friendly_before_db_opens(nsm_home, monkeypatch, app_config) -> None:
    import nsm.runtime as runtime

    _with_config(nsm_home, app_config)
    monkeypatch.setattr(runtime, "_setup_logging", lambda: None)
    monkeypatch.setattr(runtime, "port_in_use", lambda port, host="127.0.0.1": True)
    opened: list = []
    monkeypatch.setattr(runtime, "Database", lambda path: opened.append(path))
    r = runner.invoke(app, ["run"])
    assert r.exit_code == 1 and "이미 켜져 있는 것 같습니다" in r.output
    assert opened == []  # DB를 열지 않는다


def test_other_db_errors_are_not_hidden(nsm_home, monkeypatch, app_config) -> None:
    import nsm.runtime as runtime

    _with_config(nsm_home, app_config)
    monkeypatch.setattr(runtime, "_setup_logging", lambda: None)

    def broken(path):
        raise OSError("disk full")

    monkeypatch.setattr(runtime, "Database", broken)
    r = runner.invoke(app, ["run", "--once", "--job", "health"])
    assert r.exit_code != 0 and isinstance(r.exception, OSError)


def test_port_in_use_detects_a_listener() -> None:
    with socket.socket() as s:  # 임시 포트(0 → OS가 고름). 운영 포트 8765는 건드리지 않는다
        s.bind(("127.0.0.1", 0))
        s.listen()
        from nsm.runtime import port_in_use

        assert port_in_use(s.getsockname()[1]) is True


def test_invalid_config_is_one_line(nsm_home, monkeypatch) -> None:
    import nsm.runtime as runtime

    monkeypatch.setattr(runtime, "_setup_logging", lambda: None)
    raw = _sample()
    raw["settings"] = {"timezone": "Asia/Seol"}
    nsm_home.mkdir(parents=True, exist_ok=True)
    (nsm_home / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), "utf-8")
    r = runner.invoke(app, ["run"])
    assert r.exit_code == 1 and "시간대 이름이 잘못됐습니다" in r.output


def test_job_help_lists_every_job() -> None:
    from nsm.engine.job_names import ARG_JOB_NAMES, JOB_NAMES, job_help
    from nsm.engine.runner import ARG_JOBS, JOBS

    assert tuple(JOBS) == JOB_NAMES and tuple(ARG_JOBS) == ARG_JOB_NAMES
    text = job_help()
    for name in (*JOBS, *ARG_JOBS):
        assert name in text.split(": ", 1)[1].split("|")
    assert "index" in text  # 외부 검토 1-15: 빠져 있었다


# ---------------------------------------------------------------- 3. 설정 입력 검증
def test_wrong_timezone_rejected() -> None:
    with pytest.raises(ValueError, match="시간대 이름이 잘못됐습니다"):
        Settings(timezone="Asia/Seol")
    with pytest.raises(ValueError):
        Settings(timezone="")
    assert Settings(timezone="Asia/Seoul").timezone == "Asia/Seoul"


def test_windows_are_sorted_by_start() -> None:
    w = parse_windows("18:00-22:00, 09:00-12:00")
    assert [x.start for x in w] == [time(9), time(18)]


@pytest.mark.parametrize("text", ["12:00-12:30", "23:30-23:59", "09:00-09:00", "10:00-10:59"])
def test_short_or_empty_windows_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        parse_windows(text)
    with pytest.raises(ValueError, match="60분 이상"):
        parse_windows("12:00-12:30")


def test_midnight_crossing_and_boundary_windows_pass() -> None:
    assert parse_windows("22:00-02:00")[0].end == time(2)
    assert parse_windows("23:30-00:30")[0].start == time(23, 30)  # 정확히 60분
    assert len(parse_windows("00:00-23:59")) == 1


def test_config_yaml_goes_through_same_rule(tmp_path: Path) -> None:
    raw = _sample()
    raw["persona"]["active_hours"] = [
        {"start": "18:00", "end": "22:00"},
        {"start": "09:00", "end": "12:00"},
    ]
    cfg = AppConfig.model_validate(raw)
    assert cfg.persona.active_hours[0].start == time(9)  # 읽을 때 정렬
    raw["persona"]["active_hours"] = [{"start": "12:00", "end": "12:30"}]
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), "utf-8")
    with pytest.raises(ConfigInvalidError, match="60분 이상"):
        load_config(p)


def test_existing_setting_still_reads(tmp_path: Path) -> None:
    """운영 설정과 같은 모양(00:00-23:59, Asia/Seoul, 게시 시간 08:00-23:00)은 그대로 읽힌다."""
    raw = _sample()
    raw["persona"]["active_hours"] = [{"start": "00:00:00", "end": "23:59:00"}]
    raw["settings"] = {
        "timezone": "Asia/Seoul",
        "posting_hours": [{"start": "08:00:00", "end": "23:00:00"}],
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), "utf-8")
    cfg = load_config(p)
    assert cfg.persona.active_hours[0].end == time(23, 59)
    assert cfg.settings.timezone == "Asia/Seoul"
    assert cfg.settings.posting_hours[0].start == time(8)


def test_settings_form_rejects_short_window(app_config) -> None:
    from nsm.app.settings_form import SettingsError, apply_settings_form, form_values

    form = {**form_values(app_config), "active_hours": "12:00-12:30"}
    with pytest.raises(SettingsError, match="활동 시간: 활동 시간 창은 60분 이상"):
        apply_settings_form(app_config, form)
    # 게시 시간은 60분 규칙이 없다(수집·생성 오프셋과 무관). 길이 0만 거절
    ok = apply_settings_form(
        app_config, {**form_values(app_config), "posting_hours": "12:00-12:30"}
    )
    assert ok.settings.posting_hours[0].end == time(12, 30)
    with pytest.raises(SettingsError, match="게시 시간"):
        apply_settings_form(app_config, {**form_values(app_config), "posting_hours": "12:00-12:00"})


def test_preset_with_short_window_is_rejected(tmp_path: Path, app_config) -> None:
    from nsm.config.presets import PresetError, PresetStore

    store = PresetStore(root=tmp_path / "presets", default_db=tmp_path / "d.duckdb")
    store.save(app_config, "짧은창")
    f = store.dir("짧은창") / "preset.yaml"
    raw = yaml.safe_load(f.read_text("utf-8"))
    raw["persona"]["active_hours"] = [{"start": "12:00", "end": "12:30"}]
    f.write_text(yaml.safe_dump(raw, allow_unicode=True), "utf-8")
    with pytest.raises(PresetError, match="60분 이상"):
        store.load_into(app_config, "짧은창")


# ---------------------------------------------------------------- 4. doctor 판정
class _Keys(MemorySecretStore):
    pass


def _doctor_env(
    monkeypatch, nsm_home, *, dry_run: bool | None, keys: dict, aside: bool, accounts=None
):
    import nsm.config.secrets as secrets

    store = _Keys()
    for k, v in keys.items():
        store.set(k, v)
    monkeypatch.setattr(secrets, "KeyringSecretStore", lambda: store)
    monkeypatch.setattr(drivers, "find_aside_cli", lambda: Path("C:/aside.exe") if aside else None)
    if dry_run is not None:
        raw = _sample()
        raw["settings"] = {"dry_run": dry_run, "accounts": accounts or {}}
        nsm_home.mkdir(parents=True, exist_ok=True)
        (nsm_home / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), "utf-8")
    return {c.name: c for c in doctor.run_checks()}


def test_doctor_live_without_claude_key_or_aside_fails(monkeypatch, nsm_home) -> None:
    got = _doctor_env(monkeypatch, nsm_home, dry_run=False, keys={}, aside=False)
    assert got["API 키"].status == "FAIL" and "원고" in got["API 키"].detail
    assert got["Aside CLI"].status == "FAIL"
    assert got["게시 계정"].status == "FAIL" and "accounts" in got["게시 계정"].detail


def test_doctor_dry_run_without_aside_warns(monkeypatch, nsm_home) -> None:
    got = _doctor_env(
        monkeypatch,
        nsm_home,
        dry_run=True,
        keys={"anthropic_api_key": "sk-ant-fake-0000000000"},
        aside=False,
    )
    assert (
        got["Aside CLI"].status == "WARN"
        and "드라이런이라 지금은 필요 없음" in got["Aside CLI"].detail
    )
    assert got["게시 계정"].status == "OK" and got["API 키"].status == "OK"
    assert not any(
        c.status == "FAIL" for c in got.values() if c.name not in ("Playwright Chromium",)
    )


def test_doctor_without_config_fails_config_but_warns_aside(monkeypatch, nsm_home) -> None:
    got = _doctor_env(monkeypatch, nsm_home, dry_run=None, keys={}, aside=False)
    assert got["설정 파일"].status == "FAIL" and "nsm init" in got["설정 파일"].detail
    assert got["Aside CLI"].status == "WARN"  # 설정 전에는 드라이런으로 본다(_aside() 호환)


def test_doctor_live_with_everything_ok(monkeypatch, nsm_home) -> None:
    got = _doctor_env(
        monkeypatch,
        nsm_home,
        dry_run=False,
        keys={"anthropic_api_key": "sk-ant-fake-0000000000"},
        aside=True,
        accounts={"threads": "@t", "instagram": "@i"},
    )
    assert got["Aside CLI"].status == "OK" and got["게시 계정"].status == "OK"


# ---------------------------------------------------------------- 4. 마법사 계정 질문
# NEW_ANSWERS 순서: 브랜드 6, 페르소나 7, 키 3. 드라이런 "아니오"면 그 뒤에 계정(플랫폼마다)·보호 모드
LIVE_ANSWERS = [*NEW_ANSWERS, "@luna.insta", "@luna.threads", "5"]


def test_wizard_live_asks_accounts_and_protect_count(nsm_home) -> None:
    io = ScriptedIO(LIVE_ANSWERS, [True, False, True, False])  # 동의, 광고, 켜기, 드라이런 아니오
    cfg = wizard(io, MemorySecretStore())
    assert cfg.settings.dry_run is False
    assert cfg.settings.accounts == {"instagram": "@luna.insta", "threads": "@luna.threads"}
    assert cfg.settings.protect_count == 5
    assert load_config().settings.accounts == cfg.settings.accounts  # 저장됨
    assert any("config.yaml" in t and "nsm run을 끄고" in t for t in io.out)


def test_wizard_live_reasks_bad_protect_count_and_drops_blank_account(nsm_home) -> None:
    answers = [*NEW_ANSWERS, "", "@luna.threads", "스무 건", "20"]
    io = ScriptedIO(answers, [True, False, True, False])
    cfg = wizard(io, MemorySecretStore())
    assert cfg.settings.accounts == {"threads": "@luna.threads"}
    assert cfg.settings.protect_count == 20
    assert any("0 이상의 숫자" in t for t in io.out)


def test_wizard_dry_run_does_not_ask_accounts(nsm_home) -> None:
    io = ScriptedIO(NEW_ANSWERS, [True, False, True, True])
    cfg = wizard(io, MemorySecretStore())
    assert cfg.settings.accounts == {} and not any("@my_account" in q for q in io.asked)
    assert any("config.yaml" in t for t in io.out)  # 나중에 바꾸는 법은 드라이런이어도 안내


# ---------------------------------------------------------------- 5. 답글 확인 문구
def test_reply_probe_folds_all_whitespace() -> None:
    text = "  안녕하세요\t\t고객님\u00a0\u00a0문의  주신\n\n내용 확인했어요 감사합니다"
    p = reply_probe(text)
    assert p == "안녕하세요 고객님 문의 주신 내용 확인했어요"[:20]
    assert len(p) == 20 and "  " not in p and "\t" not in p and "\u00a0" not in p


def test_reply_scripts_fold_whitespace_too() -> None:
    from nsm.adapters.platforms import instagram, threads

    assert "replace(/\\s+/g, ' ').includes(args.probe)" in threads._REPLY
    assert "replace(/\\s+/g, ' ').includes(args.probe)" in instagram._REPLY


# ---------------------------------------------------------------- C61 리뷰 반영
def _write_raw(nsm_home: Path, raw: dict, encoding: str = "utf-8") -> Path:
    nsm_home.mkdir(parents=True, exist_ok=True)
    p = nsm_home / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding=encoding)
    return p


def test_wizard_keeps_old_config_that_breaks_new_rules(nsm_home) -> None:
    """0.4.49까지 받던 짧은 창·틀린 시간대: 파일을 버리지 않고 값을 살린 채 활동 시간만 다시 묻는다."""
    raw = _sample()
    raw["persona"]["active_hours"] = [{"start": "12:00", "end": "12:30"}]
    raw["settings"] = {"timezone": "Asia/Seol", "quotas": {"instagram": 3, "threads": 2}}
    _write_raw(nsm_home, raw)
    answers = ["<default>"] * 6 + ["<default>", "09:00-12:00"] + ["<default>"] * 6 + ["", "", ""]
    io = ScriptedIO(answers, [True, False, True, True])
    cfg = wizard(io, MemorySecretStore())
    assert not (nsm_home / "config.yaml.broken").exists()
    assert cfg.brand.name == raw["brand"]["name"] and cfg.persona.name == raw["persona"]["name"]
    assert cfg.settings.quotas["instagram"] == 3  # 나머지 값 유지
    assert cfg.settings.timezone == "Asia/Seoul"  # 틀린 시간대는 기본값으로
    assert load_config().persona.active_hours[0].start == time(9)
    assert any("고칠 칸이 있습니다" in t for t in io.out)
    assert any("60분 이상" in t for t in io.out)  # 기본값(12:00-12:30)을 그대로 두면 다시 묻는다


def test_wizard_dash_clears_existing_account(nsm_home) -> None:
    wizard(ScriptedIO(LIVE_ANSWERS, [True, False, True, False]), MemorySecretStore())
    keep = ["<default>"] * 13 + ["", "", ""] + ["-", "<default>", "<default>"]
    cfg = wizard(ScriptedIO(keep, [True, False, True, False]), MemorySecretStore())
    assert cfg.settings.accounts == {"threads": "@luna.threads"}  # 엔터는 유지, "-"는 지움
    assert cfg.settings.protect_count == 5


def test_wizard_rejects_unicode_digit_protect_count(nsm_home) -> None:
    answers = [*NEW_ANSWERS, "@i", "@t", "²", "7"]
    io = ScriptedIO(answers, [True, False, True, False])
    assert wizard(io, MemorySecretStore()).settings.protect_count == 7


def test_port_in_use_only_for_address_in_use(monkeypatch) -> None:
    import errno

    import nsm.runtime as runtime

    class FakeSock:
        def __init__(self, err):
            self.err = err

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def bind(self, addr):
            raise self.err

    for err, busy in (
        (OSError(errno.EADDRINUSE, "in use"), True),
        (OSError(errno.EACCES, "denied"), False),
    ):
        monkeypatch.setattr(socket, "socket", lambda *a, e=err, **k: FakeSock(e))
        assert runtime.port_in_use(1) is busy


def test_non_utf8_config_is_one_line(nsm_home, monkeypatch) -> None:
    import nsm.runtime as runtime

    monkeypatch.setattr(runtime, "_setup_logging", lambda: None)
    _write_raw(nsm_home, _sample(), encoding="cp949")
    r = runner.invoke(app, ["run"])
    assert r.exit_code == 1 and "UTF-8로 저장해 주세요" in r.output


def test_doctor_unreadable_config_does_not_claim_dry_run(monkeypatch, nsm_home) -> None:
    import nsm.config.secrets as secrets

    raw = _sample()
    raw["settings"] = {"dry_run": False, "timezone": "Asia/Seol"}
    _write_raw(nsm_home, raw)
    monkeypatch.setattr(secrets, "KeyringSecretStore", lambda: MemorySecretStore())
    monkeypatch.setattr(drivers, "find_aside_cli", lambda: None)
    got = {c.name: c for c in doctor.run_checks()}
    assert got["설정 파일"].status == "FAIL" and "시간대" in got["설정 파일"].detail
    assert "드라이런" not in got["Aside CLI"].detail and "알 수 없음" in got["Aside CLI"].detail
    assert "드라이런" not in got["게시 계정"].detail
