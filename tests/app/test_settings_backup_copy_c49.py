"""설정 화면 "백업 복사 폴더" 칸 (배치 C49). 검사가 실패해도 값은 저장하고 칸 아래에 안내."""

from __future__ import annotations

from pathlib import Path

import pytest

from nsm.app.ko import BACKUP_COPY_PROBLEMS
from nsm.app.settings_form import form_values
from tests.app.test_dashboard import _post, env  # noqa: F401


def _save(env, folder: str):  # noqa: F811
    v = form_values(env["holder"]["cfg"])
    v["backup_copy_dir"] = folder
    return _post(env, "/settings", **v)


@pytest.fixture
def backups(env, tmp_path):  # noqa: F811
    d = tmp_path / "backups"
    d.mkdir()
    env["state"].backups_dir = d
    return d


def test_good_folder_saves_without_notice(env, backups, tmp_path) -> None:  # noqa: F811
    dest = tmp_path / "OneDrive"
    dest.mkdir()
    r = _save(env, f'  "{dest}"  ')
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=1"
    assert env["holder"]["cfg"].settings.backup_copy_dir == str(dest)
    _post(env, "/ui-mode", mode="advanced")
    html = env["client"].get("/settings").text
    assert str(dest) in html and 'id="backup-copy-notice"' not in html
    assert not list(dest.iterdir())  # 쓰기 확인 파일은 지움


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda t: "relative\\backups", "relative"),
        (lambda t: str(t / "not-yet"), "missing"),
    ],
)
def test_problem_saves_value_and_shows_notice(env, backups, tmp_path, make, code) -> None:  # noqa: F811
    folder = make(tmp_path)
    r = _save(env, folder)
    assert r.status_code == 303 and f"copy_warn={code}" in r.headers["location"]
    assert env["holder"]["cfg"].settings.backup_copy_dir == folder  # 막지 않고 저장
    _post(env, "/ui-mode", mode="advanced")
    html = env["client"].get(r.headers["location"]).text
    assert BACKUP_COPY_PROBLEMS[code] in html and "저장했습니다" in html


def test_not_writable_notice(env, backups, tmp_path, monkeypatch) -> None:  # noqa: F811
    dest = tmp_path / "locked"
    dest.mkdir()
    real = Path.write_bytes

    def denied(self, data):
        if self.name.startswith(".nsm-write-test"):
            raise PermissionError(13, "Permission denied")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", denied)
    r = _save(env, str(dest))
    assert "copy_warn=not_writable" in r.headers["location"]
    assert env["holder"]["cfg"].settings.backup_copy_dir == str(dest)
    _post(env, "/ui-mode", mode="advanced")
    assert BACKUP_COPY_PROBLEMS["not_writable"] in env["client"].get(r.headers["location"]).text


def test_backups_folder_itself_is_flagged(env, backups) -> None:  # noqa: F811
    r = _save(env, str(backups))
    assert "copy_warn=inside_backups" in r.headers["location"]


def test_missing_folder_notice_stays_on_later_visits(env, backups, tmp_path) -> None:  # noqa: F811
    _save(env, str(tmp_path / "later"))
    _post(env, "/ui-mode", mode="advanced")
    html = env["client"].get("/settings").text
    assert BACKUP_COPY_PROBLEMS["missing"] in html
    (tmp_path / "later").mkdir()  # 폴더를 만들면 안내가 사라짐
    assert BACKUP_COPY_PROBLEMS["missing"] not in env["client"].get("/settings").text


def test_empty_turns_off_and_unknown_code_is_ignored(env, backups, tmp_path) -> None:  # noqa: F811
    _save(env, str(tmp_path / "x"))
    r = _save(env, "")
    assert r.headers["location"] == "/settings?saved=1"
    assert env["holder"]["cfg"].settings.backup_copy_dir == ""
    _post(env, "/ui-mode", mode="advanced")
    html = env["client"].get("/settings?saved=1&copy_warn=<script>").text
    assert 'id="backup-copy-notice"' not in html


def test_field_only_in_advanced_mode(env) -> None:  # noqa: F811
    _post(env, "/ui-mode", mode="simple")
    simple = env["client"].get("/settings").text
    assert '<h2 id="backup" class="adv-only" hidden>' in simple
    _post(env, "/ui-mode", mode="advanced")
    adv = env["client"].get("/settings").text
    assert '<h2 id="backup" class="adv-only" >' in adv
    assert 'name="backup_copy_dir"' in adv
    assert "구글 드라이브나 OneDrive 폴더를 고르면 클라우드에도 남습니다" in adv  # C51 쉬운 문구


def test_old_form_without_field_keeps_value(env, tmp_path) -> None:  # noqa: F811
    _save(env, str(tmp_path))
    v = form_values(env["holder"]["cfg"])
    del v["backup_copy_dir"]
    _post(env, "/settings", **v)
    assert env["holder"]["cfg"].settings.backup_copy_dir == str(tmp_path)


def test_stale_code_in_address_is_rechecked(env, backups, tmp_path) -> None:  # noqa: F811
    r = _save(env, str(tmp_path / "soon"))
    (tmp_path / "soon").mkdir()  # 안내를 보고 폴더를 만든 뒤 새로고침
    _post(env, "/ui-mode", mode="advanced")
    assert BACKUP_COPY_PROBLEMS["missing"] not in env["client"].get(r.headers["location"]).text


def test_write_probe_only_when_value_changes(env, backups, tmp_path, monkeypatch) -> None:  # noqa: F811
    dest = tmp_path / "Drive"
    dest.mkdir()
    _save(env, str(dest))
    probes: list[Path] = []
    real = Path.write_bytes

    def spy(self, data):
        if self.name.startswith(".nsm-write-test"):
            probes.append(self)
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", spy)
    _save(env, str(dest))  # 다른 칸만 저장한 것과 같음
    assert probes == []
    other = tmp_path / "Drive2"
    other.mkdir()
    _save(env, str(other))
    assert len(probes) == 1


def test_simple_mode_shows_notice_at_top(env, backups, tmp_path) -> None:  # noqa: F811
    _save(env, str(tmp_path / "gone"))
    _post(env, "/ui-mode", mode="simple")
    html = env["client"].get("/settings").text
    assert "백업 복사 폴더: " + BACKUP_COPY_PROBLEMS["missing"] in html
