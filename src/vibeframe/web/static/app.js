/* vibeFrame UI glue — Alpine components registered here.

   IMPORTANT: this file MUST be loaded BEFORE the Alpine CDN script. The
   CDN build calls Alpine.start() — which dispatches `alpine:init`
   synchronously — the instant it executes. If we registered our
   components only inside an `alpine:init` listener added after Alpine
   loaded, that listener would never fire and `x-data="dropzone"` would
   throw "dropzone is not defined". So we register defensively: if Alpine
   is already on the page we register immediately, otherwise we queue on
   `alpine:init` (which works because this script runs first). */

function vfRegisterAlpine(Alpine) {
  /* Drag-drop upload zone. On drop we bypass the form and POST a
     manually-built FormData — programmatic assignment to a hidden
     <input type="file">.files is unreliable across browsers. The
     browse-button path still uses the form's HTMX submit. */
  Alpine.data('dropzone', () => ({
    dragOver: false,
    _depth: 0,            // dragenter/leave counter so child elements don't flicker
    async _upload(files) {
      if (!files || !files.length) return;
      const host = document.getElementById('toast-host');
      const fd = new FormData();
      for (const f of files) fd.append('files', f);
      try {
        const r = await fetch('/images/upload', {
          method: 'POST',
          body: fd,
          headers: { 'HX-Request': 'true' },
        });
        const html = await r.text();
        if (host) host.innerHTML = html;
      } catch (e) {
        if (host) {
          host.innerHTML =
            `<div class="toast toast-err"><strong>Upload failed</strong><span>${e}</span></div>`;
        }
      }
    },
    init() {
      // Global guard: stop the browser from navigating to / opening a file
      // dropped anywhere on the page (the classic "drag-drop does nothing —
      // the page just turns into the raw image"). Bind once.
      if (!window.__vfDropGuard) {
        window.__vfDropGuard = true;
        window.addEventListener('dragover', (ev) => ev.preventDefault());
        window.addEventListener('drop', (ev) => ev.preventDefault());
      }

      const zone = this.$el;
      zone.addEventListener('dragenter', (ev) => {
        ev.preventDefault();
        this._depth++;
        this.dragOver = true;
      });
      zone.addEventListener('dragover', (ev) => {
        ev.preventDefault();
        if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'copy';
      });
      zone.addEventListener('dragleave', (ev) => {
        ev.preventDefault();
        this._depth = Math.max(0, this._depth - 1);
        if (this._depth === 0) this.dragOver = false;
      });
      zone.addEventListener('drop', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        this._depth = 0;
        this.dragOver = false;
        const files = ev.dataTransfer && ev.dataTransfer.files;
        this._upload(files);
      });
    },
  }));
}

if (window.Alpine) {
  // Alpine already present (script loaded out of order) — register now.
  vfRegisterAlpine(window.Alpine);
} else {
  // Normal path: this script ran before Alpine, so the listener is in
  // place when Alpine dispatches alpine:init during start().
  document.addEventListener('alpine:init', () => vfRegisterAlpine(window.Alpine));
}
