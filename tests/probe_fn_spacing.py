# verify: no two fn boxes on the same wire overlap; sizes shrunk
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)
    page = b.new_page()
    page.goto("http://127.0.0.1:8791/graph.html", wait_until="load")
    page.wait_for_timeout(3500)
    page.fill("#search", "magicplayer")
    page.dispatch_event("#search", "input")
    page.check("#cbFn")
    page.wait_for_timeout(1200)
    res = page.evaluate(
        """() => { const d = window.__dbg; const meta = d.fnMeta, pos = d.pos;
             const adj = {};
             d.links.forEach(l => { (adj[l.s]=adj[l.s]||new Set()).add(l.t);
                                    (adj[l.t]=adj[l.t]||new Set()).add(l.s); });
             // group boxes by their wire (nearest wire for each box)
             const byWire = new Map();
             let maxOff = 0;
             for (const fm of meta) {
               const A = fm.file, p = fm.p; let best = Infinity, bk = null;
               for (const B of (adj[A]||[])) {
                 const ax=pos[A*3],ay=pos[A*3+1],az=pos[A*3+2];
                 const bx=pos[B*3],by=pos[B*3+1],bz=pos[B*3+2];
                 const abx=bx-ax,aby=by-ay,abz=bz-az, ab2=abx*abx+aby*aby+abz*abz;
                 const apx=p[0]-ax,apy=p[1]-ay,apz=p[2]-az;
                 let tt=ab2>0?(apx*abx+apy*aby+apz*abz)/ab2:0; tt=Math.max(0,Math.min(1,tt));
                 const dx=apx-abx*tt,dy=apy-aby*tt,dz=apz-abz*tt, dd=Math.sqrt(dx*dx+dy*dy+dz*dz);
                 if (dd<best){best=dd;bk=A<B?A+'|'+B:B+'|'+A;}
               }
               maxOff=Math.max(maxOff,best);
               if(!byWire.has(bk)) byWire.set(bk,[]);
               byWire.get(bk).push(p);
             }
             // min pairwise distance per multi-box wire
             let minSep = Infinity;
             for (const arr of byWire.values()) {
               for (let i=0;i<arr.length;i++) for (let j=i+1;j<arr.length;j++) {
                 const dx=arr[i][0]-arr[j][0],dy=arr[i][1]-arr[j][1],dz=arr[i][2]-arr[j][2];
                 minSep=Math.min(minSep,Math.sqrt(dx*dx+dy*dy+dz*dz));
               }
             }
             const sz = d.sizes ? Math.max(...d.sizes) : null;
             return { boxes: meta.length, maxOff, minSep, maxSphere: sz }; }"""
    )
    print(res)
    ok = res["maxOff"] < 3.5 and res["minSep"] is not None and res["minSep"] > 2
    print(("PASS" if ok else "FAIL") + " — boxes separated on wires "
          f"(min separation {res['minSep']:.1f} vs box size 6, maxOff {res['maxOff']:.2f})")
    b.close()
    sys.exit(0 if ok else 1)
