/* rne dashboard. Plain JS; every page still works (more slowly) without it. */
(function () {
  'use strict';

  var POLL_MS = 15000;  // the worker saves progress every 20s, its heartbeat every 10s

  function localTimes(root) {
    root.querySelectorAll('.js-utc-time').forEach(function (el) {
      var utc = el.dataset.utc;
      if (!utc) return;
      var date = new Date(utc.replace(' ', 'T') + 'Z');
      if (isNaN(date)) return;
      el.textContent = date.toLocaleString(undefined, {
        month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit'
      });
      el.title = date.toLocaleString();
    });
  }

  function toast(text) {
    var el = document.createElement('p');
    el.className = 'flash flash-error toast';
    el.setAttribute('role', 'alert');
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }

  // Any form with data-confirm asks first (cancel, re-encode).
  document.addEventListener('submit', function (e) {
    var msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);

  // ── Index: update in place, drag to reorder, quick actions ────────────────

  function live(root) {
    var src = root.dataset.src;
    var last = root.innerHTML;
    var dragging = false;
    var busy = false;

    function refresh() {
      if (dragging || busy || document.hidden) return Promise.resolve();
      busy = true;
      return fetch(src, { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) {
          if (html !== null && html !== last && !dragging) {
            root.innerHTML = html;
            last = html;
            setup();
          }
        })
        .catch(function () { /* offline for a moment; try again next tick */ })
        .finally(function () { busy = false; });
    }

    function post(url, body) {
      return fetch(url, {
        method: 'POST', body: body, headers: { 'X-Rne-Async': '1' }
      }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
      });
    }

    function setup() {
      localTimes(root);
      var list = root.querySelector('#queue');
      if (list && window.Sortable) {
        window.Sortable.create(list, {
          handle: '.handle',
          animation: 150,
          delay: 0,
          ghostClass: 'drag-ghost',
          chosenClass: 'drag-chosen',
          onStart: function () { dragging = true; },
          onEnd: function (evt) {
            if (evt.oldIndex === evt.newIndex) { dragging = false; return; }
            var body = new URLSearchParams();
            list.querySelectorAll('li[data-id]').forEach(function (li) {
              body.append('ids', li.dataset.id);
            });
            post('/queue/order', body)
              .catch(function () { toast("Couldn't save the new order. Reloading the queue."); })
              .finally(function () { dragging = false; last = ''; refresh(); });
          }
        });
      }
    }

    // Buttons in the live area post in the background, then redraw.
    root.addEventListener('submit', function (e) {
      var form = e.target;
      if (!form.classList.contains('js-async') || e.defaultPrevented) return;
      e.preventDefault();
      var button = form.querySelector('button');
      if (button) button.setAttribute('aria-busy', 'true');
      post(form.action, new FormData(form))
        .catch(function () { toast('That didn’t work. Check the worker and try again.'); })
        .finally(function () { last = ''; refresh(); });
    });

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) refresh();
    });
    setInterval(refresh, POLL_MS);
    setup();
  }

  // ── Edit form ──────────────────────────────────────────────────────────────

  function editForm(form) {
    if (!form) return;
    var preview = document.getElementById('preview');
    var note = document.getElementById('save-note');
    var timer = null;
    var seq = 0;

    function updatePreview() {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var mine = ++seq;
        fetch(form.action, {
          method: 'POST', body: new FormData(form), headers: { 'X-Rne-Preview': '1' }
        })
          .then(function (r) { return r.ok ? r.text() : null; })
          .then(function (html) {
            if (html === null || mine !== seq) return;
            preview.innerHTML = html;
            var box = preview.querySelector('.preview');
            var n = box ? Number(box.dataset.errors || 0) : 0;
            note.textContent = n ? n + ' problem(s) above' : '';
          })
          .catch(function () {});
      }, 350);
    }

    function rowOf(el) { return el.closest('.track-row'); }

    form.addEventListener('click', function (e) {
      var btn = e.target.closest('button');
      if (!btn) return;
      var row = rowOf(btn);
      if (btn.classList.contains('move-up') && row && row.previousElementSibling) {
        row.parentNode.insertBefore(row, row.previousElementSibling);
        btn.focus();
        updatePreview();
      } else if (btn.classList.contains('move-down') && row && row.nextElementSibling) {
        row.parentNode.insertBefore(row.nextElementSibling, row);
        btn.focus();
        updatePreview();
      }
    });

    form.addEventListener('change', function (e) {
      var el = e.target;
      var row = rowOf(el);
      if (el.name === 'audio_include' || el.name === 'sub_include') {
        row.classList.toggle('excluded', !el.checked);
        if (el.name === 'sub_include' && !el.checked) {
          var radio = row.querySelector('input[name=sub_default]');
          if (radio && radio.checked) {
            var none = form.querySelector('input[name=sub_default][value=""]');
            if (none) none.checked = true;
          }
        }
      } else if (el.name === 'sub_default' && el.value && row) {
        var box = row.querySelector('input[name=sub_include]');
        box.checked = true;
        row.classList.remove('excluded');
      } else if (el.classList.contains('codec')) {
        row.querySelector('.bitrate').hidden = el.value === 'copy';
      } else if (el.id === 'move') {
        document.getElementById('library-row').hidden = !el.checked;
      }
      updatePreview();
    });
    form.addEventListener('input', function (e) {
      if (e.target.tagName === 'INPUT' && e.target.type !== 'checkbox') updatePreview();
    });

    // TVDB lookup: results are server-rendered buttons; picking one fills the fields.
    var find = document.getElementById('tvdb-find');
    var results = document.getElementById('tvdb-results');
    var tvdbNote = document.getElementById('tvdb-note');
    var title = form.elements.title;

    function search() {
      var q = title.value.trim();
      if (!q) { title.focus(); return; }
      find.setAttribute('aria-busy', 'true');
      fetch('/tvdb/search?kind=' + find.dataset.kind + '&q=' + encodeURIComponent(q))
        .then(function (r) { return r.text(); })
        .then(function (html) { results.innerHTML = html; })
        .catch(function () { results.innerHTML = '<p class="flash flash-warn">TVDB search failed.</p>'; })
        .finally(function () { find.removeAttribute('aria-busy'); });
    }
    if (find) {
      find.addEventListener('click', search);
      title.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); search(); }
      });
      results.addEventListener('click', function (e) {
        var pick = e.target.closest('.tvdb-pick');
        if (pick) {
          title.value = pick.dataset.name;
          form.elements.year.value = pick.dataset.year;
          form.elements.tvdb_id.value = pick.dataset.id;
          form.elements.tvdb_title.value = pick.dataset.name;
          tvdbNote.textContent = '✓ TVDB #' + pick.dataset.id;
          results.innerHTML = '';
          updatePreview();
        } else if (e.target.closest('.tvdb-close')) {
          results.innerHTML = '';
        }
      });
    }
  }

  localTimes(document);
  window.rne = { live: live, editForm: editForm };
})();
