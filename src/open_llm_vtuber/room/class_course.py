"""What class mode teaches, in order. Only outlines: every lesson itself is
written fresh by the LLM, checked in a real notebook, then taught on stream.

Pick the course with VR_CLASS_COURSE (default python-basics). When a course
ends the next one starts, so the class never runs out.
"""

from __future__ import annotations

from typing import Any

COURSES: dict[str, dict[str, Any]] = {
    "python-basics": {
        "title": "Python Basics",
        "audience": "complete beginners who have never coded",
        "lessons": [
            ("Hello, Python!", "print, comments, running a cell, what an error looks like"),
            ("Variables", "naming things, =, changing a value, why names matter"),
            ("Numbers and math", "+ - * / // % **, int vs float, order of operations"),
            ("Strings", "quotes, joining with +, f-strings, len, upper and lower"),
            ("Lists", "making a list, indexing from 0, negative index, len"),
            ("Growing lists", "append, insert, remove, pop, in"),
            ("Slicing", "list[a:b], list[:3], list[::-1], strings slice too"),
            ("For loops", "for item in list, range, adding things up in a loop"),
            ("If and else", "comparisons, if / elif / else, and / or / not"),
            ("Functions", "def, parameters, return vs print, calling a function"),
            ("Dictionaries", "keys and values, looking up, adding, looping over items"),
            ("While loops", "while, counting, break, avoiding infinite loops"),
            ("List comprehensions", "[x*2 for x in nums], with an if filter"),
            ("Errors are friends", "reading a traceback, NameError, TypeError, IndexError, try / except"),
            ("Mini project: a grade book", "dictionary of scores, average, best student, a small report"),
        ],
    },
    "math-with-python": {
        "title": "Math with Python",
        "audience": "students who know a little Python and want to see math come alive",
        "lessons": [
            ("Python as a calculator", "math module, sqrt, pi, rounding, big numbers"),
            ("Symbols with sympy", "import sympy as sp, sp.symbols, expressions, expand, factor"),
            ("Solving equations", "sp.Eq, sp.solve, checking an answer by substituting"),
            ("Functions and graphs", "plotting y = x**2 with matplotlib, reading a graph"),
            ("Derivatives", "sp.diff, slope of a curve, plotting a tangent line"),
            ("Integrals", "sp.integrate, area under a curve"),
            ("Matrices by hand", "sp.Matrix, adding, multiplying row by column step by step"),
            ("Determinant and inverse", "2x2 determinant by hand, then sympy checks it"),
            ("Systems of equations", "solving with matrices, linsolve"),
        ],
    },
    "data-basics": {
        "title": "Data with numpy and pandas",
        "audience": "students who know Python basics",
        "lessons": [
            ("numpy arrays", "np.array, shape, math on a whole array at once"),
            ("Array tricks", "arange, linspace, mean, max, boolean filters"),
            ("pandas DataFrames", "making a table from a dict, head, columns"),
            ("Filtering tables", "df[df.score > 80], sorting, picking columns"),
            ("Group and summarise", "groupby, mean, counting"),
            ("Charts from data", "bar and line charts with matplotlib from a DataFrame"),
        ],
    },
}

ORDER = ["python-basics", "math-with-python", "data-basics"]


def course(course_id: str) -> dict[str, Any]:
    return COURSES.get(course_id) or COURSES[ORDER[0]]


def next_course(course_id: str) -> str:
    try:
        index = ORDER.index(course_id)
    except ValueError:
        return ORDER[0]
    return ORDER[(index + 1) % len(ORDER)]


def lesson_at(course_id: str, index: int) -> tuple[str, int, str, str]:
    """(course id, lesson index, title, goals), rolling into the next course."""
    current = course_id if course_id in COURSES else ORDER[0]
    for _ in range(len(ORDER) + 1):
        lessons = COURSES[current]["lessons"]
        if 0 <= index < len(lessons):
            title, goals = lessons[index]
            return current, index, title, goals
        current, index = next_course(current), 0
    title, goals = COURSES[ORDER[0]]["lessons"][0]
    return ORDER[0], 0, title, goals
