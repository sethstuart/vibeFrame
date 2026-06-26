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
    init() {
      ['dragenter', 'dragover'].forEach(e => this.$el.addEventListener(e, ev => {
        ev.preventDefault(); this.dragOver = true;
      }));
      ['dragleave'].forEach(e => this.$el.addEventListener(e, ev => {
        ev.preventDefault(); this.dragOver = false;
      }));
      this.$el.addEventListener('drop', async (ev) => {
        ev.preventDefault();
        this.dragOver = false;
        const files = ev.dataTransfer && ev.dataTransfer.files;
        if (!files || !files.length) return;
        const fd = new FormData();
        for (const f of files) fd.append('files', f);
        try {
          const r = await fetch('/images/upload', {
            method: 'POST',
            body: fd,
            headers: { 'HX-Request': 'true' },
          });
          const html = await r.text();
          const host = document.getElementById('toast-host');
          if (host) host.innerHTML = html;
        } catch (e) {
          const host = document.getElementById('toast-host');
          if (host) host.innerHTML = `<div class="toast toast-err"><strong>Upload failed</strong><span>${e}</span></div>`;
        }
      });
    },
  }));
});
