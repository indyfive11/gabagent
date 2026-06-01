from gabagent.voice.speakable import SpeakableFilter


def _run(chunks, notice="CODE"):
    f = SpeakableFilter(code_notice=notice)
    out = []
    for ch in chunks:
        out += f.feed(ch)
    out += f.flush()
    speaks = [t for k, t in out if k == "speak"]
    statuses = [t for k, t in out if k == "status"]
    return speaks, statuses


def test_prose_flushes_on_sentence_boundary():
    speaks, _ = _run(["Hello there. How are you?"])
    assert "Hello there." in speaks
    assert any("How are you?" in s for s in speaks)


def test_code_block_suppressed_with_one_status():
    speaks, statuses = _run(["Here is a plan. ", "```python\nsecret = 1\n```", " All done."])
    assert "Here is a plan." in speaks
    assert "CODE" in statuses
    assert any("All done" in s for s in speaks)
    assert not any("secret" in s for s in speaks)


def test_code_fence_split_across_chunks():
    # Closing fence arrives split as "``" then "`".
    speaks, statuses = _run(["intro. ", "``", "`py\nx=1\n``", "`", " end."])
    assert statuses.count("CODE") == 1
    assert not any("x=1" in s for s in speaks)
    assert any("end" in s for s in speaks)


def test_markdown_markers_stripped():
    speaks, _ = _run(["# Heading\n", "Some *bold* and `code` text."])
    joined = " ".join(speaks)
    assert "#" not in joined
    assert "*" not in joined
    assert "`" not in joined
    assert "bold" in joined


def test_long_run_safety_flush():
    speaks, _ = _run([("word " * 60)])  # no sentence boundary
    assert speaks  # flushed despite no punctuation
