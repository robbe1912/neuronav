"""vizjs/ — the graph.html template as 17 verbatim sections.

Issues #86 phase-3 carved viz.py's monolithic template into ordered
sections; #299 A moves them out of viz.py one module per section. The
JOIN ORDER below is a load-bearing contract, not a convention:

- every section is module-scope JavaScript that must see the bindings
  of all EARLIER sections (TDZ discipline — a reorder is a boot-time
  ReferenceError, the #98 class), and
- dbg is last by design: __dbg captures everything initialized before
  it.

template() concatenates the sections in rung order at generate() time;
the join text is byte-identical to the former viz.py ``_TEMPLATE``
expression, so a bake over the same DATA is byte-identical (the
#279/#124 byte law — test_viz pins it across two bakes).

Rung order (= join order):
  1 head           _HTML_HEAD         HTML/CSS document head + UI chrome markup
  2 core           _JS_CORE           boot, scene/camera/renderer, shared JS leaves, sprite pass
  3 edges          _JS_EDGES          edge buckets, highway arcs, wire picker
  4 tick           _JS_TICK           the render tick (pure function of camera pose + build data)
  5 labels3d       _JS_LABELS3D       3D label planes + UI helpers
  6 focus_vis      _JS_FOCUS_VIS      focus entry, budget wires, compaction
  7 labels3d_tail  _JS_LABELS3D_TAIL  label solver tail (depth-tier label sets)
  8 fn_layer       _JS_FN_LAYER       fn-layer state block (fnMesh..rebuildFnLayer-1)
  9 fn_layer_b     _JS_FN_LAYER_B     rebuildFnLayer — the fn/graph bus build
 10 legend         _JS_LEGEND         legend chips (clusters + supergroups)
 11 pins           _JS_PINS           wire/node pins (latch + menu)
 12 map_render     _JS_MAP_RENDER     2D map pane layout + build
 13 map_paint      _JS_MAP_PAINT      map pane paint loop
 14 map_input      _JS_MAP_INPUT      divider drag + map input
 15 panel          _JS_PANEL          search panel + info card
 16 events         _JS_EVENTS         pointer/keyboard events, resize3D
 17 dbg            _JS_DBG            __dbg debug handle (always last: captures everything)

Carve history from the viz.py era (kept for archaeology; rungs 1-8 of
issue #86): MAP_RENDER+MAP_PAINT carved from _JS_MID (rung 2),
MAP_INPUT+LEGEND (rung 3), _JS_MINS_C renamed _JS_PINS (rung 4),
FN_LAYER state block (rung 5), FN_LAYER_B = the rebuildFnLayer block
(rung 6), LABELS3D + FOCUS_VIS + LABELS3D_TAIL (rung 7, two L3D
constants around FOCUS_VIS — join order == original text order),
EDGES+TICK+PANEL+EVENTS (rung 8 final: the 17-constant join).
"""

from vizjs.head import _HTML_HEAD
from vizjs.core import _JS_CORE
from vizjs.edges import _JS_EDGES
from vizjs.tick import _JS_TICK
from vizjs.labels3d import _JS_LABELS3D
from vizjs.focus_vis import _JS_FOCUS_VIS
from vizjs.labels3d_tail import _JS_LABELS3D_TAIL
from vizjs.fn_layer import _JS_FN_LAYER
from vizjs.fn_layer_b import _JS_FN_LAYER_B
from vizjs.legend import _JS_LEGEND
from vizjs.pins import _JS_PINS
from vizjs.map_render import _JS_MAP_RENDER
from vizjs.map_paint import _JS_MAP_PAINT
from vizjs.map_input import _JS_MAP_INPUT
from vizjs.panel import _JS_PANEL
from vizjs.events import _JS_EVENTS
from vizjs.dbg import _JS_DBG

_ORDER = (_HTML_HEAD,
          _JS_CORE,
          _JS_EDGES,
          _JS_TICK,
          _JS_LABELS3D,
          _JS_FOCUS_VIS,
          _JS_LABELS3D_TAIL,
          _JS_FN_LAYER,
          _JS_FN_LAYER_B,
          _JS_LEGEND,
          _JS_PINS,
          _JS_MAP_RENDER,
          _JS_MAP_PAINT,
          _JS_MAP_INPUT,
          _JS_PANEL,
          _JS_EVENTS,
          _JS_DBG)


def template() -> str:
    """The 17-section join in rung order — viz.generate() calls this at
    bake time (#299 A); viz.py itself stays wiring-only."""
    return "".join(_ORDER)
