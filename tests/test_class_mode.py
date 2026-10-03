from open_llm_vtuber.room.class_course import lesson_at
from open_llm_vtuber.room.class_mode import (
    normalize_lesson,
    parse_json_object,
    speech_chunks,
    variable_name,
)


def test_speech_chunks_fit_one_tts_call():
    text = "This is a sentence that goes on. " * 30
    chunks = speech_chunks(text)
    assert len(chunks) > 1
    assert all(len(c) <= 280 for c in chunks)
    assert " ".join(chunks) == " ".join(text.split())


def test_viewer_names_become_variables():
    assert variable_name("@Math Fan!") == "math_fan"
    assert variable_name("42cats") == "v_42cats"
    assert variable_name("print") == "print_"
    assert variable_name("@!!") == ""


def test_lesson_is_cleaned_up():
    raw = parse_json_object(
        '```json\n{"title": "Lists", "steps": ['
        '{"who": "teacher", "say": "Hi", "slide": {"title": "Lists", "bullets": ["a"]}},'
        '{"who": "sidekick", "say": "Why zero?"},'
        '{"who": "teacher", "say": "Watch", "cell": "x = [1]\\nx"},'
        '{"who": "sidekick", "say": "Oops", "cell": "x.add(2)", "expect_error": true},'
        '{"quiz": {"question": "len?", "choices": ["1", "2"], "answer": "a"}},'
        '{"quiz": {"question": "bad", "choices": ["1"], "answer": "Z"}}'
        "]}\n```"
    )
    lesson = normalize_lesson(raw, "Lists")
    assert lesson and lesson["title"] == "Lists"
    assert lesson["steps"][3]["expect_error"] is True
    assert lesson["steps"][4]["quiz"]["answer"] == 0
    assert all("quiz" not in s or s["quiz"]["question"] != "bad" for s in lesson["steps"])


def test_course_rolls_into_the_next():
    course_id, index, title, _ = lesson_at("python-basics", 999)
    assert course_id == "math-with-python" and index == 0 and title


def test_progress_and_current_lesson_survive_a_restart(tmp_path):
    from types import SimpleNamespace

    from open_llm_vtuber.room.class_mode import ClassEngine

    session = SimpleNamespace(room=SimpleNamespace(characters=[SimpleNamespace(id="natori")], get=lambda c: None))
    engine = ClassEngine(None, session, course_id="python-basics")
    engine.data_dir = tmp_path
    lesson = {"title": "Lists", "steps": [{"who": "teacher", "say": "Hi"}]}
    engine._save_current("data-basics", 4, lesson)
    engine._save_progress("data-basics", 4, 7)
    again = ClassEngine(None, session, course_id="python-basics")
    again.data_dir = tmp_path
    progress = again._load_progress()
    assert (progress["course"], progress["lesson"], progress["step"]) == ("data-basics", 4, 7)
    assert again._load_current("data-basics", 4) == lesson
    assert again._load_current("data-basics", 5) is None
