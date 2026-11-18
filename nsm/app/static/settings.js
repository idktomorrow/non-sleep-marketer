// 설정 화면(C50): "폴더 고르기…" 버튼. 서버가 이 PC에 Windows 폴더 선택 창을 띄우고 고른 경로를 돌려준다.
// JS가 없으면 버튼만 동작하지 않고, 경로는 칸에 직접 넣으면 된다.
(function () {
  var btn = document.getElementById("pick-folder");
  var input = document.getElementById("backup-copy-dir");
  var msg = document.getElementById("pick-folder-msg");
  if (!btn || !input || !msg) return;
  function say(text, bad) {
    msg.textContent = text;
    msg.hidden = !text;
    msg.classList.toggle("bad", !!bad);
  }
  btn.addEventListener("click", function () {
    if (btn.disabled) return;
    var before = msg.textContent, beforeBad = msg.classList.contains("bad");
    btn.disabled = true;
    say("선택 창이 떴습니다. 보이지 않으면 작업 표시줄을 확인하세요");
    var body = new URLSearchParams();
    body.set("csrf", btn.dataset.csrf);
    body.set("initial", input.value);
    fetch("/settings/pick-folder", { method: "POST", body: body, credentials: "same-origin" })
      .then(function (r) {
        // 403: nsm run을 다시 켜 화면의 보안 토큰이 낡았다. "열 수 없음"이 아니라 새로고침을 안내
        if (r.status === 403) return { error: "화면이 오래됐습니다. 새로고침한 뒤 다시 누르세요" };
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (data.path) {
          input.value = data.path;
          // 저장 전에 떠나면 알리도록(설정 화면의 "저장하지 않고 떠나면" 검사)
          input.dispatchEvent(new Event("input", { bubbles: true }));
          say("저장을 눌러야 반영됩니다");
        } else if (data.cancelled) {
          say(before, beforeBad); // 취소: 조용히 원래대로
        } else {
          say(data.error || "폴더 선택 창을 열 수 없습니다. 경로를 직접 넣으세요", true);
        }
      })
      .catch(function () {
        say("폴더 선택 창을 열 수 없습니다. 경로를 직접 넣으세요", true);
      })
      .then(function () {
        btn.disabled = false;
      });
  });
})();
