# vizjs/head.py — rung 1/17 of the template join (issues
# #86 phase-3 / #299 A): HTML/CSS document head + UI chrome markup. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_HTML_HEAD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>neuronav — code graph</title>
<style>
  :root { --pane-w: 800px; }   /* map pane width — divider drag rewrites it */
  html, body { margin:0; height:100%; background:#000; overflow:hidden;
    font: 13px/1.45 "Segoe UI", system-ui, sans-serif; color:#cfd8dc; }
  /* camera drags must never text-select the overlays (labels/panel swallowed
     pointer drags and froze the camera) */
  body, body * { user-select: none; -webkit-user-select: none; }
  input, textarea { user-select: text; -webkit-user-select: text; }
  #panel { position:fixed; top:12px; left:12px; z-index:10; width:230px;
    background:rgba(10,14,18,.82); border:1px solid #1de9b633; border-radius:10px;
    padding:12px; backdrop-filter: blur(4px); }
  #panel h1 { font-size:14px; margin:0 0 8px; color:#1de9b6; letter-spacing:1px; }
  #stats { font-size:11px; color:#78909c; margin-bottom:8px; }
  #caption { font-size:10.5px; color:#546e7a; margin:-2px 0 8px; }
  #search { width:100%; box-sizing:border-box; background:#0b1116; color:#cfd8dc;
    border:1px solid #263238; border-radius:6px; padding:6px 8px; outline:none; }
  #search:focus { border-color:#1de9b688; }
  #depthRow { display:flex; align-items:center; gap:6px; margin-top:8px;
    font-size:11px; color:#78909c; }
  #depth { flex:1; accent-color:#1de9b6; }
  #depthRow .cb { display:flex; align-items:center; gap:4px; cursor:pointer; }
  #depthRow .cb input { accent-color:#1de9b6; }
  #legend { margin-top:10px; max-height:38vh; overflow-y:auto; }
  .chip { display:inline-block; margin:2px; padding:2px 8px; border-radius:10px;
    font-size:10.5px; cursor:pointer; border:1px solid transparent; }
  .chip:hover { border-color:#fff5; }
  .chip.on { outline:1px solid #fff; }
  #toggles { margin-top:10px; display:flex; flex-wrap:wrap; gap:6px 4px; }
  button { flex:1 1 21%; background:#0b1116; color:#b0bec5; border:1px solid #263238;
    border-radius:6px; padding:5px 2px; cursor:pointer; font-size:11px;
    text-align:center; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  button.on { color:#1de9b6; border-color:#1de9b688; }
  #info { position:fixed; top:12px; right:12px; z-index:10; width:290px;
    transition:right .25s ease;
    background:rgba(10,14,18,.88); border:1px solid #1de9b633; border-radius:10px;
    padding:12px; display:none; backdrop-filter: blur(4px); }
  #info.mapShift { right: calc(var(--pane-w) + 18px); }  /* clear of the map pane */
  #info h2 { font-size:13px; margin:0 0 4px; color:#fff; word-break:break-all; }
  #info .sub { font-size:11px; color:#78909c; margin-bottom:8px; }
  #info .tag { display:inline-block; font-size:10px; padding:1px 7px;
    border-radius:8px; margin:0 4px 6px 0; }
  #info ul { list-style:none; margin:6px 0 0; padding:0; max-height:24vh;
    overflow-y:auto; }
  #info li { padding:3px 6px; border-radius:5px; cursor:pointer; font-size:11.5px; }
  #info li:hover { background:#1de9b61a; color:#1de9b6; }
  .kind { color:#546e7a; font-size:10px; text-transform:uppercase;
    letter-spacing:1px; margin-top:8px; }
  #dirRow { display:flex; align-items:center; gap:5px; margin-top:6px;
    font-size:11px; color:#78909c; }
  #dirRow .seg { flex:1; padding:3px 0; font-size:10.5px; }
  #iCopy { position:absolute; top:9px; right:9px; width:26px; height:22px;
    padding:0; flex:none; background:#0b1116; color:#78909c;
    border:1px solid #263238; border-radius:5px; cursor:pointer; font-size:11px; }
  #iCopy:hover { color:#1de9b6; border-color:#1de9b688; }
  #info li.more { color:#78909c; cursor:default; font-size:10.5px; }
  #info li.more:hover { background:none; color:#78909c; }
  #tip { position:fixed; z-index:20; pointer-events:none; display:none;
    background:#000d; border:1px solid #1de9b644; color:#eee; font-size:11px;
    padding:4px 8px; border-radius:6px; white-space:pre-line; }
  #lg3d { position:fixed; left:10px; bottom:10px; z-index:21; width:22px; height:22px;
    display:flex; align-items:center; justify-content:center; cursor:pointer;
    background:rgba(10,14,18,.82); border:1px solid #1de9b644; border-radius:50%;
    color:#80cbc4; font-size:12px; font-weight:700; user-select:none; }
  #lg3dx { position:fixed; left:38px; bottom:8px; z-index:21; display:none;
    align-items:center; gap:14px; padding:4px 10px;
    background:rgba(10,14,18,.88); border:1px solid #1de9b633; border-radius:12px;
    font-size:10.5px; color:#b0bec5; white-space:nowrap; }
  #lg3dx span { display:flex; align-items:center; gap:5px; }
  #lg3dx i { display:inline-block; }
  .lgTrunk { width:16px; height:4px; border-radius:2px;
    background:linear-gradient(90deg,#e8996d,#e8b084); }
  .lgDot { width:9px; height:9px; border-radius:50%; background:#f2efe4; }
  .lgChev { width:0; height:0; border-left:5px solid transparent;
    border-right:5px solid transparent; border-bottom:9px solid #f5a623; }
  .lgDash { width:16px; border-top:2px dashed #546e7a; }
  .lgSem { width:16px; border-top:3px dotted #b892ff; }
  #edgeLegend { display:flex; flex-wrap:wrap; gap:3px 10px; margin-top:6px;
    font-size:10px; color:#78909c; }
  .eKey { display:flex; align-items:center; gap:4px; }
  .eKey i { width:14px; border-top:2px solid #ffffff55; display:inline-block; }
  .eHint { color:#546e7a; }
  #crumb { position:fixed; top:12px; left:50%; transform:translateX(-50%);
    z-index:10; display:none; align-items:center; gap:6px;
    background:rgba(10,14,18,.82); border:1px solid #1de9b633;
    border-radius:14px; padding:4px 7px 4px 14px; font-size:11px; color:#b0bec5; }
  /* pane open: center over the 3D region, not the window - a wide map pane
     (default 800) would otherwise put the crumb ON TOP of the pane's vars
     chip, stealing its clicks */
  body.mapOpen #crumb { left: calc((100% - var(--pane-w)) / 2); }
  #crumb b { color:#1de9b6; font-weight:600; }
  #crumb .x { cursor:pointer; color:#78909c; padding:0 6px; border-radius:50%;
    line-height:1.3; }
  #crumb .x:hover { color:#fff; background:#ffffff14; }
  #crumb .back { cursor:pointer; color:#1de9b6; padding:0 6px; border-radius:50%;
    line-height:1.3; }
  #crumb .back:hover { color:#fff; background:#ffffff14; }
  #crumb .hint { color:#546e7a; }
  #crumb .f { cursor:pointer; color:#ffcc80; padding:0 7px; border-radius:9px;
    border:1px solid #ffcc8044; }
  #crumb .f:hover { background:#ffcc8022; }
  #hubs { position:fixed; inset:0; z-index:5; pointer-events:none;
    overflow:hidden; }
  .hub { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    max-width:230px; overflow:hidden; text-overflow:ellipsis;
    font-size:10.5px; color:#eceff1; background:rgba(8,12,16,.8);
    padding:1px 7px; border-radius:7px; border:1px solid #ffffff1f;
    pointer-events:auto; cursor:pointer; }
  .hub:hover { border-color:#fff7; background:rgba(18,26,32,.88); }
  #elabs { position:fixed; inset:0; z-index:4; pointer-events:none;
    overflow:hidden; }
  .elab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:11px; padding:0 5px; border-radius:5px;
    background:rgba(8,12,16,.75); pointer-events:none; }
  #xtlabs { position:fixed; inset:0; z-index:4; pointer-events:none;
    overflow:hidden; }
  .xtlab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:10.5px; color:#b0bec5; padding:0 5px; border-radius:5px;
    background:rgba(8,12,16,.75); pointer-events:none; }
  #clabs { position:fixed; inset:0; z-index:3; pointer-events:none;
    overflow:hidden; }
  .clab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:12px; font-weight:700; letter-spacing:1.5px; opacity:.95;
    text-shadow:0 0 6px #000, 0 1px 3px #000, 0 0 12px #000; }
  #flabs { position:fixed; inset:0; z-index:4; pointer-events:none;
    overflow:hidden; }
  .flab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:11px; padding:1px 6px; border-radius:5px; cursor:pointer;
    background:rgba(8,12,16,.78); color:#cfd8dc; pointer-events:auto;
    text-shadow:0 1px 2px #000; }
  .flab:hover { color:#fff; background:rgba(20,30,38,.92); }
  .flab.fn { font-size:10px; color:#8fa3ad; background:rgba(8,12,16,.6); }
  /* [#8] super-hub fold chip: the count that replaces the folded 1-hop
     fan — dashed reads as an aggregate, not a file */
  .flab.fold { color:#9fb3bc; background:rgba(13,20,26,.85);
    border:1px dashed #607d8b; }
  .flab.fold:hover { color:#fff; background:rgba(20,30,38,.95); }
  /* search matches (issue #51): hubs + fn labels carrying .hl lift to the
     accent — the toggles existed but no rule backed them (dead ink) */
  .hub.hl, .flab.hl { color:#1de9b6; background:#1de9b626;
    border-color:#1de9b666; }
  .flab.fn:hover { color:#d0f2ea; background:rgba(14,26,24,.9); }
  #stubLabs { position:fixed; inset:0; z-index:6; pointer-events:none;
    overflow:hidden; }
  .stublab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:10px; color:#8fa3ad; padding:0 5px; border-radius:5px;
    background:rgba(8,12,16,.75); }
  /* 3D region: canvas pinned left of the map pane; renderer.setSize keeps
     its style box in sync on every divider/resize event */
  canvas#gl { position:fixed; top:0; left:0; display:block; }
  #mapPane { position:fixed; top:0; right:0; width:var(--pane-w); height:100%;
    display:block; background:#0b0f14; z-index:8;
    border-left:1px solid #1de9b633; }
  #mapPane.collapsed { display:none; }
  /* draggable vertical split between the 3D view and the map pane */
  #divider { position:fixed; top:0; right:var(--pane-w); width:7px; height:100%;
    z-index:12; cursor:col-resize; background:transparent;
    border-left:1px solid #1de9b633; touch-action:none; user-select:none; }
  #divider:hover, #divider.drag { background:#1de9b622; }
  #divider.collapsed { display:none; }
  /* map-local DOM overlays (tooltip / bundle list / fn picker) — one
     container spanning the pane area, children opt into pointer events */
  #mapOv { position:fixed; top:0; right:0; width:var(--pane-w); height:100%;
    z-index:9; pointer-events:none; overflow:hidden; display:none;
    font:10.5px ui-monospace, Menlo, Consolas, monospace; color:#cfd8dc; }
  body.mapOpen #mapOv { display:block; }
  /* label overlays are projected in CANVAS space — confine them to the 3D
     region whenever the map pane cedes width */
  body.mapOpen #hubs, body.mapOpen #elabs, body.mapOpen #xtlabs,
  body.mapOpen #clabs, body.mapOpen #flabs { right: var(--pane-w); }
  #mapTip { position:absolute; display:none; background:#000d;
    border:1px solid #1de9b644; color:#eee; padding:4px 8px; border-radius:6px;
    white-space:pre-line; max-width:280px; line-height:1.5; }
  #wireTip { position:fixed; display:none; z-index:30; background:#000e;
    border:1px solid #1de9b655; color:#eee; padding:6px 10px; border-radius:6px;
    font-size:12px; white-space:pre-line; max-width:340px; max-height:45vh;
    overflow-y:auto; line-height:1.5; pointer-events:none; }
  #mapList { position:absolute; display:none; pointer-events:auto;
    background:rgba(8,12,16,.95); border:1px solid #263238; border-radius:8px;
    padding:8px; min-width:240px; max-width:300px; max-height:50vh;
    overflow-y:auto; }
  #mapList h3 { margin:0 0 6px; font-size:11px; font-weight:600; color:#1de9b6;
    word-break:break-all; }
  #mapList .row, #mapPick .row, #fnPick .row, #searchResults .row {
    padding:2px 4px; border-radius:4px; cursor:pointer; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis; }
  #mapList .row:hover, #mapPick .row:hover, #fnPick .row:hover,
  #searchResults .row:hover { background:#1de9b61a; color:#1de9b6; }
  /* search results dropdown: rides the sidebar under #search (fnPick
     pattern — fixed, viewport coords, hidden until a query matches) */
  #searchResults { display:none; position:fixed; z-index:6;
    background:rgba(8,12,16,.95); border:1px solid #263238; border-radius:8px;
    padding:8px; width:250px; max-height:40vh; overflow-y:auto; }
  #searchResults .more { padding:2px 4px; color:#546e7a; font-size:10.5px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  #searchResults .fnrow { color:#8fa3ad; }
  #searchResults .fnrow b { color:#ffcc80; font-weight:600; }
  #mapPick, #fnPick { display:none; pointer-events:auto;
    background:rgba(8,12,16,.95); border:1px solid #263238; border-radius:8px;
    padding:8px; width:250px; max-height:40vh; overflow-y:auto; }
  #mapPick { position:absolute; }
  /* fn picker rides the 3D view: body-level fixed, viewport coords */
  #fnPick { position:fixed; z-index:6; }
  #mapPick input, #fnPick input { width:100%; box-sizing:border-box; background:#0b1116;
    color:#cfd8dc; border:1px solid #263238; border-radius:5px; padding:4px 6px;
    outline:none; font:inherit; margin-bottom:6px; }
  #mapPick input:focus, #fnPick input:focus { border-color:#1de9b688; }
  </style>
</head>
<body>
<div id="panel">
  <h1>neuronav</h1>
  <div id="caption">color = subsystem · size = connectivity · click a node to explore</div>
  <div id="stats"></div>
  <div id="edgeLegend"></div>
  <input id="search" placeholder="search file / class…">
  <div id="searchResults"></div>
  <div id="depthRow">
    <span>depth</span>
<input id="depth" type="range" min="1" max="3" value="1">
<span id="depthVal">1</span>
<span style="margin-left:10px">spread</span>
<input id="spread" type="range" min="60" max="260" value="100" title="stretch the whole layout apart (scales from the centroid)">
<span id="spreadVal">1.0</span>
    <label class="cb"><input type="checkbox" id="cbFn" checked> functions</label>
    <label class="cb"><input type="checkbox" id="cbSpin"> spin</label>
  </div>
  <div id="dirRow">
    <span>dir</span>
    <button class="seg on" data-d="0">both</button>
    <button class="seg" data-d="1">out</button>
    <button class="seg" data-d="2">in</button>
  </div>
  <div class="kind">subsystems</div>
  <div id="legend"></div>
  <div class="kind">folders</div>
  <div id="dirs"></div>
  <div id="toggles">
  <button id="bCalls" class="on">calls</button>
  <button id="bSignals" class="on">signals</button>
  <button id="bSemAff" class="on">affinity</button>
   <!-- bSemAff tooltip is set from DATA.meta at boot (#368): top-k + floor
       are bake law, never restated in static HTML -->
<button id="bMut" title="fn layer: only functions that write member state (✎ badge)">mutators</button>
    <button id="bInst">contains</button>
  <button id="bVar" title="member-var references — dense, off by default">var</button>
  <button id="bGhost" title="focus mode: ghost wires outside the hub budget (quiet layer — off by default; hover a node or wire to reveal)">ghosts</button>
    <button id="bGround" title="fixed ground grid under the graph (orientation aid)">ground</button>
    <button id="bGroups" title="recolor by coarse supergroups (two-level navigation)">groups</button>
    <button id="bCollapse" title="collapse every cluster of 3+ visible files into one supernode; edges re-attach to the merged sphere">collapse</button>
    <button id="bDead" title="show only files flagged dead: at least 40% of their funcs are dead candidates">dead only</button>
    <button id="bMap" title="collapse / expand the named-wire map pane (right)">map</button>
    <button id="bCyc" title="show only files inside call cycles (strongly connected components); cycle members tint red like madge's cyclic marker">cycles</button>
    <button id="bReset">reset</button>
  </div>
</div>
<div id="crumb"></div>
<div id="info" style="display:none">
  <h2 id="iTitle"></h2>
  <div class="sub" id="iSub"></div>
  <button id="iCopy" title="copy res:// path">⧉</button>
  <div id="iTags"></div>
  <div class="kind" id="kUses">USES (0)</div>
  <ul id="iUses"></ul>
  <div class="kind" id="kUsedBy">USED BY (0)</div>
  <ul id="iUsedBy"></ul>
  <div class="kind" id="kSem">SEMANTIC NEIGHBORS (0)</div>
  <ul id="iSem"></ul>
</div>
<div id="tip"></div>
<div id="lg3d" title="what am I looking at?">?</div>
<div id="lg3dx">
  <span><i class="lgTrunk"></i>trunk = bundled calls (one corridor)</span>
  <span><i class="lgSem"></i>violet dotted = semantic affinity twins (embedding cosine — not a dependency)</span>
  <span><i class="lgDot"></i>ivory dot = junction (wires merge)</span>
  <span><i class="lgChev"></i>amber chevron = delivery direction</span>
  <span><i class="lgDash"></i>dashed = quiet (many thin calls)</span>
</div>
<div id="hubs"></div>
<div id="stubLabs"></div>
<div id="elabs"></div>
<div id="xtlabs"></div>
<div id="flabs"></div>
<div id="clabs"></div>
<canvas id="mapPane"></canvas>
<div id="divider" title="drag to resize the map pane"></div>

<script type="importmap">
__IMPORTMAP__
</script>
"""
