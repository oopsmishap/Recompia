// What the player does to a row's own control (type, drag, pick, Browse, Real time): turns it into a value for h.commit. Number fields
// are checked against the schema's bounds first; selects that need a question (DLSS download, joining Testing) ask it before committing.
import { api } from '../../api.js';
import { askTesting } from '../testing-notice.js';
import { chooseBuild } from '../builds.js';
import { customStart, fitNumber, readField, FIELDS } from '../../settings-schema.js';

const SLIDER_WAIT_MS = 250;
const TEXT_WAIT_MS = 600;

const OPEN = { save: () => api.openSaveDir(), game: () => api.openGameDir() };

export const fieldOf = (t) => {
  const [id, part] = (t.type === 'radio' ? t.name : t.id).split('--');
  return { f: FIELDS[id], part };
};

/** dlss: the DLSS module (pick(f, value) asks for the download before an upscaler that needs the file is saved). */
export function createControls(h, dlss) {
  const { el, st } = h;

  function commitNumber(f, t, part, final) {
    const bounds = f.custom ?? f;
    const fit = (n) => fitNumber(n.value, bounds.min, bounds.max);
    const parts = part === 'custom' ? [t] : [el.querySelector(`#${f.id}--w`), el.querySelector(`#${f.id}--h`)];
    const fitted = parts.map(fit);
    const exact = parts.every((n, i) => fitted[i] !== null && Number(n.value) === fitted[i]);
    t.setAttribute('aria-invalid', String(!exact && !final));
    if (!final && !exact) return;              // "6" on the way to "60" is not a value yet
    if (fitted.includes(null)) return h.draw(true);   // left empty: back to the saved value
    parts.forEach((n, i) => { n.value = fitted[i]; });
    h.commit(f, fitted.join('x'));
  }

  /** The value a control holds, for the types that need no help. */
  function commitControl(f, t, part, final) {
    if (part) return commitNumber(f, t, part, final);
    if (f.type === 'clock') {
      const picked = new Date(t.value);
      if (!t.value || !Number.isFinite(picked.getTime())) return h.draw(true);
      return h.commit(f, Math.round((picked.getTime() - Date.now()) / 60000));
    }
    if (f.type === 'toggle') return h.commit(f, t.checked);
    if (f.type === 'slider') return h.commit(f, Number(t.value));
    return h.commit(f, t.value);
  }

  function onSelect(f, value) {
    if (f.type === 'choice' || f.type === 'size') {
      if (value === 'custom') {
        st.customOpen.add(f.id);
        const cur = readField(f, st.src);
        return h.commit(f, f.type === 'size' ? cur : customStart(f, cur));
      }
      st.customOpen.delete(f.id);
    }
    if (f.guard === 'dlss' && ['dlss', 'dlaa'].includes(value) && !st.dlss?.present) return dlss.pick(f, value);
    // joining Testing is a question first: what it means, and that a tester is expected to share findings and logs. Cancel keeps Stable.
    if (f.guard === 'testing' && value === 'testing' && readField(f, st.src) !== 'testing') {
      return askTesting().then(async (yes) => {
        if (!yes) return h.draw(true);
        await h.commit(f, value);
        await chooseBuild('testing', true);   // the Testing builds, right away: install one now or stay and update later
      });
    }
    if (f.replaces) st.customOpen.clear();
    return h.commit(f, value);
  }

  return {
    /** A control was changed (picked, left, toggled). */
    onChange(t) {
      const { f, part } = fieldOf(t);
      if (!f) return;
      h.dropLater(f.id);
      if (t.tagName === 'SELECT') onSelect(f, t.value);
      else commitControl(f, t, part, true);
    },
    /** A text field or slider is being edited: commit after a pause. */
    onInput(t) {
      if (!t.matches('input[type="range"], input.input')) return;
      const { f, part } = fieldOf(t);
      if (!f || f.commit === 'blur') return;
      h.later(f.id, t.type === 'range' ? SLIDER_WAIT_MS : TEXT_WAIT_MS, () => commitControl(f, t, part, false));
    },
    actions: {
      async browse(arg) {
        const f = FIELDS[arg];
        const start = readField(f, st.src) || f.placeholder(h.env()) || null;
        const picked = await api.pickFolder({ title: f.pick, start });
        if (picked) {
          h.dropLater(f.id);
          const input = el.querySelector(`#${f.id}`);
          if (input) input.value = picked;
          h.commit(f, picked);
        }
      },
      async open(arg) {
        await h.flush();
        await OPEN[arg]();
      },
      'clock-real': (id) => h.commit(FIELDS[id], 0),
    },
  };
}
