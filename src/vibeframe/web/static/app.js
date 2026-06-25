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

  /* Drag-drop upload zone. Forwards files to a hidden <input type="file">. */
  Alpine.data('dropzone', () => ({
    dragOver: false,
    init() {
      const input = this.$refs.input;
      const form = this.$refs.form;
      ['dragenter', 'dragover'].forEach(e => this.$el.addEventListener(e, ev => {
        ev.preventDefault(); this.dragOver = true;
      }));
      ['dragleave', 'drop'].forEach(e => this.$el.addEventListener(e, ev => {
        ev.preventDefault(); this.dragOver = false;
      }));
      this.$el.addEventListener('drop', ev => {
        if (!ev.dataTransfer || !ev.dataTransfer.files.length) return;
        input.files = ev.dataTransfer.files;
        // dispatch a change so HTMX form sees it, then submit
        form.requestSubmit();
      });
    },
  }));
});
