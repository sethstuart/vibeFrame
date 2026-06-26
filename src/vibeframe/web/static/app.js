/* vibeFrame UI glue — tiny helpers driven by Alpine.js where useful.
   Most interactivity comes from HTMX; this file only handles:
     - multi-select state on the library page
     - lightbox open/close
     - drag-and-drop upload zone
*/

document.addEventListener('alpine:init', () => {
  /* Multi-select store for the library. Photo cards toggle into a Set. */
  Alpine.data('selection', () => ({
    mode: false,
    ids: new Set(),
    toggleMode() { this.mode = !this.mode; if (!this.mode) this.ids.clear(); },
    toggle(id) {
      if (this.ids.has(id)) this.ids.delete(id); else this.ids.add(id);
      // trigger reactivity by reassigning
      this.ids = new Set(this.ids);
    },
    has(id) { return this.ids.has(id); },
    get count() { return this.ids.size; },
    list() { return Array.from(this.ids); },
    clear() { this.ids = new Set(); },
  }));

  /* Lightbox — open/close + which image. */
  Alpine.data('lightbox', () => ({
    open: false, current: null,
    show(id, path, shownAt) { this.current = { id, path, shownAt }; this.open = true; },
    close() { this.open = false; this.current = null; },
  }));

  /* Drag-drop upload zone. Bypasses the form on drop because programmatic
     assignment to <input type="file">.files is browser-quirky on hidden
     inputs — POST a manually-built FormData instead. The browse-button
     path still uses the form's HTMX submit. */
  Alpine.data('dropzone', () => ({
    dragOver: false,
    _depth: 0,            // dragenter/leave counter so child elements don't flicker
    async _upload(files) {
      if (!files || !files.length) return;
      const host = document.getElementById('toast-host');
      const fd = new FormData();
      for (const f of files) {
        // Only forward actual files (a dragged file has a non-empty type or
        // a filename); skip dragged text/links which arrive as 0 files anyway.
        fd.append('files', f);
      }
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
      // that's dropped anywhere on the page (the #1 reason drag-drop "does
      // nothing" — the page just gets replaced by the raw image). Bind once.
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
});
