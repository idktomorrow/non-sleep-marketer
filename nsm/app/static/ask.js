// 문서 질문 탭(R3c). JS가 없어도 폼은 그대로 동작한다. 있으면 두 가지만 돕는다.
// 1) 묻기·측정 버튼을 누르면 잠그고 "찾는 중…"을 보여 두 번 눌림을 막는다.
// 2) 답 안의 [n]을 누르면 해당 근거를 펼치고 그리로 이동한다.
(function () {
  document.querySelectorAll("form[data-busy-form]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var btn = form.querySelector("button[type=submit]");
      if (!btn) return;
      if (btn.dataset.busy === "1") return;
      btn.dataset.busy = "1";
      btn.dataset.label = btn.textContent;
      // 제출이 끝난 뒤에 잠근다(먼저 잠그면 브라우저가 폼을 보내지 않는다)
      setTimeout(function () {
        btn.disabled = true;
        btn.textContent = btn.dataset.busyText || "처리 중…";
      }, 0);
    });
  });
  function openSource(n) {
    var li = document.getElementById("src-" + n);
    if (!li) return false;
    var d = li.querySelector("details");
    if (d) d.open = true;
    li.scrollIntoView({ block: "center" });
    return true;
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a.cite");
    if (!a) return;
    if (openSource(a.dataset.src)) e.preventDefault();
  });
  if (location.hash.indexOf("#src-") === 0) openSource(location.hash.slice(5));
  // 뒤로 가기로 저장된 페이지가 돌아오면 "찾는 중…" 잠금을 푼다
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-busy-form] button[type=submit]").forEach(function (btn) {
      if (btn.dataset.busy !== "1") return;
      btn.dataset.busy = "";
      btn.disabled = false;
      if (btn.dataset.label) btn.textContent = btn.dataset.label;
    });
  });
})();
