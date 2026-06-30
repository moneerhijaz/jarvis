from jarvis.memory.vault import Vault
from jarvis.memory.graph import build_graph
from jarvis.memory.retrieval import Retriever
from jarvis.memory.gardener import run_gardener


def test_scaffold_and_profile(tmp_path):
    v = Vault(tmp_path / "brain")
    assert not v.exists()
    v.scaffold()
    assert v.exists()
    # nested taxonomy dir must be created with parents=True (regression guard)
    assert (v.root / "system" / "templates").is_dir()
    v.write_profile("# About me\nI like concise answers.")
    assert "concise" in v.read_profile()


def test_note_ids_are_unique_per_path(tmp_path):
    v = Vault(tmp_path / "brain")
    v.scaffold()
    a = v.write_note("Meeting", "notes one", folder="notes")
    b = v.write_note("Meeting", "people note", folder="people")
    # same title, different folders -> distinct, path-derived IDs (no collision)
    assert a.meta["id"] != b.meta["id"]
    # rewriting the same note keeps its ID stable
    again = v.write_note("Meeting", "notes one updated", folder="notes")
    assert again.meta["id"] == a.meta["id"]


def test_write_note_frontmatter_and_links(tmp_path):
    v = Vault(tmp_path / "brain")
    v.scaffold()
    n = v.write_note("Vault design", "Built on [[memory]] ideas.", tags=["arch"], links=["second brain"])
    reread = v.read_note(n.path)
    assert reread.meta["title"] == "Vault design"
    assert reread.meta["tags"] == ["arch"]
    assert "memory" in reread.links
    assert "second brain" in reread.links


def test_graph_neighbors(tmp_path):
    v = Vault(tmp_path / "brain")
    v.scaffold()
    v.write_note("alpha", "links to [[beta]]")
    v.write_note("beta", "links to [[gamma]]")
    v.write_note("gamma", "leaf")
    g = build_graph(v)
    nb = g.neighbors("notes/alpha.md", hops=2)
    assert "notes/beta.md" in nb
    assert "notes/gamma.md" in nb  # reached via 2 hops


def test_hybrid_retrieval_keyword_only(tmp_path):
    v = Vault(tmp_path / "brain")
    v.scaffold()
    v.write_note("coffee", "The user prefers oat milk lattes.")
    v.write_note("unrelated", "Notes about taxes.")
    r = Retriever(v, embed_fn=None)
    hits = r.search("what milk for coffee")
    assert hits
    assert hits[0].source == "notes/coffee.md"
    assert hits[0].text  # excerpt present


def test_project_scaffold_and_scope(tmp_path):
    v = Vault(tmp_path / "brain")
    v.scaffold()
    base = v.new_project("YouTube Channel", goal="grow to 10k subs", role="editor")
    for sub in ("Inputs", "Process", "Outputs", "Feedback"):
        assert (base / sub).exists()
    assert "youtube-channel" in v.list_projects()
    v.write_note("idea1", "a project idea", folder="projects/youtube-channel/Inputs")
    r = Retriever(v, embed_fn=None)
    scoped = r.search("idea", project="YouTube Channel")
    assert all("youtube-channel" in s.source for s in scoped)


def test_gardener_files_inbox_and_summarizes(tmp_path):
    v = Vault(tmp_path / "brain")
    v.scaffold()
    v.capture("Remember: deploy on Fridays.")
    report = run_gardener(v)
    assert report.filed  # inbox note was filed
    # inbox emptied of that note
    assert not list((v.root / "inbox").glob("*.md"))
    # a daily summary exists
    assert list((v.root / "daily").glob("*-garden.md"))


def test_reindex_rebuilds_from_markdown(tmp_path):
    import shutil
    v = Vault(tmp_path / "brain")
    v.scaffold()
    v.write_note("keepme", "important content")
    cache = v.root / ".jarvis"
    (cache / "junk.txt").write_text("derived")
    shutil.rmtree(cache)  # nuke derived cache
    # canonical markdown still present -> retrieval still works
    r = Retriever(v, embed_fn=None)
    assert r.search("important content")
