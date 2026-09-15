# Pluggable embed providers (issue #17) — provider selection, protocol
# adapters, auth injection, batching, 429 retry, loud failures, fake-mode
# isolation. Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_embedprov.py
# Hermetic: loopback stub server (stdlib http.server, ephemeral port)
# speaking both the Ollama /api/embed and OpenAI /v1/embeddings wire
# shapes; temp config + state dir — never the real index or .neuronav.
import gzip
import io
import json
import os
import sys
import tempfile
import threading
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP = Path(tempfile.mkdtemp(prefix="nav-embedprov-"))
SRV: HTTPServer
CAPTURED: list[dict] = []
MODE = {"protocol": "ollama", "require_auth": None, "retry_429": 0, "short_by": 0, "shuffle": False}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        auth = self.headers.get("Authorization")
        CAPTURED.append({"path": self.path, "auth": auth, "body": body})
        if MODE["require_auth"] is not None and auth != MODE["require_auth"]:
            out = b'{"error": {"message": "missing api key"}}'
            self.send_response(401)
        elif MODE["retry_429"] > 0:
            MODE["retry_429"] -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            out = b"{}"
        else:
            texts = body["input"]
            if MODE["protocol"] == "ollama":
                if MODE.get("hash_vecs"):  # #220 leg: text-deterministic
                    import hashlib

                    rows = []
                    for t in texts:
                        h = hashlib.sha256(f"stub:{t}".encode()).digest()
                        rows.append([(b / 255.0) * 2 - 1 for b in h])
                else:
                    rows = [[float(i), float(i), float(i)] for i in range(len(texts))]
                payload = {"model": body.get("model", ""), "embeddings": rows}
            else:
                order = list(range(len(texts)))
                if MODE["shuffle"]:
                    order.reverse()
                payload = {"object": "list", "model": body.get("model", ""),
                           "data": [{"object": "embedding", "index": i,
                                     "embedding": [float(i), 1.0, 2.0]} for i in order]}
            if MODE["short_by"]:
                payload["embeddings" if MODE["protocol"] == "ollama" else "data"] = (
                    payload.get("embeddings") or payload.get("data"))[:-MODE["short_by"]]
            out = json.dumps(payload).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def start_stub() -> int:
    global SRV
    SRV = HTTPServer(("127.0.0.1", 0), Stub)  # ephemeral loopback port
    threading.Thread(target=SRV.serve_forever, daemon=True).start()
    return SRV.server_address[1]


PORT = start_stub()
os.environ["NEURONAV_CONFIG"] = str(TMP / "config.json")
os.environ.pop("NEURONAV_EMBED_FAKE", None)
os.environ.pop("NEURONAV_EMBED_KEY", None)

CFG = TMP / "config.json"


def write_cfg(**over):
    cfg = {"root": str(TMP), "state_dir": str(TMP / "state"), "collection": "embedprov",
           "embed_model": "test-model", "embed_dim": 3, **over}
    CFG.write_text(json.dumps(cfg), encoding="utf-8")
    import nav
    nav._apply_config(CFG)
    return nav


write_cfg()  # bootstrap: first nav import (inside) binds the temp config
import nav  # noqa: E402  (cached module from write_cfg's import)

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# --- provider selection -------------------------------------------------
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed")
check("no embed_provider + ollama url -> ollama", nav.EMBED_PROVIDER == "ollama", nav.EMBED_PROVIDER)
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/v1/embeddings")
check("auto-detect: url ends /embeddings -> openai", nav.EMBED_PROVIDER == "openai", nav.EMBED_PROVIDER)
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/v1/embeddings", embed_provider="ollama")
check("explicit provider beats url auto-detect", nav.EMBED_PROVIDER == "ollama", nav.EMBED_PROVIDER)
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_provider="OpenAI")
check("provider is case-insensitive", nav.EMBED_PROVIDER == "openai", nav.EMBED_PROVIDER)
try:
    write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_provider="bedrock")
    check("unknown provider fails loud", False, "no SystemExit")
except SystemExit as e:
    check("unknown provider fails loud", "bedrock" in str(e), str(e))

# --- protocol adapters --------------------------------------------------
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed")
vecs = nav.embed(["a", "b", "c"])
check("ollama adapter returns embeddings in input order",
      [v[0] for v in vecs] == [0.0, 1.0, 2.0], str(vecs))
check("request carries model + input",
      CAPTURED[-1]["body"] == {"model": "test-model", "input": ["a", "b", "c"]}, str(CAPTURED[-1]))

MODE["protocol"] = "openai"
MODE["shuffle"] = True
CAPTURED.clear()
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/v1/embeddings")
vecs = nav.embed(["a", "b", "c"])
check("openai adapter sorts shuffled data[] by index",
      [v[0] for v in vecs] == [0.0, 1.0, 2.0], str(vecs))
check("openai posts to the configured url", CAPTURED[0]["path"] == "/v1/embeddings", CAPTURED[0]["path"])
MODE["shuffle"] = False

# --- batching -----------------------------------------------------------
CAPTURED.clear()
nav.embed([f"t{i}" for i in range(40)])
check("40 texts chunk at EMBED_BATCH -> posts of 32 + 8",
      [len(c["body"]["input"]) for c in CAPTURED] == [32, 8],
      str([len(c["body"]["input"]) for c in CAPTURED]))

# --- auth ---------------------------------------------------------------
os.environ["NEURONAV_EMBED_KEY"] = "sk-env-secret"
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/v1/embeddings", embed_api_key="sk-cfg-secret")
nav.embed(["k"])
check("env key beats config key", CAPTURED[-1]["auth"] == "Bearer sk-env-secret", str(CAPTURED[-1]["auth"]))
del os.environ["NEURONAV_EMBED_KEY"]
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/v1/embeddings", embed_api_key="sk-cfg-secret")
nav.embed(["k"])
check("config key used when env unset", CAPTURED[-1]["auth"] == "Bearer sk-cfg-secret", str(CAPTURED[-1]["auth"]))
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/v1/embeddings")
nav.embed(["k"])
check("keyless request carries no Authorization header", CAPTURED[-1]["auth"] is None, str(CAPTURED[-1]["auth"]))

MODE["require_auth"] = "Bearer sk-needed"
try:
    nav.embed(["needs", "key"])
    check("missing key against authed endpoint fails loud", False, "no exception")
except Exception as e:
    check("missing key against authed endpoint fails loud",
          getattr(e, "response", None) is not None and e.response.status_code == 401, f"{type(e).__name__}: {e}")
MODE["require_auth"] = None

# --- loud failures, never pad/truncate ----------------------------------
MODE["protocol"] = "ollama"
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed")
MODE["short_by"] = 1
try:
    nav.embed(["a", "b", "c"])
    check("short response raises, never pads", False, "no exception")
except RuntimeError as e:
    check("short response raises, never pads",
          "ollama" in str(e) and "2 embeddings" in str(e) and "3 inputs" in str(e), str(e))
MODE["short_by"] = 0
MODE["protocol"] = "openai"  # server answers OpenAI shape...
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/v1/embeddings")
nav.EMBED_PROVIDER = "ollama"  # ...but the client speaks Ollama
try:
    nav.embed(["x"])
    check("protocol mismatch fails loud, names provider", False, "no exception")
except RuntimeError as e:
    check("protocol mismatch fails loud, names provider", "ollama" in str(e), str(e))
finally:
    nav.EMBED_PROVIDER = "openai"

# --- fake mode isolated -------------------------------------------------
os.environ["NEURONAV_EMBED_FAKE"] = "1"
CAPTURED.clear()
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/v1/embeddings", embed_api_key="sk-ignored")
v1 = nav.embed(["fake", "mode"])
v2 = nav.embed(["fake", "mode"])
check("fake mode never touches the server", CAPTURED == [], f"{len(CAPTURED)} requests")
check("fake mode deterministic", v1 == v2 and len(v1) == 2, f"equal={v1 == v2}")
check("fake mode honors EMBED_DIM", all(len(v) == 3 for v in v1), str([len(v) for v in v1]))
del os.environ["NEURONAV_EMBED_FAKE"]

# --- collection fingerprint names provider ------------------------------
MODE["protocol"] = "ollama"
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-openai-era", embed_provider="openai")
col = nav._collection()
check("fresh collection records model + provider",
      (col.metadata or {}).get("embed_model") == "m-openai-era"
      and (col.metadata or {}).get("embed_provider") == "openai", str(col.metadata))
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-new", embed_provider="ollama")
try:
    nav._collection()
    check("model swap raises naming both providers", False, "no exception")
except RuntimeError as e:
    check("model swap raises naming both providers",
          "provider 'openai'" in str(e) and "provider 'ollama'" in str(e), str(e))


def rec_eq(a, b, atol=1e-6):
    """Records by id: documents/metadata exact; embeddings tolerate
    chroma's one-time f32 quantization settle on copy (<= 1 ulp, then
    bit-stable — chroma's get() returns off-grid f64s)."""
    if a.keys() != b.keys():
        return False
    for k, (e1, d1, m1) in a.items():
        e2, d2, m2 = b[k]
        if d1 != d2 or m1 != m2 or len(e1) != len(e2):
            return False
        if any(abs(x - y) > atol for x, y in zip(e1, e2)):
            return False
    return True

# --- #103: the metadata stamp must keep hnsw:space -----------------------
MODE["protocol"] = "ollama"
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-era3", embed_provider="ollama")
cl = nav.client()
try:
    cl.delete_collection(nav.COLLECTION)
except Exception:
    pass  # absent on a fresh state dir
pre = cl.create_collection(name=nav.COLLECTION, metadata={"hnsw:space": "cosine"})  # pre-upgrade shape
# q=[1,0]: cosine ranks v2,v1; l2 ranks v1,v2 (magnitudes differ)
pre.add(ids=["v1", "v2"], embeddings=[[1.0, 0.9], [5.0, 3.0]],
        documents=["doc one", "doc two"], metadatas=[{"sha": "a"}, {"sha": "b"}])
snap = pre.get(include=["embeddings", "documents", "metadatas"])
records_before = {i: (list(map(float, e)), d, m) for i, e, d, m in
                  zip(snap["ids"], snap["embeddings"], snap["documents"], snap["metadatas"])}
err = io.StringIO()
with redirect_stderr(err):
    col = nav._collection()
meta = col.metadata or {}
check("stamp preserves hnsw:space (#103)", meta.get("hnsw:space") == "cosine", str(meta))
check("stamp records embed keys (#103)",
      meta.get("embed_model") == "m-era3" and meta.get("embed_provider") == "ollama", str(meta))
check("stamp keeps vectors (#103)", col.count() == 2, str(col.count()))
got = col.get(include=["embeddings", "documents", "metadatas"])
check("records identical by id across the re-stamp (#103)",
      rec_eq({i: (list(map(float, e)), d, m) for i, e, d, m in
              zip(got["ids"], got["embeddings"], got["documents"], got["metadatas"])},
             records_before),
      f"{len(records_before)} records before vs {col.count()} after")
check("cosine ranking survives the stamp (#103)",
      col.query(query_embeddings=[[1.0, 0.0]], n_results=2)["ids"][0] == ["v2", "v1"],
      str(col.query(query_embeddings=[[1.0, 0.0]], n_results=2)["ids"]))
check("re-stamp announces itself on stderr (#103)", "re-stamp" in err.getvalue(), err.getvalue().strip())
again = nav._collection()
check("re-stamp is idempotent (#103)",
      (again.metadata or {}).get("hnsw:space") == "cosine" and again.count() == 2, str(again.metadata))
# mismatched space: a wiped or foreign stamp gets healed, loudly
try:
    cl.delete_collection(nav.COLLECTION)
except Exception:
    pass
bad = cl.create_collection(name=nav.COLLECTION, metadata={"hnsw:space": "l2"})
bad.add(ids=["v1", "v2"], embeddings=[[1.0, 0.9], [5.0, 3.0]],
        documents=["doc one", "doc two"], metadatas=[{"sha": "a"}, {"sha": "b"}])
err = io.StringIO()
with redirect_stderr(err):
    col = nav._collection()
check("mismatched hnsw:space repaired to cosine (#103)",
      (col.metadata or {}).get("hnsw:space") == "cosine", str(col.metadata))
check("repaired collection ranks cosine (#103)",
      col.query(query_embeddings=[[1.0, 0.0]], n_results=2)["ids"][0] == ["v2", "v1"],
      str(col.query(query_embeddings=[[1.0, 0.0]], n_results=2)["ids"]))
check("repair keeps vectors (#103)", col.count() == 2, str(col.count()))
check("repair is loud (#103)", "re-stamp" in err.getvalue(), err.getvalue().strip())

# --- #103 hardening: CodeRabbit post-merge review of #151 ----------------
MODE["protocol"] = "ollama"
# (1) provider is part of the fingerprint: same model, flipped provider
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-era3", embed_provider="openai")
try:
    nav._collection()
    check("provider flip with same model demands re-embed", False, "no exception")
except RuntimeError as e:
    check("provider flip with same model demands re-embed",
          "provider 'ollama'" in str(e) and "provider 'openai'" in str(e), str(e))
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-era3", embed_provider="ollama")

# (2) the race fallback must validate the winner, not return foreign metadata
try:
    cl.delete_collection(nav.COLLECTION)
except Exception:
    pass
cl.create_collection(name=nav.COLLECTION,
                     metadata={"hnsw:space": "ip", "embed_model": "foreign-model"})


class DeadCol:  # healthy space, no model key -> repair path; get() explodes
    name = nav.COLLECTION
    metadata = {"hnsw:space": "cosine"}

    def get(self, **kw):
        raise RuntimeError("simulated read failure")


try:
    nav._check_model(DeadCol())
    check("race fallback refuses foreign metadata", False, "no exception")
except RuntimeError as e:
    check("race fallback refuses foreign metadata", "foreign" in str(e) or "re-stamp race" in str(e), str(e))

# (3) durability: a mid-copy failure keeps the source; the retry heals fully
try:
    cl.delete_collection(nav.COLLECTION)
except Exception:
    pass
src = cl.create_collection(name=nav.COLLECTION, metadata={"hnsw:space": "cosine"})
ids70 = [f"f{i:03d}" for i in range(70)]  # spans two UPSERT_BATCH=64 batches
src.add(ids=ids70, embeddings=[[1.0 + i / 100.0, 0.9] for i in range(70)],
        documents=[f"doc {i}" for i in range(70)],
        metadatas=[{"sha": f"s{i}"} for i in range(70)])
snap = src.get(include=["embeddings", "documents", "metadatas"])
records = {i: (list(map(float, e)), d, m) for i, e, d, m in
           zip(snap["ids"], snap["embeddings"], snap["documents"], snap["metadatas"])}
real_client = nav.client


class FailingAdd:
    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    def __getattr__(self, k):
        return getattr(self._inner, k)

    def add(self, *a, **k):
        self.calls += 1
        if self.calls >= 2:
            raise RuntimeError("simulated disk full mid-copy")
        return self._inner.add(*a, **k)


class FailingClient:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, k):
        return getattr(self._inner, k)

    def create_collection(self, name=None, **kw):
        col = self._inner.create_collection(name=name, **kw)
        return FailingAdd(col) if name in (nav.COLLECTION, f"{nav.COLLECTION}-restamp") else col


nav.client = lambda: FailingClient(real_client())
try:
    nav._collection()
    check("mid-copy failure raises loudly", False, "no exception")
except RuntimeError as e:
    check("mid-copy failure raises loudly", "disk full" in str(e), str(e))
nav.client = real_client
healed = nav._collection()
check("source survives a mid-copy failure (70 vectors)", healed.count() == 70, str(healed.count()))
h = healed.get(include=["embeddings", "documents", "metadatas"])
check("retry heals records identical by id",
      rec_eq({i: (list(map(float, e)), d, m) for i, e, d, m in
              zip(h["ids"], h["embeddings"], h["documents"], h["metadatas"])}, records), "")
check("healed metadata carries the full stamp",
      (healed.metadata or {}).get("hnsw:space") == "cosine"
      and (healed.metadata or {}).get("embed_model") == "m-era3"
      and (healed.metadata or {}).get("embed_provider") == "ollama", str(healed.metadata))
try:
    cl.get_collection(f"{nav.COLLECTION}-restamp")
    check("no re-stamp temp left behind", False, "temp collection still present")
except Exception:
    check("no re-stamp temp left behind", True)

# --- #159: legacy pre-#17 stores heal instead of refusing ----------------
MODE["protocol"] = "ollama"
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-leg", embed_provider="ollama")


def legacy_store(meta):
    """Main collection in a legacy shape: model-stamped vectors, minus
    whatever keys the era did not stamp yet (engine-store verified:
    model present, provider and hnsw:space null)."""
    try:
        cl.delete_collection(nav.COLLECTION)
    except Exception:
        pass
    col = cl.create_collection(name=nav.COLLECTION, metadata=meta)
    col.add(ids=["v1", "v2"], embeddings=[[1.0, 0.9], [5.0, 3.0]],
            documents=["doc one", "doc two"], metadatas=[{"sha": "a"}, {"sha": "b"}])
    return col


def legacy_open(meta):
    """legacy_store + _collection(); returns (col, stderr, exc) so a
    hard refusal reports as a FAIL row, not a crashed suite."""
    col = legacy_store(meta)
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            return nav._collection(), err.getvalue(), None
    except RuntimeError as e:
        return None, err.getvalue(), e


legacy = legacy_store({"embed_model": "m-leg"})  # the engine-store shape
snap = legacy.get(include=["embeddings", "documents", "metadatas"])
legacy_records = {i: (list(map(float, e)), d, m) for i, e, d, m in
                  zip(snap["ids"], snap["embeddings"], snap["documents"], snap["metadatas"])}
err = io.StringIO()
healed = None
try:
    with redirect_stderr(err):
        healed = nav._collection()
    check("legacy store (model stamped, provider absent) heals via re-stamp (#159)", True)
except RuntimeError as e:
    check("legacy store (model stamped, provider absent) heals via re-stamp (#159)",
          False, str(e))
if healed is not None:
    m = healed.metadata or {}
    check("heal stamps current provider + model + space (#159)",
          m.get("embed_provider") == "ollama" and m.get("embed_model") == "m-leg"
          and m.get("hnsw:space") == "cosine", str(m))
    check("heal keeps vectors (#159)", healed.count() == 2, str(healed.count()))
    got = healed.get(include=["embeddings", "documents", "metadatas"])
    check("heal keeps records identical by id (#159)",
          rec_eq({i: (list(map(float, e)), d, m) for i, e, d, m in
                  zip(got["ids"], got["embeddings"], got["documents"], got["metadatas"])},
                 legacy_records), "")
    check("healed store serves normal ops: cosine ranking (#159)",
          healed.query(query_embeddings=[[1.0, 0.0]], n_results=2)["ids"][0] == ["v2", "v1"],
          str(healed.query(query_embeddings=[[1.0, 0.0]], n_results=2)["ids"]))
    check("heal is announced on stderr (#159)", "re-stamp" in err.getvalue(), err.getvalue().strip())
    check("healed store takes the stamped fast path next call (#159)",
          nav._collection().count() == 2, "")

# mid-era shape (between #103 and #17): model + space stamped, provider
# absent — the fast path must not swallow it, the stamp still heals
healed2, out2, exc2 = legacy_open({"hnsw:space": "cosine", "embed_model": "m-leg"})
check("mid-era store (space stamped, provider absent) heals too (#159)",
      exc2 is None and (healed2.metadata or {}).get("embed_provider") == "ollama",
      str(exc2) if exc2 else str(healed2.metadata))
check("mid-era heal keeps vectors (#159)",
      healed2 is not None and healed2.count() == 2,
      str(healed2.count()) if healed2 else "refused")

# provider-less vectors are pre-#17 ollama-protocol output: under an
# openai config that is real drift — refuse, printing the raw stamp
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-leg", embed_provider="openai")
_, _, exc3 = legacy_open({"embed_model": "m-leg"})
check("provider-less store vs openai config still refuses (#159)",
      exc3 is not None and "run `python nav.py drop`" in str(exc3),
      str(exc3) if exc3 else "no exception")
check("refusal prints the raw stored provider, no 'ollama' default (#159)",
      exc3 is not None and "provider None" in str(exc3) and "provider 'openai'" in str(exc3),
      str(exc3))

# --- #159: base manifest gates dim only when stamped -----------------------
write_cfg(embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-leg", embed_provider="ollama")
nav.BASE_DIR.mkdir(parents=True)
for rid in ("f1.txt", "f2.txt"):
    (nav.ROOT / rid).write_text(f"content of {rid}\n", encoding="utf-8")
with gzip.GzipFile(nav.BASE_DIR / "shard-0000.jsonl.gz", mode="wb", compresslevel=9, mtime=0) as f:
    for rid, emb in (("f1.txt", [1.0, 0.9]), ("f2.txt", [5.0, 3.0])):
        f.write((json.dumps({"id": rid, "emb": emb, "meta": {"sha": rid}},
                            sort_keys=True) + "\n").encode("utf-8"))
manifest = {"model": "m-leg", "count": 2, "shards": 1,
            "exported_at": "2026-09-12T00:00:00+00:00"}  # no dim/provider: legacy
(nav.BASE_DIR / nav.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n",
                                              encoding="utf-8")
try:  # import seeds only an empty store
    cl.delete_collection(nav.COLLECTION)
except Exception:
    pass
try:
    report = nav.import_base()
    check("manifest without dim/provider imports fine (#159)",
          report.get("imported") == 2, str(report))
except RuntimeError as e:
    check("manifest without dim/provider imports fine (#159)", False, str(e))
manifest["dim"] = 999  # present and wrong: the hard gate stays
(nav.BASE_DIR / nav.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n",
                                              encoding="utf-8")
try:  # empty again for the refusal leg
    cl.delete_collection(nav.COLLECTION)
except Exception:
    pass
try:
    nav.import_base()
    check("manifest with a changed dim still refuses (#159)", False, "no exception")
except RuntimeError as e:
    check("manifest with a changed dim still refuses (#159)",
          "999" in str(e) and "run `python nav.py drop`" in str(e), str(e))
    check("manifest refusal prints the raw provider, no 'ollama' default (#159)",
          "provider None" in str(e), str(e))


# --- #220: the stamp records embed mode; cross-mode reuse is loud --------
MODE["hash_vecs"] = True
C220 = TMP / "corpus220"
C220.mkdir(exist_ok=True)
for i in range(3):
    (C220 / f"f{i}.py").write_text(f"def fn_{i}():\n    return {i}\n",
                                   encoding="utf-8", newline="\n")
write_cfg(root=str(C220), state_dir=str(TMP / "state220"), collection="mode220",
          embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-220",
          embed_provider="ollama", embed_dim=32, include_dirs=["."],
          extensions=[".py"], exclude_dirs=[])


def _cos220(a, b):
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _docs_and_fresh():
    got = nav._collection().get(limit=3, include=["embeddings", "documents"])
    docs = list(got["documents"])
    texts = ([nav.EMBED_DOC_PREFIX + d for d in docs]
             if nav.EMBED_DOC_PREFIX else docs)  # mirror rescan's flush()
    return got, nav.embed(texts)


# fake bootstrap (the CI test_recall shape) builds + stamps fake
os.environ["NEURONAV_EMBED_FAKE"] = "1"
err = io.StringIO()
with redirect_stderr(err):
    st = nav.rescan()
check("220: fake rescan builds the store",
      st["added"] == 3 and nav.count() == 3 and err.getvalue() == "",
      f"{st['added']}+ files, stderr={err.getvalue()[:80]!r}")
check("220: store stamps embed_mode=fake",
      (nav._collection().metadata or {}).get("embed_mode") == "fake",
      str(nav._collection().metadata))
err = io.StringIO()
with redirect_stderr(err):
    st = nav.rescan()
check("220: fake->fake rescan stays sha-gated (CI pattern unchanged)",
      (st["added"], st["updated"], st["unchanged"]) == (0, 0, 3)
      and "re-embedding" not in err.getvalue(),
      f"{st['added']}+/{st['updated']}~/{st['unchanged']}=")

# real rescan over the fake store: loud full re-embed + heal
del os.environ["NEURONAV_EMBED_FAKE"]
err = io.StringIO()
with redirect_stderr(err):
    st = nav.rescan()
check("220: real rescan re-embeds a fake store loudly",
      st["updated"] == 3
      and "'fake'-mode vectors but this rescan embeds 'real'" in err.getvalue(),
      f"{st['updated']}~ stderr={err.getvalue()[:120]!r}")
col = nav._collection()
check("220: healed store stamps embed_mode=real",
      (col.metadata or {}).get("embed_mode") == "real", str(col.metadata))
got, fresh = _docs_and_fresh()
sims = [round(_cos220(s, v), 4) for s, v in zip(got["embeddings"], fresh)]
check("220: healed vectors match fresh real embeds (cosine ~1)",
      min(sims) > 0.999, str(sims))
err = io.StringIO()
with redirect_stderr(err):
    st = nav.rescan()
check("220: real->real rescan stays sha-gated",
      (st["added"], st["updated"], st["unchanged"]) == (0, 0, 3)
      and "re-embedding" not in err.getvalue(),
      f"{st['added']}+/{st['updated']}~/{st['unchanged']}=")

# fn store rides the same gate (graph's sha-cache would otherwise reuse
# fake fn vectors for real queries)
import graph

os.environ["NEURONAV_EMBED_FAKE"] = "1"
err = io.StringIO()
with redirect_stderr(err):
    fns_fake = graph.sync_functions([], [])  # first build: fake mode
del os.environ["NEURONAV_EMBED_FAKE"]
err = io.StringIO()
with redirect_stderr(err):
    fns_real = graph.sync_functions([], [])
check("220: fn store re-embeds across the mode gate too",
      fns_fake["fns_upserted"] == 3 and fns_real["fns_upserted"] == 3
      and fns_real["fns_cached"] == 0 and "fn store" in err.getvalue(),
      f"fake={fns_fake} real={fns_real} stderr={err.getvalue()[:100]!r}")
check("220: fn store stamps embed_mode=real after heal",
      (nav.fns_collection().metadata or {}).get("embed_mode") == "real",
      str(nav.fns_collection().metadata))

# bench guard: healthy store passes, poisoned store refuses loudly
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
import run_bench  # noqa: E402

check("220: bench guard passes a healthy store",
      run_bench._verify_store_vectors(nav), "")
os.environ["NEURONAV_EMBED_FAKE"] = "1"
nav.client().delete_collection(nav.COLLECTION)
with redirect_stderr(io.StringIO()):
    nav.rescan()  # rebuild the poison: fake vectors, real-mode process next
del os.environ["NEURONAV_EMBED_FAKE"]
buf = io.StringIO()
with redirect_stdout(buf):
    ok = run_bench._verify_store_vectors(nav)
check("220: bench guard refuses a mode-poisoned store",
      not ok and str(TMP / "state220" / "chroma") in buf.getvalue()
      and "embed_mode='fake'" in buf.getvalue() and "#220" in buf.getvalue(),
      buf.getvalue()[:160].replace("\n", " "))

# pre-#220 lineage: a store with no embed_mode key is real, never a
# MODE mismatch — the mode law alone must not churn owner-rig stores.
# #229: the same unstamped store still re-embeds ONCE, loudly, for the
# doc-shape upgrade (raw-doc vectors under a shaping process), then
# heals stamped and stays gated.
nav.client().delete_collection(nav.COLLECTION)
old = nav.client().create_collection(
    name=nav.COLLECTION,
    metadata={"hnsw:space": "cosine", "embed_model": "m-220",
              "embed_provider": "ollama"})
fps = nav.stat_fingerprint()
for p in sorted(C220.glob("*.py")):
    doc_text = p.read_text(encoding="utf-8")
    fid = nav.file_id(p)
    m = fps[fid]
    old.add(ids=[fid], embeddings=nav.embed([doc_text]), documents=[doc_text],
            metadatas=[{"sha": nav.sha256_of(p), "ext": ".py",
                        "mtime_ns": m[0], "size": m[1]}])
err = io.StringIO()
with redirect_stderr(err):
    st = nav.rescan()
check("220: unstamped store churns for the #229 shape, never the mode",
      st["updated"] == 3 and "(#229)" in err.getvalue()
      and "embed_mode" not in err.getvalue() and "'-mode" not in err.getvalue(),
      f"{st['added']}+/{st['updated']}~/{st['unchanged']}="
      f" stderr={err.getvalue()[:100]!r}")
err = io.StringIO()
with redirect_stderr(err):
    st = nav.rescan()
check("220: healed pre-law store stays gated after the shape upgrade",
      (st["added"], st["updated"], st["unchanged"]) == (0, 0, 3)
      and "re-embedding" not in err.getvalue()
      and (nav._collection().metadata or {}).get("doc_shape", "").startswith("cast"),
      f"{st['added']}+/{st['updated']}~/{st['unchanged']}="
      f" stderr={err.getvalue()[:80]!r}")
MODE["hash_vecs"] = False
os.environ.pop("NEURONAV_EMBED_FAKE", None)

# --- #229: the doc-shape stamp — cAST-shaped docs re-embed loudly ------
# The #220 law extended to doc construction: sha-gating skips on
# unchanged bytes, so a shape flip must force the re-embed instead of
# serving vectors built from the other doc shape.
MODE["hash_vecs"] = True  # deterministic 32-dim rows match embed_dim=32
write_cfg(root=str(C220), state_dir=str(TMP / "state220"), collection="mode220",
          embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-220",
          embed_provider="ollama", embed_dim=32, include_dirs=["."],
          extensions=[".py"], exclude_dirs=[], chunk_file_doc=0.0)
check("229: doc_shape reports raw under the 0.0 knob",
      nav.doc_shape() == "raw", nav.doc_shape())
err = io.StringIO()
with redirect_stderr(err):
    st = nav.rescan()
check("229: raw flip re-embeds the shaped store loudly",
      st["updated"] == 3
      and "docs shaped 'cast1@1' but this rescan shapes 'raw'" in err.getvalue(),
      f"{st['updated']}~ stderr={err.getvalue()[:120]!r}")
check("229: raw store stamps doc_shape=raw",
      (nav._collection().metadata or {}).get("doc_shape") == "raw",
      str(nav._collection().metadata))
_ids = sorted(p.name for p in C220.glob("*.py"))
got = nav._collection().get(ids=_ids, include=["documents"])
check("229: raw docs are the file text again",
      all(d == (C220 / i).read_text(encoding="utf-8")
          for i, d in zip(_ids, got["documents"])),
      str(got["documents"])[:100])

write_cfg(root=str(C220), state_dir=str(TMP / "state220"), collection="mode220",
          embed_url=f"http://127.0.0.1:{PORT}/api/embed", embed_model="m-220",
          embed_provider="ollama", embed_dim=32, include_dirs=["."],
          extensions=[".py"], exclude_dirs=[], chunk_file_doc=1.0)
check("229: doc_shape reports cast<rev>@scale under the 1.0 knob",
      nav.doc_shape() == f"cast{graph.FILE_DOC_REV}@1", nav.doc_shape())
err = io.StringIO()
with redirect_stderr(err):
    st = nav.rescan()
check("229: shape flip re-embeds the raw store loudly",
      st["updated"] == 3 and "docs shaped 'raw'" in err.getvalue(),
      f"{st['updated']}~ stderr={err.getvalue()[:120]!r}")
col = nav._collection()
check("229: healed store stamps the doc shape",
      (col.metadata or {}).get("doc_shape") == f"cast{graph.FILE_DOC_REV}@1",
      str(col.metadata))
got = col.get(ids=_ids, include=["documents"])
check("229: stored docs are the cAST-shaped docs",
      all(d.startswith(f"# {i}") and "# symbols: fn_" in d and "def fn_" in d
          for i, d in zip(_ids, got["documents"])),
      str(got["documents"])[:100])
err = io.StringIO()
with redirect_stderr(err):
    st = nav.rescan()
check("229: same-shape rescan stays sha-gated",
      (st["added"], st["updated"], st["unchanged"]) == (0, 0, 3)
      and "re-embedding" not in err.getvalue(),
      f"{st['added']}+/{st['updated']}~/{st['unchanged']}=")

print()
print(f"{len(FAILS)} failure(s)" + (": " + ", ".join(FAILS) if FAILS else ""))
sys.exit(1 if FAILS else 0)
