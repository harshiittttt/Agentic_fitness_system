(function () {
  var s = window.SITE || {};
  document.querySelectorAll('[data-version]').forEach(function (el) { el.textContent = s.version || ''; });
  document.querySelectorAll('[data-download]').forEach(function (b) {
    var label = b.querySelector('span');
    if (s.apkUrl) {
      b.setAttribute('href', s.apkUrl);
      b.setAttribute('download', '');
      b.removeAttribute('aria-disabled');
      b.classList.remove('is-disabled');
      label.textContent = 'Download for Android (v' + (s.version || '') + ')';
    } else {
      b.removeAttribute('href');
      b.setAttribute('role', 'link');
      b.setAttribute('aria-disabled', 'true');
      b.setAttribute('tabindex', '-1');
      b.classList.add('is-disabled');
      label.textContent = 'Coming soon';
    }
  });
  document.querySelectorAll('[data-download-note]').forEach(function (n) {
    n.textContent = s.apkUrl
      ? 'Android APK, installed outside the Play Store. See the steps below.'
      : 'The download link is not published yet. Ask the person who invited you.';
  });
  if (s.supportEmail) {
    document.querySelectorAll('[data-support]').forEach(function (el) {
      var a = document.createElement('a');
      a.href = 'mailto:' + s.supportEmail;
      a.textContent = s.supportEmail;
      el.appendChild(document.createTextNode('Questions? '));
      el.appendChild(a);
    });
  }
})();
