// Saving. write_settings and save_config go through one queue, in order. A change is shown at once (st.src), written through the queue,
// and put back from the files if the write fails. Text and slider edits wait a moment (later) so typing is not a write per key.
import { api } from '../../api.js';
import { errText, icon, toast } from '../../ui.js';
import { store } from '../../store.js';
import { applyEdits, makeSource, splitEdits, writeField } from '../../settings-schema.js';

const SAVED_SHOW_MS = 1400;

export function createWriter(el, st, draw) {
  const pending = new Map();   // field id -> { timer, run }: debounced commits
  let queue = Promise.resolve();
  let savedTimer = null;

  function saved() {
    const tick = el.querySelector('#set-saved');
    tick.innerHTML = `${icon('check')}Saved`;
    tick.classList.add('show');
    clearTimeout(savedTimer);
    savedTimer = setTimeout(() => tick.classList.remove('show'), SAVED_SHOW_MS);
  }

  /** Whatever the files say now. Runs inside the queue, so it must not wait for it; from outside see reload. */
  async function readBack(force = false) {
    const [d, c, s, o] = await Promise.all([
      api.readSettings(),
      api.getConfig(),
      api.dlssStatus().catch(() => st.dlss),
      api.getOverview().catch(() => st.overview),
    ]);
    st.src = st.unsaved.reduce(applyEdits, makeSource(d, c));   // what is still on its way stays on screen
    st.gameDir = d.game_dir;
    st.dlss = s;
    st.overview = o;
    draw(force);
  }

  const reload = () => (queue = queue.then(() => readBack()));

  async function persist(edits, reloads) {
    const { ini, config } = splitEdits(edits);
    if (ini.length) await api.writeSettings({ edits: ini });
    if (config) await api.saveConfig({ config: { ...(await api.getConfig()), ...config } });
    if (config && 'channel' in config) await store.refreshNews(true);   // another channel, another list of releases
    if (reloads) await store.refresh();
    saved();
  }

  /** Gives field f this value: on screen at once, in the files through the queue, back as it was if that fails. */
  function commit(f, value) {
    const edits = writeField(f, value, st.src);
    if (edits.length) {
      st.src = applyEdits(st.src, edits);
      st.unsaved.push(edits);
      const done = () => { st.unsaved = st.unsaved.filter((x) => x !== edits); };
      queue = queue.then(async () => {
        try {
          await persist(edits, f.reloads);
          done();
          if (f.reloads) await readBack();
        } catch (e) {
          done();
          toast(errText(e), 'error');
          await readBack(true).catch((err) => console.error(err));
        }
      });
    }
    draw();
    return queue;
  }

  const later = (id, ms, run) => {
    clearTimeout(pending.get(id)?.timer);
    const entry = { run, timer: setTimeout(() => { pending.delete(id); run(); }, ms) };
    pending.set(id, entry);
  };
  const dropLater = (id) => {
    clearTimeout(pending.get(id)?.timer);
    pending.delete(id);
  };

  /** Everything still waiting goes out; resolves when it is on disk. */
  function flush() {
    for (const [id, p] of [...pending]) {
      dropLater(id);
      p.run();
    }
    return queue;
  }

  return {
    saved,
    reload,
    commit,
    later,
    dropLater,
    flush,
    dispose() {
      clearTimeout(savedTimer);
      flush();
    },
  };
}
