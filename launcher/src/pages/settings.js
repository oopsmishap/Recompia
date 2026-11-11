// Settings: draws settings-schema.js and saves every change by itself. The schema says what exists; the modules in settings/ know how to
// draw each row type (rows.js, draw.js), when to write (writer.js, controls.js) and what the buttons do (one module per group of buttons).
//
// This file only wires them: it builds the page state (state.js) and the handle h every module gets, { draw, flush, reload, commit, env }
// plus the page's element, ctx and state. A module keeps nothing of its own: everything it shares goes through h.st.
import './settings.css';
import { api } from '../api.js';
import { setLanguage, t } from '../i18n.js';
import { errText, esc, segmented, toast } from '../ui.js';
import { store } from '../store.js';
import { SECTIONS } from '../settings-schema.js';
import { createState, env, rememberSection } from './settings/state.js';
import { createDrawer } from './settings/draw.js';
import { createWriter } from './settings/writer.js';
import { createControls } from './settings/controls.js';
import { createDlss } from './settings/dlss.js';
import { createProfileActions } from './settings/profiles.js';
import { createSaveActions } from './settings/saves.js';
import { createSteamActions } from './settings/steam.js';
import { createRepairActions } from './settings/repair-actions.js';
import { createUpdateActions } from './settings/updates.js';
import { createGameActions } from './settings/game.js';
import { loadPromptIcons } from './settings/prompt-icons.js';

export async function render(el, ctx) {
  const [dump, cfg, dlss, overview, promptIcons] = await Promise.all([
    api.readSettings(),
    api.getConfig(),
    api.dlssStatus().catch(() => null),
    api.getOverview().catch(() => null),
    loadPromptIcons(),
  ]);
  const st = createState({ dump, cfg, dlss, overview, promptIcons }, ctx);

  el.innerHTML = `<div class="page set">
    <div class="set-head">
      <h1>${esc(t('Settings'))}</h1>
      <span class="set-saved" id="set-saved" role="status"></span>
    </div>
    <div class="set-pills">${segmented('section', SECTIONS.map((s) => ({ value: s.id, label: s.label })), st.section, { label: t('Settings section') })}</div>
    <div class="set-body" id="set-body"></div>
  </div>`;

  const drawer = createDrawer(el, st);
  const writer = createWriter(el, st, drawer.draw);
  const h = {
    el, ctx, st,
    draw: drawer.draw,
    env: () => env(st),
    saved: writer.saved,
    flush: writer.flush,
    reload: writer.reload,
    commit: writer.commit,
    later: writer.later,
    dropLater: writer.dropLater,
  };

  const dlssModule = createDlss(h);
  const controls = createControls(h, dlssModule);
  const modules = [controls, dlssModule, createProfileActions(h), createSaveActions(h), createSteamActions(h),
    createRepairActions(h), createUpdateActions(h), createGameActions(h)];
  const ACTIONS = Object.assign({}, ...modules.map((m) => m.actions));

  el.addEventListener('change', (e) => {
    const t = e.target;
    if (t.name === 'section') {
      st.section = t.value;
      rememberSection(t.value);
      h.draw(true);
      el.closest('#view')?.scrollTo(0, 0);
      return;
    }
    if (t.id === 'language') return setLanguage(t.value);   // reloads: every string is read once at start
    controls.onChange(t);
  });
  el.addEventListener('input', (e) => controls.onInput(e.target));
  el.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    try {
      await ACTIONS[b.dataset.act](b.dataset.arg, b.dataset.name);
    } catch (err) {
      toast(errText(err), 'error');
    }
  });

  let hadDlss = !!store.state.dlss;
  let wasActive = h.env().gameBusy;
  ctx.on((s, why) => {
    const active = h.env().gameBusy;
    const dlssEnded = hadDlss && !s.dlss;
    hadDlss = !!s.dlss;
    if ((wasActive && !active) || dlssEnded) h.reload().catch((e) => console.error(e));   // the game writes its settings as it leaves
    else if (why !== 'setup' || st.section === 'game') h.draw();
    wasActive = active;
  });

  h.draw(true);
  modules.forEach((m) => m.load?.());

  return () => {
    drawer.dispose();
    writer.dispose();
  };
}
