"""Cluster labeling + mega-blob splitting for nav.clusters().

Imported lazily by nav.clusters() so module import stays cheap. Works on
paths + class names + embeddings only — no language parsing, so a second
language's files label the same way.

Label cascade (first confident hit wins):
  1. AUTOLOAD   member is an autoload backing file       conf 1.0
  2. DIR        >=55% share a non-generic dir segment    conf 0.85
  3. SCENE STEM scene-dominant + >=50% share a stem      conf 0.8
  4. c-TF-IDF   top discriminative identifier tokens     conf 0.6
  5. FALLBACK   centroid member's class_name / Mixed(N)  conf 0.4/0.2

Blob split (Qwen3-0.6B over-merge artifact, e.g. a 238-file mega-cluster):
  dir shortcut (>=60% one dir seg -> group by subdirectory) else
  AgglomerativeClustering(cosine, average, sim>=0.65); subclusters <3
  members merge into the nearest centroid (cosine>=0.55) or a Misc bucket;
  subclusters >60 members recurse once at sim 0.70 (max depth 2).
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import nav

GENERIC_DIRS = {
    "scripts", "src", "core", "code", "main", "game", "utils", "helpers",
    "common", "shared", "autoload", "singletons", "resources", "assets",
    "scenes",
}

# compact english stopword set — no external dep, deterministic
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when",
    "at", "by", "for", "with", "about", "into", "through", "during",
    "before", "after", "to", "from", "up", "down", "in", "out", "on",
    "off", "over", "under", "again", "further", "once", "here", "there",
    "all", "any", "both", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than",
    "too", "very", "can", "will", "just", "should", "now", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "do",
    "does", "did", "of", "it", "its", "this", "that", "these", "those",
    "as", "while", "because", "until", "against", "between", "them",
    "they", "their", "you", "your", "we", "our",
}

BLOB_MIN = 60          # clusters larger than this get split
SMALL_MIN = 3          # subclusters smaller than this merge / go Misc
MERGE_SIM = 0.55       # centroid cosine needed to merge a small subcluster
SPLIT_SIM = 0.65       # default agglomerative similarity floor (dist 0.35)
RECURSE_SIM = 0.70     # second-pass floor for still-big subclusters (0.30)
MAX_DEPTH = 2

TOKEN_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")


def titleize(seg: str) -> str:
    return re.sub(r"[\s_-]+", " ", seg).strip().title()


def tokenize_ident(s: str) -> list[str]:
    s = s.replace("_", " ").replace("-", " ")
    s = TOKEN_CAMEL_RE.sub(r"\1 \2", s)
    return [t.lower() for t in s.split()]


def _ok_token(t: str) -> bool:
    return len(t) >= 3 and t not in STOPWORDS and t not in GENERIC_DIRS and not t.isdigit()


def dir_segments(path: str) -> list[str]:
    """Non-generic directory segments of a repo-relative posix path."""
    parts = path.split("/")[:-1]  # drop the file name
    return [p for p in parts if p.lower() not in GENERIC_DIRS]


def _stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0]


def _autoload_map() -> dict[str, str]:
    """rel path -> autoload singleton name (inverse of project.godot)."""
    pg = nav.ROOT / "project.godot"
    out: dict[str, str] = {}
    if not pg.is_file():
        return out
    in_auto = False
    for line in nav._read_text(pg).splitlines():
        s = line.strip()
        if s.startswith("[autoload]"):
            in_auto = True
            continue
        if s.startswith("["):
            in_auto = False
        if in_auto:
            m = re.match(r'^(\w+)\s*=\s*"\*?res://([\w/.-]+\.\w+)"', s)
            if m:
                out[m.group(2)] = m.group(1)
    return out


@dataclass
class LabelContext:
    autoloads: dict[str, str]
    df: Counter                    # token -> number of clusters containing it
    k: int                         # cluster count
    centroid_path: dict[int, str] = field(default_factory=dict)


def _doc_tokens(paths: list[tuple[str, str]]) -> list[str]:
    toks: list[str] = []
    for p, cls in paths:
        toks.extend(t for t in tokenize_ident(_stem(p)) if _ok_token(t))
        if cls:
            toks.extend(t for t in tokenize_ident(cls) if _ok_token(t))  # x2 weight
    return toks


def tfidf_terms(paths: list[tuple[str, str]], ctx: LabelContext, n: int = 3) -> list[str]:
    """c-TF-IDF: (freq_in_cluster / total_tokens) * log(1 + K / df)."""
    toks = _doc_tokens(paths)
    if not toks:
        return []
    total = len(toks)
    freq = Counter(toks)
    scored = []
    for t, f in freq.items():
        df = max(1, ctx.df.get(t, 0))
        scored.append((f / total * math.log(1 + ctx.k / df), t))
    scored.sort(key=lambda st: (-st[0], st[1]))  # score desc, alpha tie-break
    return [t for _, t in scored[:n]]


def label_cluster(
    members: list[tuple[str, str]], ctx: LabelContext, cid: int = -1
) -> tuple[str, float, str]:
    ordered = sorted(members)
    # 1. autoload backing file
    for p, _ in ordered:
        if p in ctx.autoloads:
            return ctx.autoloads[p], 1.0, "autoload"
    n = len(members) or 1
    # 2. dominant non-generic dir segment
    seg_counts: Counter = Counter()
    for p, _ in members:
        for seg in set(dir_segments(p)):
            seg_counts[seg] += 1
    if seg_counts:
        seg, cnt = sorted(seg_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if cnt / n >= 0.55:
            return titleize(seg), 0.85, "dir"
    # 3. scene stem
    scene = [p for p, _ in members if p.endswith(".tscn")]
    if len(scene) * 2 > len(members):
        stems = Counter(_stem(p) for p in scene)
        stem, cnt = sorted(stems.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if cnt / n >= 0.5:
            return titleize(stem), 0.8, "scene"
    # 4. c-TF-IDF terms
    terms = tfidf_terms(members, ctx, n=3)
    if terms:
        return " - ".join(terms), 0.6, "tfidf"
    # 5. centroid member's class_name, else Mixed
    cp = ctx.centroid_path.get(cid, "")
    for p, cls in ordered:
        if p == cp and cls:
            return titleize(cls), 0.4, "class"
    for _, cls in ordered:
        if cls:
            return titleize(cls), 0.35, "class"
    return f"Mixed ({len(members)})", 0.2, "mixed"


def _dedupe_label(label: str, used: set[str], paths: list[tuple[str, str]], ctx: LabelContext) -> str:
    if label not in used:
        return label
    for extra in tfidf_terms(paths, ctx, n=8):
        cand = f"{label} {extra}"
        if cand not in used:
            return cand
    for seg in dir_segments(paths[0][0]):
        cand = f"{label} {titleize(seg)}"
        if cand not in used:
            return cand
    i = 2
    while f"{label} {i}" in used:
        i += 1
    return f"{label} {i}"


def _vec(rows: dict[str, int], mat, path: str):
    i = rows.get(path)
    return None if i is None else mat[i]


def _centroid_path(paths: list[tuple[str, str]], rows: dict[str, int], mat) -> str:
    import numpy as np

    vecs = [mat[rows[p]] for p, _ in paths if p in rows]
    if not vecs:
        return paths[0][0]
    mean = np.mean(vecs, axis=0)
    mean /= max(float(np.linalg.norm(mean)), 1e-9)
    best, best_sim = paths[0][0], -2.0
    for p, _ in paths:
        v = _vec(rows, mat, p)
        if v is None:
            continue
        sim = float(np.dot(v, mean))
        if sim > best_sim or (sim == best_sim and p < best):
            best, best_sim = p, sim
    return best


def _split_cluster(
    cluster: dict, rows: dict[str, int], mat, sim: float, depth: int
) -> list[dict]:
    paths = [p for p, _ in cluster["paths"]]
    # 1. one dir segment covering >=60% -> group by subdirectory, done
    seg_counts: Counter = Counter()
    for p in paths:
        for seg in set(dir_segments(p)):
            seg_counts[seg] += 1
    groups: dict[str, list[int]] = defaultdict(list)
    if seg_counts:
        seg, cnt = sorted(seg_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if cnt / len(paths) >= 0.60:
            for i, p in enumerate(paths):
                groups["/".join(p.split("/")[:-1])].append(i)
        else:
            groups = defaultdict(list)
    if not groups:
        # 2. agglomerative over the blob's embeddings only
        from sklearn.cluster import AgglomerativeClustering

        idxs = [rows[p] for p in paths if p in rows]
        if len(idxs) < len(paths):
            return [dict(cluster)]  # embeddings missing — leave untouched
        labels = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=1.0 - sim,
            metric="cosine",
            linkage="average",
        ).fit_predict(mat[idxs])
        for pos, lab in enumerate(labels):
            groups[int(lab)].append(pos)
    out: list[dict] = []
    for _, member_pos in sorted(groups.items(), key=lambda kv: (-len(kv[1]), paths[kv[1][0]])):
        sub_paths = [cluster["paths"][i] for i in sorted(member_pos)]
        sub = {"paths": sub_paths, "size": len(sub_paths)}
        if depth < MAX_DEPTH and sub["size"] > BLOB_MIN:
            out.extend(_split_cluster(sub, rows, mat, RECURSE_SIM, depth + 1))
        else:
            out.append(sub)
    return out or [dict(cluster)]


def _merge_small(subs: list[dict], rows: dict[str, int], mat) -> list[dict]:
    import numpy as np

    big = [s for s in subs if s["size"] >= SMALL_MIN]
    small = sorted(
        (s for s in subs if s["size"] < SMALL_MIN),
        key=lambda s: s["paths"][0][0],
    )

    def centroid(s: dict):
        vecs = [mat[rows[p]] for p, _ in s["paths"] if p in rows]
        return None if not vecs else np.mean(vecs, axis=0)

    misc: list[tuple[str, str]] = []
    for s in small:
        c = centroid(s)
        best, best_sim = None, -2.0
        for b in big:
            bc = centroid(b)
            if bc is None or c is None:
                continue
            cos = float(np.dot(c, bc) / (np.linalg.norm(c) * np.linalg.norm(bc) + 1e-12))
            if cos > best_sim:
                best, best_sim = b, cos
        if best is not None and best_sim >= MERGE_SIM:
            best["paths"] = sorted(best["paths"] + s["paths"])
            best["size"] = len(best["paths"])
        else:
            misc.extend(s["paths"])
    if misc:
        big.append({"paths": sorted(misc), "size": len(misc)})
    return big


def finalize(
    raw: list[dict], ids: list[str], mat, split_sim: float = SPLIT_SIM, blob_min: int = BLOB_MIN
) -> list[dict]:
    """Split mega-blobs, merge tiny subclusters, label everything.

    raw: [{id, size, paths: [(path, class_name)]}] from nav's union-find.
    Returns same dicts plus label / confidence / method per cluster.
    """
    rows = {p: i for i, p in enumerate(ids)}
    parts: list[dict] = []
    for c in raw:
        if c["size"] > blob_min:
            parts.extend(_split_cluster(c, rows, mat, split_sim, depth=1))
        else:
            parts.append({"paths": list(c["paths"]), "size": c["size"]})
    parts = _merge_small(parts, rows, mat)
    parts.sort(key=lambda c: (-c["size"], c["paths"][0][0] if c["paths"] else ""))
    for i, c in enumerate(parts):
        c["id"] = i

    df: Counter = Counter()
    for c in parts:
        for t in set(_doc_tokens(c["paths"])):
            df[t] += 1
    ctx = LabelContext(
        autoloads=_autoload_map(),
        df=df,
        k=len(parts),
        centroid_path={c["id"]: _centroid_path(c["paths"], rows, mat) for c in parts},
    )
    used: set[str] = set()
    for c in parts:  # size-desc: bigger clusters win the clean label
        label, conf, method = label_cluster(c["paths"], ctx, c["id"])
        label = _dedupe_label(label, used, c["paths"], ctx)
        used.add(label)
        c["label"], c["confidence"], c["method"] = label, conf, method
    return parts


def coarse_groups(fine: list[dict], ids: list[str], mat, cut: float = 0.45) -> list[dict]:
    """Optional coarse level: scipy average-linkage over cluster centroids
    (cosine), cut at `cut` distance, nudged into 3..8 groups. Named by
    dominant dir (>=60%) else c-TF-IDF union."""
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage

    rows = {p: i for i, p in enumerate(ids)}
    cents = []
    for c in fine:
        vecs = [mat[rows[p]] for p, _ in c["paths"] if p in rows]
        v = np.mean(vecs, axis=0) if vecs else np.zeros(mat.shape[1])
        v = v / max(float(np.linalg.norm(v)), 1e-9)
        cents.append(v)
    z = linkage(np.array(cents), method="average", metric="cosine")
    t = cut
    labels = fcluster(z, t=t, criterion="distance")
    # higher cut distance = more merging = fewer groups
    while len(set(labels)) > 8 and t < 0.95:
        t += 0.05
        labels = fcluster(z, t=t, criterion="distance")
    while len(set(labels)) < 3 and t > 0.05:
        t -= 0.05
        labels = fcluster(z, t=t, criterion="distance")
    by_group: dict[int, list[int]] = defaultdict(list)
    for ci, lab in enumerate(labels):
        by_group[int(lab)].append(ci)
    # name by dominant dir else tfidf union
    df: Counter = Counter()
    for c in fine:
        for tok in set(_doc_tokens(c["paths"])):
            df[tok] += 1
    ctx = LabelContext(autoloads=_autoload_map(), df=df, k=len(fine))
    out = []
    for lab, cis in sorted(by_group.items()):
        paths = [p for ci in cis for p in fine[ci]["paths"]]
        name = ""
        segs: Counter = Counter()
        for p, _ in paths:
            for seg in set(dir_segments(p)):
                segs[seg] += 1
        if segs:
            seg, cnt = sorted(segs.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if cnt / len(paths) >= 0.60:
                name = titleize(seg)
        if not name:
            terms = tfidf_terms(paths, ctx, n=3)
            name = " - ".join(terms) if terms else "Mixed"
        out.append({"id": len(out), "label": name, "cluster_ids": cis, "size": len(paths)})
    return out
