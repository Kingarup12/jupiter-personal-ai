"""Validate provider-owned Search evidence before publishing current web answers."""
from urllib.parse import quote, urlsplit

from core.provider_contracts import AIRequest, FailureClass, NormalizedFailure, ProviderFailure
from services.task_preferences import WEB_TASKS


def requires_grounding(request: AIRequest) -> bool:
    return request.task_type in WEB_TASKS or "web_search" in request.required_capabilities


def grounded_content(response):
    candidates = getattr(response, "candidates", None) or []
    metadata = getattr(candidates[0], "grounding_metadata", None) if candidates else None
    chunks = getattr(metadata, "grounding_chunks", None) or []
    sources = {}
    for index, chunk in enumerate(chunks):
        web = getattr(chunk, "web", None)
        uri = str(getattr(web, "uri", "") or "")
        try:
            parsed = urlsplit(uri)
            valid = parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid or any(ord(char) < 32 for char in uri):
            continue
        sources[index] = {"url": quote(uri, safe=":/?#%=&+@,;~!$*.-_"),
                          "title": str(getattr(web, "title", "") or parsed.hostname)[:200]}
    text = str(getattr(response, "text", "") or "")
    supports = getattr(metadata, "grounding_supports", None) or []
    insertions = {}
    for support in supports:
        end = getattr(getattr(support, "segment", None), "end_index", None)
        indices = getattr(support, "grounding_chunk_indices", None) or []
        if isinstance(end, int) and 0 < end <= len(text):
            for index in indices:
                if index in sources:
                    insertions.setdefault(end, set()).add(index)
    # Tool configuration alone is not evidence. Fail closed if no claim has
    # actual retrieval support, so fallback cannot masquerade as live search.
    if not sources or not insertions:
        raise ProviderFailure(NormalizedFailure(
            FailureClass.UNSUPPORTED, retryable=False,
            safe_message="Verified web sources were unavailable. Please try the search again.",
        ))
    for end in sorted(insertions, reverse=True):
        links = " ".join(f"[{index + 1}]({sources[index]['url']})" for index in sorted(insertions[end]))
        text = text[:end] + " " + links + text[end:]
    # Retain structured evidence; never render provider HTML into the chat DOM.
    return text, {"sources": list(sources.values()), "grounded": True,
                  "search_entry_point": getattr(getattr(metadata, "search_entry_point", None), "rendered_content", None)}
