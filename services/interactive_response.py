"""Message-only, non-executable interactive response requests.

Explicit intent is resolved locally by the orchestrator. No provider HTML,
script, style, URL or global application command is accepted as a component.
"""
import re
import math


COMPONENT_REGISTRY = {
    "calculator": {"version": 1, "title": "Calculator"},
    "matrix_determinant": {"version": 1, "title": "Matrix determinant"},
    "differentiation_calculator": {"version": 1, "title": "Differentiation calculator"},
    "integration_calculator": {"version": 1, "title": "Integration calculator"},
    "function_graph": {"version": 1, "title": "Function graph"},
}
MATH_NAMES = {"x", "pi", "e", "sin", "cos", "tan", "asin", "acos", "atan",
              "sinh", "cosh", "tanh", "sqrt", "abs", "log", "exp"}


def valid_expression(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        return False
    if re.search(r"[^a-z0-9.+\-*/^()\s]", value) or set(re.findall(r"[a-z]+", value)) - MATH_NAMES:
        return False
    depth = 0
    for char in value:
        depth += (char == "(") - (char == ")")
        if depth < 0 or depth > 12:
            return False
    return depth == 0 and value.count("^") <= 8


def validate_interactive_response(value):
    if not isinstance(value, dict) or set(value) - {"type", "component", "version", "props"}:
        return None
    if value.get("type") != "interactive_response" or type(value.get("version")) is not int or value.get("version") != 1:
        return None
    component = value.get("component")
    props = value.get("props", {})
    if not isinstance(component, str) or component not in COMPONENT_REGISTRY or not isinstance(props, dict):
        return None
    if component == "calculator":
        if props:
            return None
    elif component == "matrix_determinant":
        if set(props) - {"size"} or type(props.get("size", 3)) is not int or not 2 <= props.get("size", 3) <= 5:
            return None
        props = {"size": props.get("size", 3)}
    else:
        allowed = {"expression", "xMin", "xMax"} if component == "function_graph" else {"expression"}
        if set(props) - allowed or not valid_expression(props.get("expression", "x^2")):
            return None
        props = {"expression": props.get("expression", "x^2"), **({key: props.get(key, default)
                 for key, default in (("xMin", -10), ("xMax", 10))} if component == "function_graph" else {})}
        if component == "function_graph":
            if any(type(props[key]) not in (int, float) or not math.isfinite(props[key]) or abs(props[key]) > 10000
                   for key in ("xMin", "xMax")) or props["xMin"] >= props["xMax"]:
                return None
    return {"type": "interactive_response", "component": component, "version": 1, "props": props}


# Semantic request frames. The component noun alone never decides: what the user wants DELIVERED does.
# An artifact frame ("calculator code in C", "a calculator app using React", "how to build a calculator")
# asks for text/code about the tool; a tool frame ("open a calculator", "give me a calculator here")
# asks to use the tool; a mixed frame additionally asks for an explanation/answer, so the model answers
# and the component is attached instead of replacing the answer.
_COMPONENT_NOUN = r"(?:calculator|calc|graph(?:ing)?(?: tool)?|plot(?:ter)?|matrix|determinant|derivative|differentiation|integral|integration)"
_ARTIFACT_NOUN = (r"(?:code|coding|program(?:me)?s?|script|source|implementation|function|method|class|module|app|"
                  r"application|project|algorithm|logic|pseudo-?code|snippet|tutorial|website|web ?page|gui|ui|"
                  r"interface|api|library|repo(?:sitory)?|assignment|homework|syntax|compiler)")
_TECHNOLOGY = (r"(?:c\+\+|c#|\bc\b|python|java(?!script)|javascript|\bjs\b|typescript|html|css|react|vue|angular|kotlin|swift|"
               r"golang|\bgo\b|rust|php|ruby|dart|flutter|tkinter|pygame|swing|android studio|c programming|"
               r"programming(?: language)?|\bin c\b)")
_REQUEST_VERB = (r"\b(?:give|show|open|launch|start|bring up|pull up|display|provide|add|use|need|want|"
                 r"can i (?:get|have|use)|let me use|put)\b")
_EXPLANATION = (r"\b(?:explain|why|how (?:do|does|did|can|is|are)|what (?:is|are|does)|teach|describe|difference|prove|"
                r"derive|solve|step[- ]by[- ]step|walk me through|meaning|intuition|help me understand)\b")


def _artifact_frame(text):
    """True when the component noun is the topic of a requested artifact, not the tool to open."""
    for noun in re.finditer(_COMPONENT_NOUN, text):
        window = text[max(0, noun.start() - 48):noun.end() + 60]
        if re.search(rf"\b{_ARTIFACT_NOUN}\b", window) or re.search(_TECHNOLOGY, window):
            return True
    if re.search(rf"\b(?:how (?:do i|to|can i|would i)|steps to)\s+(?:make|build|create|code|write|implement|design|program)\b", text):
        return True
    if re.search(rf"\b(?:write|code|implement|develop|program)\b(?:\W+\w+){{0,4}}\W+(?:a |an |the )?{_COMPONENT_NOUN}", text):
        return True
    return False


def _tool_frame(text):
    """Request verb whose object is the component itself (only determiners/adjectives in between)."""
    if re.fullmatch(rf"\s*(?:an? |the )?(?:simple |basic |interactive )?{_COMPONENT_NOUN}(?: please| pls| now| here)?\s*[.!?]?\s*", text):
        return True
    return bool(re.search(
        rf"{_REQUEST_VERB}(?:\s+(?:me|us|you))?(?:\s+(?:a|an|the|this|that|my|your|some|simple|basic|quick|"
        rf"interactive|small|working|standard|editable|little|new))*\s+{_COMPONENT_NOUN}\b", text))


def interactive_intent(text):
    """Classify a message as {'schema', 'mode'} or None.

    mode 'component_only': the user only wants to use a component.
    mode 'attach': the user also wants an explanation/answer — the model answers and the component is attached.
    """
    text = str(text or "").strip().lower()
    if len(text) > 1600 or re.search(r"\b(?:don't|do not|dont|without|remove|hide|stop|never)\b", text):
        return None
    if re.search(r"\b(?:broken|bug|problem|wrong|report|not working|doesn't work)\b", text):
        return None
    if _artifact_frame(text):
        return None
    graph = re.search(r"\b(?:plot|graph)\b", text)
    if not (_tool_frame(text) or graph):
        return None
    schema = requested_interactive_response(text, _frames_checked=True)
    if schema is None:
        return None
    residual = re.sub(rf"{_REQUEST_VERB}(?:\s+\w+){{0,4}}?\s+{_COMPONENT_NOUN}\b", " ", text)
    mixed = bool(re.search(_EXPLANATION, residual)) or bool(re.search(r"\d\s*[-+*/x×^]\s*\d", residual))
    return {"schema": schema, "mode": "attach" if mixed else "component_only"}


def requested_interactive_response(text, _frames_checked=False):
    text = str(text or "").strip().lower()
    if not _frames_checked:
        intent = interactive_intent(text)
        return intent["schema"] if intent else None
    request = _tool_frame(text)
    graph = re.search(r"\b(?:plot|graph)\b", text)
    if graph:
        component, props = "function_graph", {}
        candidate = re.sub(r"^(?:\s*(?:of|for|y\s*=))?\s*", "", text[graph.end():]).strip().rstrip('.?!')
        if valid_expression(candidate):
            props["expression"] = candidate
        elif not (request and re.search(r"\b(?:calculator|function graph|graphing tool)\b", text)):
            return None
    elif re.search(r"\b(?:differentiation|derivative)\b", text):
        component, props = "differentiation_calculator", {}
    elif re.search(r"\b(?:integration|integral)\b", text):
        component, props = "integration_calculator", {}
    elif re.search(r"\b(?:matrix|determinant)\b", text):
        component, props = "matrix_determinant", {"size": 3}
    elif re.search(r"\bcalculator\b", text):
        component, props = "calculator", {}
    else:
        return None
    return validate_interactive_response({"type": "interactive_response", "component": component, "version": 1, "props": props})


def sanitize_interactive_metadata(value):
    """Keep unrelated capability metadata; retain only validated component data."""
    if not isinstance(value, dict):
        return {}
    result = dict(value)
    for key in ("interactive_response", "interactiveResponse"):
        if key in result:
            schema = validate_interactive_response(result[key])
            if schema is None:
                result.pop(key)
            else:
                result[key] = schema
    return result
