"""The notebook kernel that runs INSIDE the sandbox (one per class).

It reads one JSON request per line on stdin ({"id", "code"}), runs the cell in
one shared namespace (so variables carry over, like Jupyter) and writes one
reply line that starts with MARKER. Anything else a program prints straight
to the terminal is collected by the parent as part of the cell's output.

Reply: {"id", "stdout", "error": {"name", "value", "line", "trace"} | null,
"result": {"text", "latex"} | null, "images": [base64 png]}
"""

import ast
import base64
import contextlib
import io
import json
import os
import sys
import traceback

MARKER = "\x1e__VRK__"
MAX_TEXT = 6000

namespace = {"__name__": "__main__"}
real_stdout = sys.stdout
cell_number = 0


def _send(payload):
    real_stdout.write(MARKER + json.dumps(payload) + "\n")
    real_stdout.flush()


def _latex(value):
    sympy = sys.modules.get("sympy")
    if sympy is None:
        return ""
    try:
        if isinstance(value, (sympy.Basic, sympy.MatrixBase)):
            return sympy.latex(value)
    except Exception:
        return ""
    return ""


def _figures():
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is None:
        return []
    images = []
    for number in plt.get_fignums()[:4]:
        figure = plt.figure(number)
        buffer = io.BytesIO()
        try:
            figure.savefig(buffer, format="png", dpi=90, bbox_inches="tight")
            images.append(base64.b64encode(buffer.getvalue()).decode())
        except Exception:
            pass
    plt.close("all")
    return images


def run(code):
    global cell_number
    cell_number += 1
    name = f"<cell {cell_number}>"
    out = io.StringIO()
    result = None
    error = None
    try:
        tree = ast.parse(code, filename=name)
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body.pop().value)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            exec(compile(tree, name, "exec"), namespace)
            if last is not None:
                value = eval(compile(last, name, "eval"), namespace)
                if value is not None:
                    namespace["_"] = value
                    result = {"text": repr(value)[:MAX_TEXT], "latex": _latex(value)[:MAX_TEXT]}
    except BaseException as exc:  # SystemExit and KeyboardInterrupt too: the kernel lives on
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == name]
        line = frames[-1].lineno if frames else getattr(exc, "lineno", None)
        trace = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        error = {
            "name": type(exc).__name__,
            "value": str(exc)[:800],
            "line": line,
            "trace": (f"line {line}: " if line else "") + trace[:1500],
        }
    return {
        "stdout": out.getvalue()[:MAX_TEXT],
        "error": error,
        "result": result,
        "images": _figures(),
    }


def main():
    os.environ.setdefault("MPLBACKEND", "Agg")
    _send({"id": "ready"})
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
        except ValueError:
            continue
        reply = run(str(request.get("code") or ""))
        reply["id"] = request.get("id")
        _send(reply)


if __name__ == "__main__":
    main()
