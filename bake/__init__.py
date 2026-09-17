"""bake/ — pure per-job transforms for the viz DATA pipeline (issue #86
phase-2; #299 B finished the split). viz._build_data stays the single
entry: it owns the stage order and threads results through these
leaves. budget: _cap_rows byte-budget keeper  gitinfo: head/churn stamps
files_model: J1-J4  wires: J5-J8  overlays: J13/J14/J17  fnio: J15-J16
embeddings: J9 kNN + the ONE chroma fetch (the only nav store edge; the
store-index space derives exactly once there — #299 C)
semantics: J11 cluster matrix + J10 supergroups + #279 semAff rows
(J12/J18 ride the orchestrator: layout loud-abort + DATA assembly.)"""
