# vizjs/panel.py — rung 15/17 of the template join (issues
# #86 phase-3 / #299 A): search panel + info card. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_PANEL = r"""const searchEl = document.getElementById("search");
const depthEl = document.getElementById("depth");
const cbFnEl = document.getElementById("cbFn");
fnMode = cbFnEl.checked;   // checkbox is the truth; sync the flag at boot
const cbSpinEl = document.getElementById("cbSpin");
cbSpinEl.addEventListener("change", () => { spinEnabled = cbSpinEl.checked; });
const bDeadEl = document.getElementById("bDead");
// issue #33: typing NEVER starts focus or the fn tier — it highlights the
// matches in place (spheres + labels) and fills the results list. Focus
// (compaction, budget fan, fn satellites) begins only at a node click or
// a results-row click, both of which run the same focus() path.
searchEl.oninput = e => {
  query = e.target.value.toLowerCase();
  applyHighlight();
};
searchEl.onblur = () => setTimeout(hideSearchResults, 120);
searchEl.onfocus = () => { if (query) buildSearchResults(); };
depthEl.oninput = e => { depth = +e.target.value; document.getElementById("depthVal").textContent = depth; applyVisibility(); reframeOnDepth(); };   // [#62] crumb refreshes in applyVisibility; camera tracks the lit set
document.getElementById("spread").oninput = e => {
  const v = +e.target.value / 100;
  document.getElementById("spreadVal").textContent = v.toFixed(1);
  applySpread(v);
};
document.querySelectorAll("#dirRow .seg").forEach(b => {
  b.onclick = () => {
    dirMode = +b.dataset.d;
    document.querySelectorAll("#dirRow .seg").forEach(x =>
      x.classList.toggle("on", x === b));
    applyVisibility();
  };
});
document.getElementById("cbFn").onchange = e => { fnMode = e.target.checked; applyVisibility(); };
// cluster supernode collapse: toggle owns collapsed + the fn layer (the fn
// wires reference individual member files, meaningless once members merge;
// forced off and restored across the round-trip). applyVisibility runs the
// collapse pass (alphaTgt/dpos/supernode matrices), then the containment
// rebuild picks up the " · N files" labels.
document.getElementById("bCollapse").onclick = e => {
  collapsed = !collapsed;
  e.target.classList.toggle("on", collapsed);
  if (collapsed) { fnWasOn = fnMode; fnMode = false; cbFnEl.checked = false; }
  else { fnMode = fnWasOn; cbFnEl.checked = fnWasOn; fnWasOn = false; }
  applyVisibility();
  buildContainment();
};
function clearFocus() {
  // one scope for Esc / right-click / crumb ✕: drop the focus, the query,
  // the back-stack and the info panel together — and return every piece of
  // focus-scoped UI to its boot value (issue #31: fnMode/depth used to
  // stay dirty after Escape, so the next focus inherited a stale tier)
  focusSeeds.clear(); query = "";
  focusStack = [];
  camTween = null;   // [issue #112] Esc mid-tween must cancel it outright:
  // frameGraph sets the overview pose below, and the tick's tween block
  // would otherwise re-lerp the camera back toward the dead focus pose
  // for the remaining ~400ms, landing it on an empty de-compacted region
  document.getElementById("search").value = "";
  info.style.display = "none";
  // [issue #82] hiding the fn panel closes the 2D wire's menu: a pin that
  // menu introduced goes with it
  if (wirePin && wirePin.menu === "info") wirePinClear();
  depth = 1; depthEl.value = 1;
  document.getElementById("depthVal").textContent = "1";
  fnMode = cbFnEl.checked = true;   // boot default: checked (tier shows only in focus)
  if (showInst) {   // seeded by the focus transition (issue #39) — the boot
    showInst = false;   // default returns with the rest of the focus scope
    document.getElementById("bInst").classList.remove("on");
  }
  // [issue #112] the hover stalk is white depthTest-off ink over a fn
  // layer this teardown disposes — Esc with the mouse stationary (no
  // pointermove comes to hide it) left a bright line at the dead layer's
  // coordinates. The hover id dies with it.
  fnStalkHide(); hoveredFn = -1; _sfDirty = true;
  applyHighlight();   // query is empty -> clears hlArr/hlFn/.hl classes + results
  applyVisibility();
  mapTeardown();   // [issue #113] stale L3 freeze + vars chip rect die here
  frameGraph();   // the camera followed the focus in; it follows the reset out
}
addEventListener("keydown", e => {
  // [issue #82] Esc chain, one intent per press: 1st press dismisses the
  // sticky pin (consumed); the NEXT press walks the existing ladder —
  // wire tip -> map overlays (picker/list/freeze) -> clear focus. A pinned
  // highlight never silently swallows the older bindings; it queues ahead.
  if (e.key === "Escape" && wirePin) {
    // [issue #84] skeptic #4: one press = one intent, dismiss the sticky
    // selection WHOLE - the pin AND the overlay it owns - instead of
    // leaving an orphaned list/picker behind. The NEXT press walks the
    // existing ladder (tip -> map overlays -> clear focus) as pinned.
    const pinMenu = wirePin.menu;
    wirePinClear();
    if (pinMenu === "list") mapOvCloseOne();
    return;
  }
  if (e.key === "Escape" && wireTipEl.style.display !== "none") { hideWireTip(); return; }
  if (e.key === "Escape" && mapOvCloseOne()) return;   // map overlays own ESC first
  if (e.key === "Escape" && (focusSeeds.size || query)) clearFocus();
  else if (e.key === "Backspace" && !(e.target instanceof Element &&
    (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" ||
     e.target.isContentEditable)) &&
    focusSeeds.size && focusStack.length) {
    e.preventDefault();
    popFocus();
  }
});
// right-click (not a drag) exits node focus
renderer.domElement.addEventListener("contextmenu", e => {
  e.preventDefault();
  if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return;
  if (focusSeeds.size || query || info.style.display !== "none") clearFocus();
});
// [issue #82] right-click dismisses the pin FIRST and consumes the press:
// capture phase, ahead of every existing contextmenu handler (the 3D
// clear-focus above included, so dismissal never fights them — the next
// right-click, with no pin, fires the old behavior unchanged.
document.addEventListener("contextmenu", e => {
  if (!wirePin) return;
  e.preventDefault();
  e.stopPropagation();
  wirePinClear();
}, true);
// reset owns EVERY piece of UI state — one click must return the app to
// its boot state with nothing half-reset (vars and classes in lockstep)
function resetAll() {
  activeClusters.clear(); activeDirs.clear();
  deadOnly = false; cycOnly = false; query = ""; focusSeeds.clear(); focusStack = [];
  dirMode = 0; showSignals = true; showVar = false; fnMode = true; depth = 1;
  searchEl.value = ""; depthEl.value = 1;
  applyHighlight();   // query is empty -> drops hl classes + results list
  document.getElementById("depthVal").textContent = "1";
  mutOnly = false;
  showInst = false; showCalls = true; showTests = false; showGhost = false;
  groupsMode = false;   // coloring level is view state — reset to fine clusters
  collapsed = false; fnWasOn = false;   // supernode collapse off — resetAll's button wipe clears its .on
  cbFnEl.checked = true;   // boot default: functions box checked
document.getElementById("spread").value = 100;
document.getElementById("spreadVal").textContent = "1.0";
applySpread(1);
  info.style.display = "none";
  camTween = null;
  camera.position.set(0, 0, 1400); controls.target.set(0, 0, 0);
  document.querySelectorAll(".chip, button").forEach(x => x.classList.remove("on"));
  document.getElementById("bCalls").classList.add("on");
  document.getElementById("bSignals").classList.add("on");
  document.getElementById("bMut").classList.remove("on");
  document.querySelector("#dirRow .seg").classList.add("on");
  // ground is a viewport pref, not filter state - it survives the reset
  if (showGround) document.getElementById("bGround").classList.add("on");
  // the map pane is a viewport pref too — keep its button in lockstep
  if (mapVisible) document.getElementById("bMap").classList.add("on");
  // ...but its content state resets with everything else
  mapExpandUser.clear(); mapVarsOn = false; mapHover = -1;
  mapTeardown();   // [issue #113] one clear function for the 2D surface
  // [issue #112] "return everything to boot" includes EVERY pin, not just
  // list-menu ones: a tip/info pin (and its persistent wire tip) used to
  // survive the reset — violating the pin-dies-with-its-menu rule that
  // clearFocus implements. wirePinClear also hides the tip.
  wirePinClear();
  mapOvCloseOne();
  frameGraph();
  buildLegend();
  buildContainment(); applyVisibility();
}
document.getElementById("bReset").onclick = resetAll;

const info = document.getElementById("info");
let panelCopyText = "";   // res:// target of whatever the info panel shows
function copyPanelPath() {
  const t = panelCopyText;
  if (!t) return;
  const done = () => {
    const b = document.getElementById("iCopy");
    b.textContent = "✓";
    setTimeout(() => { b.textContent = "⧉"; }, 900);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done, done);
  } else {
    // file:// pages may lack the async clipboard API
    const ta = document.createElement("textarea");
    ta.value = t; document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (err) {}
    ta.remove(); done();
  }
}
document.getElementById("iCopy").onclick = copyPanelPath;

// render one directed section of the connections panel; entries beyond 24
// collapse into an explicit "+N more hidden" footer (never silently)
function renderSection(kindId, ulId, entries, degArr, degWord, onJump) {
  const arr = [...entries.values()].sort((a, b) => b.w - a.w);
  document.getElementById(kindId).textContent =
    kindId === "kUses" ? `USES (${arr.length})` : `USED BY (${arr.length})`;
  const ul = document.getElementById(ulId);
  ul.innerHTML = "";
  const shown = arr.slice(0, 24);
  shown.forEach(({ j, rel, w, vis }) => {
    const li = document.createElement("li");
    li.textContent = `${nodes[j].label} · ${rel} ×${w} · ${degArr[j]} ${degWord}`;
    if (!vis) li.style.opacity = 0.45;
    li.onclick = () => onJump(j);
    ul.appendChild(li);
  });
  if (arr.length > shown.length) {
    const li = document.createElement("li");
    li.className = "more";
    li.textContent = `+${arr.length - shown.length} more hidden`;
    ul.appendChild(li);
  }
}
function showInfo(i) {
  const n = nodes[i];
  panelCopyText = "res://" + n.path;
  info.style.display = "block";
  document.getElementById("iTitle").textContent = n.label;
  document.getElementById("iSub").textContent = n.dir || "/";
  const tags = document.getElementById("iTags");
  tags.innerHTML = "";
  const mk = (txt, color) => {
    const t = document.createElement("span");
    t.className = "tag"; t.textContent = txt;
    t.style.background = color + "22"; t.style.color = color;
    tags.appendChild(t);
  };
  mk(n.ext, "#80cbc4");
  if (n.cls) mk(n.cls, "#ce93d8");
  if (n.cluster >= 0) mk("cluster c" + n.cluster, `#${new THREE.Color().setHSL(hue(n.cluster),0.72,lightOf(n.cluster)).getHexString()}`);
  if (n.dl || n.dead > 0) mk(n.dl ? "likely dead" : "maybe dead", "#ef5350");
  // directed halves: USES = edges this file sends, USED BY = edges it receives
  const outs = new Map(), ins = new Map();
  links.forEach(l => {
    // list every relationship type regardless of the view toggles;
    // entries whose type is currently toggled off render dimmed
    let j, rel, tgt;
    if (l.s === i) { j = l.t; rel = l.ty === "inst" ? "contains" : l.ty === "attach" ? "attaches" : l.ty; tgt = outs; }
    else if (l.t === i) { j = l.s; rel = l.ty === "inst" ? "part of" : l.ty === "attach" ? "used by" : l.ty; tgt = ins; }
    else return;
    const key = j + "|" + rel;
    const cur = tgt.get(key) || { j, rel, w: 0, vis: false };
    cur.w += l.w;
    if (typeVisible(l.ty)) cur.vis = true;
    tgt.set(key, cur);
  });
  const jump = j => { pushFocusState(); showInfo(j); focusSeeds.clear(); focusSeeds.add(j); applyVisibility(); focus(j); };
  renderSection("kUses", "iUses", outs, outDeg, "downstream", jump);
  renderSection("kUsedBy", "iUsedBy", ins, inDeg, "upstream", jump);
  // #279: semantic neighbors — the card rides the SAME capped rows the
  // wires ride (bake-ranked, descending cosine); render gates are UI-only
  const nbrs = semByNode.get(i) || [];
  const kSemEl = document.getElementById("kSem");
  const iSemEl = document.getElementById("iSem");
  const hasN = nbrs.length > 0;
  kSemEl.style.display = hasN ? "" : "none";
  iSemEl.style.display = hasN ? "" : "none";
  if (hasN) {
    kSemEl.textContent = `SEMANTIC NEIGHBORS (${nbrs.length})`;
    iSemEl.innerHTML = "";
    nbrs.slice(0, 5).forEach(({ j, s }) => {
      const li = document.createElement("li");
      li.textContent = `${nodes[j].label} · cos ${s.toFixed(2)}`;
      li.onclick = () => jump(j);
      iSemEl.appendChild(li);
    });
    if (nbrs.length > 5) {
      const li = document.createElement("li");
      li.className = "more";
      li.textContent = `+${nbrs.length - 5} more hidden`;
      iSemEl.appendChild(li);
    }
  }
}
function focus(i) {
  // tween the camera around node i (400ms ease-out). Distance frames the
  // COMPACT BALL, not a fixed 320: the ball's radius scales with the
  // neighborhood, and a fixed distance parks the camera INSIDE it — the
  // hub ring and the lit fan end up clipped off-screen. focus() callers
  // all run applyVisibility() first, so compactTgt is the landed layout.
  const to = new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2]);
  const dir = new THREE.Vector3(camera.position.x - to.x,
    camera.position.y - to.y, camera.position.z - to.z);
  if (dir.lengthSq() < 1) dir.set(0.42, 0.5, 0.76);
  dir.normalize();
  let r = 0;
  if (compactTgt && compactIdx && compactIdx.length) {
    const rad = sphR(i);
    for (let q = 0; q < compactIdx.length; q++) {
      const j = compactIdx[q];
      const d = Math.hypot(compactTgt[j*3] - to.x, compactTgt[j*3+1] - to.y,
        compactTgt[j*3+2] - to.z) + rad;
      if (d > r) r = d;
    }
  }
  tweenCamTo(to, to.clone().addScaledVector(dir, Math.max(320, r * 2.1)));
}
// [#62] depth-slider camera nudge: growing (or shrinking) the radius
// mid-focus changes the lit set past the framing focus() computed for
// the 1-hop ball — level 2-3 nodes sit at their frozen layout positions,
// map-scale away, so served content left the frame until a re-click.
// Mirror focus()'s tween around the live lit set (level <= depth), but
// distance from the ACTIVE half-fov: the map pane can narrow the canvas
// below square (aspect < 1), making the horizontal half-fov the tighter
// one — r*2.1 lands the silhouette edge on the vertical border (asin(1/2.1)
// = 28.4° vs 27.5°) and overflows horizontally when pane-narrowed; the
// ball's small radius hides this from focus()'s 320 floor. A user grab
// cancels it like any tween.
function reframeOnDepth() {
  if (!focusSeeds.size) return;
  let cx = 0, cy = 0, cz = 0, n0 = 0;
  for (let i = 0; i < N; i++) if (level[i] === 0) {
    cx += pos[i*3]; cy += pos[i*3+1]; cz += pos[i*3+2]; n0++;
  }
  if (!n0) return;
  const to = new THREE.Vector3(cx / n0, cy / n0, cz / n0);
  const dir = new THREE.Vector3(camera.position.x - to.x,
    camera.position.y - to.y, camera.position.z - to.z);
  if (dir.lengthSq() < 1) dir.set(0.42, 0.5, 0.76);
  dir.normalize();
  let r = 0;
  for (let i = 0; i < N; i++) {
    if (level[i] < 0 || level[i] > depth || alphaTgt[i] <= 0.5) continue;
    const dd = Math.hypot(pos[i*3] - to.x, pos[i*3+1] - to.y, pos[i*3+2] - to.z) + sphR(i);
    if (dd > r) r = dd;
  }
  const vHalf = camera.fov * Math.PI / 360;
  const hHalf = Math.atan(Math.tan(vHalf) * camera.aspect);
  const fit = r / Math.sin(Math.min(vHalf, hHalf)) * 1.02;
  tweenCamTo(to, to.clone().addScaledVector(dir, Math.max(320, fit)));
}

function showFnInfo(k) {
  const fm = fnMeta[k];
  info.style.display = "block";
  document.getElementById("iTitle").textContent = fm.name + "()";
  document.getElementById("iSub").textContent = nodes[fm.file].path;
  panelCopyText = "res://" + fnKey(fm.file, fm.name);
  const tags = document.getElementById("iTags");
  tags.innerHTML = "";
  // IO surface: signature line + writes/mutates chips (fn-IO feature)
  const io = (DATA.fio || {})[fnKey(fm.file, fm.name)];
  if (io) {
    if (io.sig) {
      const sig = document.createElement("div");
      sig.style.cssText = "font:11px/1.5 monospace;color:#cfd8dc;margin:2px 0 6px;word-break:break-all";
      sig.textContent = io.sig + (io.ret ? " -> " + io.ret : "");
      tags.appendChild(sig);
    }
    const chip = (txt, bg) => {
      const s = document.createElement("span");
      s.style.cssText = `display:inline-block;margin:0 4px 4px 0;padding:2px 7px;border-radius:6px;font-size:10.5px;color:#eceff1;background:${bg}`;
      s.textContent = txt;
      return s;
    };
    if (io.w.length) tags.appendChild(chip("✎ writes: " + io.w.join(", "), "rgba(0,105,92,.55)"));
    if (io.mp.length) tags.appendChild(chip("⇄ mutates: " + io.mp.join(", "), "rgba(180,100,20,.45)"));
    if (!io.w.length && !io.mp.length) tags.appendChild(chip("pure — no state writes", "rgba(55,71,79,.7)"));
  }
  // jumping to a caller/callee focuses its file and keeps the fn layer on
  const jumpFn = j => {
    if (!fnMode) { fnMode = true; document.getElementById("cbFn").checked = true; }
    pushFocusState();
    focusSeeds.clear(); focusSeeds.add(j);
    showInfo(j); applyVisibility(); focus(j);
  };
  const renderFn = (kindId, ulId, label, entries) => {
    document.getElementById(kindId).textContent = `${label} (${entries.length})`;
    const ul = document.getElementById(ulId);
    ul.innerHTML = "";
    // no 24-cap: the ul's max-height + overflow-y scroll carries any
    // length (map-spec-v2 section 8 BUGFIX)
    entries.forEach(e => {
      const li = document.createElement("li");
      const j = kindId === "kUses" ? e[2] : e[0];
      li.textContent = nodes[j].label + " :: " + (kindId === "kUses" ? e[3] : e[1]);
      li.onclick = () => jumpFn(j);
      ul.appendChild(li);
    });
  };
  const outs = [], ins = [], seen = new Set();
  fedges.forEach(e => {
    if (e[0] === fm.file && e[1] === fm.name) {
      const key = "→" + e[2] + "::" + e[3];
      if (!seen.has(key)) { seen.add(key); outs.push(e); }
    }
    if (e[2] === fm.file && e[3] === fm.name) {
      const key = "←" + e[0] + "::" + e[1];
      if (!seen.has(key)) { seen.add(key); ins.push(e); }
    }
  });
  renderFn("kUses", "iUses", "CALLS", outs);
  renderFn("kUsedBy", "iUsedBy", "CALLED BY", ins);
}

// which sphere a function belongs to
let fnStalk = null;
function fnStalkUpdate(fi, p) {
  if (!fnStalk) {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(6), 3));
    fnStalk = new THREE.Line(g, new THREE.LineBasicMaterial(
      { color: 0xffffff, transparent: true, opacity: 0.85, depthTest: false }));
    fnStalk.renderOrder = 5;
    scene.add(fnStalk);
  }
  const a = fnStalk.geometry.attributes.position.array;
  a[0] = p[0]; a[1] = p[1]; a[2] = p[2];
  a[3] = pos[fi*3]; a[4] = pos[fi*3+1]; a[5] = pos[fi*3+2];
  fnStalk.geometry.attributes.position.needsUpdate = true;
  fnStalk.visible = true;
}
function fnStalkHide() { if (fnStalk) fnStalk.visible = false; }

// hover greyout: steal from Cosmograph (cosmos config.ts highlightedPointIndices
// / linkGreyoutOpacity 0.1) — dim everything outside the hovered node's 1-hop
// neighborhood instead of waiting for a click. Grey, not hidden: structure
// stays on screen, the eye gets an instant "what relates to this".
// greyout also dims the DOM label layers (hub pills, cluster names, focus
// labels): labels at full ink floating over a greyed scene read as
// un-greyed content
"""
