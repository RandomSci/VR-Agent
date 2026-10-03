from src.open_llm_vtuber.live import clip_marks as cm


def test_moments_become_45_second_clips_best_first(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "LOG_DIR", tmp_path)
    clips = cm.ClipMarks()
    clips.set_live(1000.0, "abc123")
    clips.mark("done", "Mika: piece built", 1.0, at=1000.0 + 600)
    clips.mark(
        "wrong", "Luna: network guessed wrong (9 for a 4)", 2.5, at=1000.0 + 1810
    )
    clips.mark(
        "reaction", "Luna: Argh!!! a 9?!", 1.5, at=1000.0 + 1815
    )  # same moment: one clip
    clips.mark("built", "Castle COMPLETE", 4.0, at=1000.0 + 3700)
    out = clips.clips()
    assert [round(c["score"], 1) for c in out] == [4.0, 4.0, 1.0]
    castle = next(c for c in out if c["moments"][0].text == "Castle COMPLETE")
    assert castle["end"] - castle["start"] == cm.BEFORE + cm.AFTER == 45
    merged = next(
        c for c in out if len(c["moments"]) == 2
    )  # the guess and her Argh: one clip, 50 s
    assert merged["end"] - merged["start"] == 50
    text = clips.report()
    assert "1:01:20 - 1:02:05" in text  # Castle: 20 s before to 25 s after, stream time
    assert "https://youtu.be/abc123?t=3680" in text
    assert "29:50 - 30:40" in text and "Argh" in text  # the merged guess + reaction
    path = clips.save()
    assert path and path.read_text().startswith("✂")


def test_youtube_time_wins_over_the_obs_guess():
    clips = cm.ClipMarks()
    clips.set_live(500.0, exact=False)  # OBS started streaming
    clips.set_live(520.0, "vid")  # YouTube: really live 20 s later
    clips.set_live(530.0, exact=False)  # a second OBS guess never overrides it
    assert clips.live_since == 520.0 and clips.video_id == "vid"


def test_nothing_to_report_without_moments(capsys):
    clips = cm.ClipMarks()
    clips.finish()
    assert capsys.readouterr().out == ""


def test_moments_close_together_never_make_a_clip_over_a_minute():
    clips = cm.ClipMarks()
    clips.set_live(0.0)
    for i in range(10):
        clips.mark("chat", f"moment {i}", at=100.0 + i * 15)
    assert all(c["end"] - c["start"] <= cm.MAX_CLIP for c in clips.clips())
