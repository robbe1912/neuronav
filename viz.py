"""neuronav viz: self-contained force-directed 3D graph of the indexed repo.

Generates `graph.html` (single file, three.js from CDN). Nodes = indexed
files colored by semantic cluster; red-mixed nodes contain dead-code
candidates. Edges = aggregated structural links (call edges, scene
instancing, scene→script attachment).

Usage:  python viz.py            # writes graph.html next to this file
        python viz.py out.html   # custom output path
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import nav
import graph


def _build_data() -> dict:
    g = graph.get_graph()
    clusters = nav.clusters()

    # file -> cluster id
    file_cluster: dict[str, int] = {}
    for c in clusters:
        for path, _cls in c["paths"]:
            file_cluster[path] = int(c["id"])

    # dead-code candidates per file (tier-aware: likely=1, review=0.5)
    dead = g.dead_code(limit=10**9)
    dead_weight: dict[str, float] = defaultdict(float)
    for cand in dead["candidates"]:
        dead_weight[cand["path"]] += 1.0 if cand["tier"] == "likely" else 0.5

    # nodes: files known to nav's index (searchable corpus) ∪ graph files
    paths = sorted(
        set(file_cluster)
        | {rel for rel, fs in g.files.items() if not rel.startswith("assets/")}
    )
    idx: dict[str, int] = {}
    nodes: list[dict] = []
    for p in paths:
        fs = g.files.get(p)
        idx[p] = len(nodes)
        degree = 0
        nodes.append(
            {
                "id": idx[p],
                "path": p,
                "label": p.rsplit("/", 1)[-1],
                "dir": p.rsplit("/", 1)[0] if "/" in p else "",
                "ext": fs.ext if fs else Path(p).suffix,
                "cls": fs.class_name if fs else "",
                "cluster": file_cluster.get(p, -1),
                "dead": dead_weight.get(p, 0.0),
            }
        )

    # edges: aggregate func-level call edges to file level + instancing + attach
    edge_weight: dict[tuple[int, int], int] = defaultdict(int)

    def add_edge(src: str, dst: str) -> None:
        if src in idx and dst in idx and src != dst:
            edge_weight[(idx[src], idx[dst])] += 1

    for src_key, dsts in g.edges.items():
        src = src_key.split("::")[0]
        for dst_key in dsts:
            if dst_key.endswith("::tscn"):
                continue
            add_edge(src, dst_key.split("::")[0])

    for rel, fs in g.files.items():
        if fs.ext != ".tscn":
            continue
        # attached script: ext_resource path stored on the scene
        att = fs.attached_script
        if att:
            add_edge(rel, att.removeprefix("res://"))
        for inst in fs.instances:
            add_edge(rel, inst.removeprefix("res://"))

    links = [
        {"s": s, "t": t, "w": w} for (s, t), w in sorted(edge_weight.items())
    ]

    n_clusters = len(clusters)
    return {
        "nodes": nodes,
        "links": links,
        "meta": {
            "files": len(nodes),
            "edges": len(links),
            "clusters": n_clusters,
            "deadFiles": sum(1 for v in dead_weight.values() if v >= 1.0),
            "deadLikely": dead["by_tier"].get("likely", 0),
            "deadReview": dead["by_tier"].get("review", 0),
        },
    }


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>neuronav — code graph</title>
<style>
  html, body { margin:0; height:100%; background:#000; overflow:hidden;
    font: 13px/1.45 "Segoe UI", system-ui, sans-serif; color:#cfd8dc; }
  #panel { position:fixed; top:12px; left:12px; z-index:10; width:230px;
    background:rgba(10,14,18,.82); border:1px solid #1de9b633; border-radius:10px;
    padding:12px; backdrop-filter: blur(4px); }
  #panel h1 { font-size:14px; margin:0 0 8px; color:#1de9b6; letter-spacing:1px; }
  #stats { font-size:11px; color:#78909c; margin-bottom:8px; }
  #search { width:100%; box-sizing:border-box; background:#0b1116; color:#cfd8dc;
    border:1px solid #263238; border-radius:6px; padding:6px 8px; outline:none; }
  #search:focus { border-color:#1de9b688; }
  #legend { margin-top:10px; max-height:38vh; overflow-y:auto; }
  .chip { display:inline-block; margin:2px; padding:2px 8px; border-radius:10px;
    font-size:10.5px; cursor:pointer; border:1px solid transparent; }
  .chip:hover { border-color:#fff5; }
  .chip.on { outline:1px solid #fff; }
  #toggles { margin-top:10px; display:flex; gap:6px; }
  button { flex:1; background:#0b1116; color:#b0bec5; border:1px solid #263238;
    border-radius:6px; padding:5px 0; cursor:pointer; font-size:11px; }
  button.on { color:#1de9b6; border-color:#1de9b688; }
  #info { position:fixed; top:12px; right:12px; z-index:10; width:290px;
    background:rgba(10,14,18,.88); border:1px solid #1de9b633; border-radius:10px;
    padding:12px; display:none; backdrop-filter: blur(4px); }
  #info h2 { font-size:13px; margin:0 0 4px; color:#fff; word-break:break-all; }
  #info .sub { font-size:11px; color:#78909c; margin-bottom:8px; }
  #info .tag { display:inline-block; font-size:10px; padding:1px 7px;
    border-radius:8px; margin:0 4px 6px 0; }
  #info ul { list-style:none; margin:6px 0 0; padding:0; max-height:34vh;
    overflow-y:auto; }
  #info li { padding:3px 6px; border-radius:5px; cursor:pointer; font-size:11.5px; }
  #info li:hover { background:#1de9b61a; color:#1de9b6; }
  .kind { color:#546e7a; font-size:10px; text-transform:uppercase;
    letter-spacing:1px; margin-top:8px; }
  #tip { position:fixed; z-index:20; pointer-events:none; display:none;
    background:#000d; border:1px solid #1de9b644; color:#eee; font-size:11px;
    padding:4px 8px; border-radius:6px; }
</style>
</head>
<body>
<div id="panel">
  <h1>neuronav</h1>
  <div id="stats"></div>
  <input id="search" placeholder="search file / class…">
  <div id="legend"></div>
  <div id="toggles">
    <button id="bDead">dead code</button>
    <button id="bReset">reset view</button>
  </div>
</div>
<div id="info">
  <h2 id="iTitle"></h2>
  <div class="sub" id="iSub"></div>
  <div id="iTags"></div>
  <div class="kind">connections</div>
  <ul id="iLinks"></ul>
</div>
<div id="tip"></div>

<script type="importmap">
{ "imports": {
  "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
  "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
} }
</script>
<script type="module">
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const DATA = __DATA__;
const nodes = DATA.nodes, links = DATA.links, N = nodes.length;

// ---- scene -----------------------------------------------------------------
const renderer = new THREE.WebGLRenderer({ antialias:true });
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x000000, 0.0006);
const camera = new THREE.PerspectiveCamera(55, innerWidth/innerHeight, 1, 20000);
camera.position.set(0, 0, 1400);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

// cluster hue: golden angle spread; dead files tinted toward red
const hue = c => c < 0 ? 0.08 : (c * 0.61803398875 + 0.55) % 1;
const colorOf = n => {
  const col = new THREE.Color().setHSL(hue(n.cluster), 0.72, 0.58);
  if (n.dead > 0) col.lerp(new THREE.Color(0.95, 0.12, 0.12), n.dead >= 1 ? 0.85 : 0.68);
  return col;
};

const pos = new Float32Array(N * 3);
const colArr = new Float32Array(N * 3);
const sizes = new Float32Array(N);
const degree = new Float32Array(N);
links.forEach(l => { degree[l.s] += l.w; degree[l.t] += l.w; });
nodes.forEach((n, i) => {
  pos[i*3] = (Math.random()-0.5) * 1600;
  pos[i*3+1] = (Math.random()-0.5) * 900;
  pos[i*3+2] = (Math.random()-0.5) * 1600;
  const c = colorOf(n);
  colArr[i*3] = c.r; colArr[i*3+1] = c.g; colArr[i*3+2] = c.b;
  sizes[i] = Math.min(15, 4.5 + Math.sqrt(degree[i]) * 1.4);
});

const pGeo = new THREE.BufferGeometry();
const alphaArr = new Float32Array(N).fill(1);
pGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
pGeo.setAttribute("color", new THREE.BufferAttribute(colArr, 3));
pGeo.setAttribute("psize", new THREE.BufferAttribute(sizes, 1));
pGeo.setAttribute("aalpha", new THREE.BufferAttribute(alphaArr, 1));
const pMat = new THREE.ShaderMaterial({
  transparent: true, depthWrite: false, vertexColors: true,
  vertexShader: `
    attribute float psize; attribute float aalpha;
    varying vec3 vColor; varying float vA;
    void main(){ vColor = color; vA = aalpha;
      vec4 mv = modelViewMatrix * vec4(position,1.0);
      gl_PointSize = psize * (900.0 / -mv.z);
      gl_Position = projectionMatrix * mv; }`,
  fragmentShader: `
    varying vec3 vColor; varying float vA;
    void main(){ float d = length(gl_PointCoord - 0.5);
      float a = smoothstep(0.5, 0.18, d) * vA;
      gl_FragColor = vec4((vColor + vec3(0.25)) * a, a); }`,
});
scene.add(new THREE.Points(pGeo, pMat));

// edges
const MAXL = links.length;
const ePos = new Float32Array(MAXL * 6);
const eCol = new Float32Array(MAXL * 6);
const eColBase = new Float32Array(MAXL * 6);
links.forEach((l, i) => {
  const c1 = new THREE.Color(colArr[l.s*3], colArr[l.s*3+1], colArr[l.s*3+2]);
  const c2 = new THREE.Color(colArr[l.t*3], colArr[l.t*3+1], colArr[l.t*3+2]);
  eCol.set([c1.r,c1.g,c1.b, c2.r,c2.g,c2.b], i*6);
});
eColBase.set(eCol);
const eGeo = new THREE.BufferGeometry();
eGeo.setAttribute("position", new THREE.BufferAttribute(ePos, 3));
eGeo.setAttribute("color", new THREE.BufferAttribute(eCol, 3));
eGeo.setDrawRange(0, MAXL * 2);
function syncEdgePos() {
  links.forEach((l, i) => {
    const s = l.s * 3, t = l.t * 3, o = i * 6;
    ePos[o]   = pos[s];   ePos[o+1] = pos[s+1]; ePos[o+2] = pos[s+2];
    ePos[o+3] = pos[t];   ePos[o+4] = pos[t+1]; ePos[o+5] = pos[t+2];
  });
  eGeo.attributes.position.needsUpdate = true;
}
syncEdgePos();
const eMat = new THREE.LineBasicMaterial({ vertexColors:true, transparent:true,
  opacity:0.7, blending:THREE.AdditiveBlending, depthWrite:false });
scene.add(new THREE.LineSegments(eGeo, eMat));

// ---- force layout ------------------------------------------------------------
const vel = new Float32Array(N * 3);
let alpha = 1.0, settled = 0;
function step() {
  alpha *= 0.995;
  // pairwise repulsion (grid-bucketed would be faster; N is small)
  for (let i = 0; i < N; i++) for (let j = i+1; j < N; j++) {
    let dx = pos[j*3]-pos[i*3], dy = pos[j*3+1]-pos[i*3+1], dz = pos[j*3+2]-pos[i*3+2];
    let d2 = dx*dx + dy*dy + dz*dz + 1.0;
    if (d2 > 2500000) continue;
    let f = 52000 / d2;
    dx *= f/d2; dy *= f/d2; dz *= f/d2;
    vel[i*3] -= dx; vel[i*3+1] -= dy; vel[i*3+2] -= dz;
    vel[j*3] += dx; vel[j*3+1] += dy; vel[j*3+2] += dz;
  }
  // springs
  links.forEach(l => {
    let dx = pos[l.t*3]-pos[l.s*3], dy = pos[l.t*3+1]-pos[l.s*3+1], dz = pos[l.t*3+2]-pos[l.s*3+2];
    let d = Math.sqrt(dx*dx+dy*dy+dz*dz) + 0.01;
    let rest = 190 / (1 + l.w * 0.35);
    let f = (d - rest) / d * 0.008 * Math.min(3, 1 + l.w*0.2);
    dx *= f; dy *= f; dz *= f;
    vel[l.s*3] += dx; vel[l.s*3+1] += dy; vel[l.s*3+2] += dz;
    vel[l.t*3] -= dx; vel[l.t*3+1] -= dy; vel[l.t*3+2] -= dz;
  });
  // cluster gravity: pull members toward their cluster centroid (keeps
  // subsystems visibly separated even when weak inter-cluster links chain)
  const ccx = {}, ccy = {}, ccz = {}, ccn = {};
  for (let i = 0; i < N; i++) {
    const c = nodes[i].cluster;
    ccx[c] = (ccx[c]||0) + pos[i*3]; ccy[c] = (ccy[c]||0) + pos[i*3+1]; ccz[c] = (ccz[c]||0) + pos[i*3+2]; ccn[c] = (ccn[c]||0) + 1;
  }
  for (const c in ccx) { ccx[c] /= ccn[c]; ccy[c] /= ccn[c]; ccz[c] /= ccn[c]; }
  for (let i = 0; i < N; i++) {
    const c = nodes[i].cluster;
    vel[i*3]   += (ccx[c] - pos[i*3])   * 0.006;
    vel[i*3+1] += (ccy[c] - pos[i*3+1]) * 0.006;
    vel[i*3+2] += (ccz[c] - pos[i*3+2]) * 0.006;
  }
  // integrate + center gravity + damping
  for (let i = 0; i < N; i++) {
    vel[i*3] -= pos[i*3] * 0.0012; vel[i*3+1] -= pos[i*3+1] * 0.0012; vel[i*3+2] -= pos[i*3+2] * 0.0012;
    pos[i*3] += vel[i*3] * alpha; pos[i*3+1] += vel[i*3+1] * alpha; pos[i*3+2] += vel[i*3+2] * alpha;
    vel[i*3] *= 0.86; vel[i*3+1] *= 0.86; vel[i*3+2] *= 0.86;
  }
}
let running = true;
window.__dbg = { pos, nodes, links, syncEdgePos, renderer: null, camera: null, THREE, alpha: alphaArr };
function tick() {
  if (window.__dbg) { window.__dbg.renderer = renderer; window.__dbg.camera = camera; }
  if (running) {
    for (let s = 0; s < 2; s++) step();
    syncEdgePos();
    pGeo.attributes.position.needsUpdate = true;
    eGeo.attributes.position.needsUpdate = true;
    if (++settled > 900) { running = false; frameGraph(); }
  }
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}

// frame camera on the graph centroid + bounding radius
function graphBounds() {
  const c = new THREE.Vector3(), lo = new THREE.Vector3(1e9,1e9,1e9), hi = new THREE.Vector3(-1e9,-1e9,-1e9);
  for (let i = 0; i < N; i++) {
    const p = new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2]);
    c.add(p); lo.min(p); hi.max(p);
  }
  c.divideScalar(N || 1);
  return { center: c, radius: lo.distanceTo(hi) * 0.5 };
}
function frameGraph() {
  const b = graphBounds();
  const dir = new THREE.Vector3(camera.position.x - controls.target.x,
    camera.position.y - controls.target.y,
    camera.position.z - controls.target.z);
  if (dir.lengthSq() < 1) dir.set(0, 0, 1);
  dir.normalize();
  camera.position.copy(b.center).addScaledVector(dir, Math.max(420, b.radius * 1.45));
  controls.target.copy(b.center);
}

// ---- UI ---------------------------------------------------------------------
const stats = document.getElementById("stats");
const m = DATA.meta;
stats.textContent = `${m.files} files · ${m.edges} links · ${m.clusters} clusters · dead ${m.deadLikely}+${m.deadReview}`;

const tip = document.getElementById("tip");
const raycaster = new THREE.Raycaster();
raycaster.params.Points.threshold = 14;
const mouse = new THREE.Vector2();
let hovered = -1, selected = -1, activeCluster = null, deadOnly = false, query = "";

function nodeVisible(n) {
  if (deadOnly && n.dead <= 0) return false;
  if (activeCluster !== null && n.cluster !== activeCluster) return false;
  if (query && !(n.path.toLowerCase().includes(query) || n.cls.toLowerCase().includes(query))) return false;
  return true;
}
function applyVisibility() {
  for (let i = 0; i < N; i++) {
    const vis = nodeVisible(nodes[i]);
    alphaArr[i] = vis ? 1 : 0.02;
    if (vis) {
      const c = colorOf(nodes[i]);
      colArr[i*3] = c.r; colArr[i*3+1] = c.g; colArr[i*3+2] = c.b;
    }
  }
  pGeo.attributes.color.needsUpdate = true;
  pGeo.attributes.aalpha.needsUpdate = true;
  // dim edges whose endpoints are hidden
  links.forEach((l, i) => {
    const bothVis = alphaArr[l.s] > 0.5 && alphaArr[l.t] > 0.5;
    const k = bothVis ? 1 : 0.012;
    const o = i * 6;
    for (let c = 0; c < 6; c++) eCol[o + c] = eColBase[o + c] * k;
  });
  eGeo.attributes.color.needsUpdate = true;
}

const legend = document.getElementById("legend");
const topClusters = Object.entries(
  nodes.reduce((acc, n) => { if (n.cluster >= 0) acc[n.cluster] = (acc[n.cluster]||0)+1; return acc; }, {})
).sort((a,b) => b[1]-a[1]).slice(0, 14);
topClusters.forEach(([cid, count]) => {
  const c = new THREE.Color().setHSL(hue(+cid), 0.72, 0.58);
  const chip = document.createElement("span");
  chip.className = "chip";
  chip.style.background = `#${c.getHexString()}22`;
  chip.style.color = `#${c.getHexString()}`;
  chip.textContent = `c${cid} · ${count}`;
  chip.onclick = () => {
    activeCluster = activeCluster === +cid ? null : +cid;
    document.querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    if (activeCluster !== null) chip.classList.add("on");
    applyVisibility();
  };
  legend.appendChild(chip);
});

document.getElementById("bDead").onclick = e => {
  deadOnly = !deadOnly;
  e.target.classList.toggle("on", deadOnly);
  applyVisibility();
};
document.getElementById("bReset").onclick = () => {
  camera.position.set(0, 0, 1400); controls.target.set(0,0,0); frameGraph();
  activeCluster = null; deadOnly = false; query = "";
  document.getElementById("search").value = "";
  document.querySelectorAll(".chip, button").forEach(x => x.classList.remove("on"));
  applyVisibility();
};
document.getElementById("search").oninput = e => {
  query = e.target.value.toLowerCase();
  applyVisibility();
};

const info = document.getElementById("info");
function showInfo(i) {
  const n = nodes[i];
  selected = i;
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
  if (n.cluster >= 0) mk("cluster c" + n.cluster, `#${new THREE.Color().setHSL(hue(n.cluster),0.72,0.58).getHexString()}`);
  if (n.dead > 0) mk(n.dead >= 1 ? "likely dead" : "maybe dead", "#ef5350");
  const ul = document.getElementById("iLinks");
  ul.innerHTML = "";
  const nb = new Map();
  links.forEach(l => {
    if (l.s === i) nb.set(l.t, (nb.get(l.t)||0) + l.w);
    if (l.t === i) nb.set(l.s, (nb.get(l.s)||0) + l.w);
  });
  [...nb.entries()].sort((a,b) => b[1]-a[1]).slice(0, 20).forEach(([j, w]) => {
    const li = document.createElement("li");
    li.textContent = `${nodes[j].label} ×${w}`;
    li.onclick = () => { showInfo(j); focus(j); };
    ul.appendChild(li);
  });
}
function focus(i) {
  controls.target.set(pos[i*3], pos[i*3+1], pos[i*3+2]);
  const d = 320;
  const dir = new THREE.Vector3(camera.position.x - controls.target.x,
    camera.position.y - controls.target.y, camera.position.z - controls.target.z).normalize();
  camera.position.copy(controls.target).addScaledVector(dir, d);
}

renderer.domElement.addEventListener("pointermove", e => {
  mouse.x = (e.clientX/innerWidth)*2-1; mouse.y = -(e.clientY/innerHeight)*2+1;
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObject(scene.children[0]);
  hovered = hits.length ? hits[0].index : -1;
  if (hovered >= 0) {
    tip.style.display = "block";
    tip.style.left = (e.clientX+14)+"px"; tip.style.top = (e.clientY+14)+"px";
    tip.textContent = nodes[hovered].path;
    renderer.domElement.style.cursor = "pointer";
  } else { tip.style.display = "none"; renderer.domElement.style.cursor = "default"; }
});
renderer.domElement.addEventListener("click", () => { if (hovered >= 0) showInfo(hovered); });
addEventListener("resize", () => {
  camera.aspect = innerWidth/innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

tick();
</script>
</body>
</html>
"""


def generate(out: str | Path | None = None) -> Path:
    out = Path(out) if out else Path(__file__).resolve().parent / "graph.html"
    data = _build_data()
    html = _TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    path = generate(sys.argv[1] if len(sys.argv) > 1 else None)
    print(path)
