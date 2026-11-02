"""Marker 218 (WT test report 25 Oct: "Open Calculator and calculate 125 times 8" -> the calculator never opened and
Jupiter said it was opening it): open Windows Calculator, press its OWN buttons through UI Automation (no mouse, no
keyboard, works even if another window is in front) and read the answer back from Calculator's display. The answer
read from the display is the proof that it worked; nothing is claimed without it."""
from __future__ import annotations

import re
import sys
import time
from typing import Any, Callable

_WORDS = [
    (r"\b(?:multiplied\s+by|times|into|x|guna|gunaa)\b|[×✕]", "*"),
    (r"\b(?:divided\s+by|over|bata|by)\b|÷", "/"),
    (r"\b(?:plus|add|and|jod|jodo)\b", "+"),
    (r"\b(?:minus|less|subtract|ghatao)\b|−", "-"),
    (r"\b(?:point|dashamlav)\b", "."),
]
_SAFE = re.compile(r"^\d+(?:\.\d+)?(?:[+\-*/]\d+(?:\.\d+)?)*$")
# Calculator's own buttons (AutomationId) for each character of the expression.
BUTTONS = {**{str(d): f"num{d}Button" for d in range(10)}, "+": "plusButton", "-": "minusButton",
           "*": "multiplyButton", "/": "divideButton", ".": "decimalSeparatorButton", "=": "equalButton"}


def expression_of(text: str) -> str:
    """"calculate 125 times 8" -> "125*8"; "" when the words are not a plain arithmetic expression."""
    value = str(text or "").casefold()
    asked = re.search(r"\b(?:calculate|compute|solve|what\s+is|what's|kitna\s+hai|kitne)\b(.+)$", value)
    if asked:                                       # "open calculator and calculate 125 times 8": only the maths
        value = asked.group(1)
    value = re.sub(r"(?<=\d),(?=\d{3}\b)", "", value)                      # 1,000 -> 1000
    for pattern, symbol in _WORDS:
        value = re.sub(pattern, f" {symbol} ", value)
    tokens = re.findall(r"\d+(?:\.\d+)?|[+\-*/]", value)
    expression = "".join(tokens)
    return expression if _SAFE.match(expression) and re.search(r"[+\-*/]", expression) else ""


def display_value(name: str) -> str:
    """Calculator's result element is named "Display is 1,000"."""
    match = re.search(r"display\s+is\s+(.+)$", str(name or ""), re.I)
    return match.group(1).strip() if match else ""


def calculate(expression: str, *, presser: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    """Press the buttons for ``expression`` + "=" and return {"ok", "expression", "result"} from the display."""
    if not _SAFE.match(str(expression or "")):
        return {"ok": False, "error": "unsafe_expression"}
    sequence = ["clearButton"] + [BUTTONS[ch] for ch in expression + "="]
    try:
        shown = (presser or _press_in_calculator)(sequence)
    except Exception:
        shown = ""
    result = display_value(shown)
    if not result:
        return {"ok": False, "error": "no_result", "expression": expression}
    return {"ok": True, "expression": expression, "result": result}


def _press_in_calculator(sequence: list[str], wait_s: float = 8.0) -> str:     # pragma: no cover - needs Windows
    """Open (or reuse) Calculator, Invoke each button by AutomationId, return the display's Name."""
    if sys.platform != "win32":
        return ""
    import os

    import clr
    from services.windows_automation import _UIA_STRONG_NAME
    clr.AddReference(f"UIAutomationClient, {_UIA_STRONG_NAME}")
    clr.AddReference(f"UIAutomationTypes, {_UIA_STRONG_NAME}")
    from System.Windows.Automation import (AndCondition, AutomationElement, ControlType, InvokePattern,
                                           PropertyCondition, TreeScope)

    def calculator_window():
        """A Calculator window whose buttons really exist. A minimised or suspended (DWM-cloaked) Store frame is
        still listed as "Calculator" but exposes no buttons (WT test 26 Oct: that frame was picked and nothing
        was pressed), so it does not count."""
        condition = AndCondition(PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Window),
                                 PropertyCondition(AutomationElement.NameProperty, "Calculator"))
        for candidate in AutomationElement.RootElement.FindAll(TreeScope.Children, condition):
            if candidate.FindFirst(TreeScope.Descendants,
                                   PropertyCondition(AutomationElement.AutomationIdProperty, "num1Button")) is not None:
                return candidate
        return None

    window = calculator_window()
    if window is None:
        os.startfile("calculator:")              # the Store app's own protocol: opens, restores or wakes it
        deadline = time.monotonic() + wait_s
        while window is None and time.monotonic() < deadline:
            time.sleep(0.25)
            window = calculator_window()
    if window is None:
        return ""

    def find(automation_id: str):
        element = None
        deadline = time.monotonic() + 3.0
        while element is None and time.monotonic() < deadline:
            element = window.FindFirst(TreeScope.Descendants,
                                       PropertyCondition(AutomationElement.AutomationIdProperty, automation_id))
            if element is None:
                time.sleep(0.15)
        return element

    for automation_id in sequence:
        button = find(automation_id)
        if button is None:
            if automation_id == "clearButton":
                continue                                     # older layouts: "CE"/"C" may be missing; go on
            return ""
        button.GetCurrentPattern(InvokePattern.Pattern).Invoke()
        time.sleep(0.05)
    time.sleep(0.25)
    display = find("CalculatorResults")
    return str(display.Current.Name or "") if display is not None else ""
