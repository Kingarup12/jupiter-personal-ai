"""Chat-completion wrapper used by the main conversation loop."""

from config.settings import TEMPERATURE
from core.model_manager import Task, ModelManager

def ask_llm(task: Task, messages, temperature=TEMPERATURE):
    """Send a full messages list and return the assistant reply based on the task."""
    from config.provider_settings import multi_provider_enabled

    if multi_provider_enabled():
        from core.backends import ChatResult
        from services.ai_gateway import AIGateway, legacy_request

        response = AIGateway().generate(legacy_request(task, messages, stream=False))
        usage = None
        if response.input_usage is not None or response.output_usage is not None:
            usage = {
                "input": response.input_usage,
                "output": response.output_usage,
                "total": (response.input_usage or 0) + (response.output_usage or 0),
            }
        return ChatResult(
            response.content,
            usage,
            model=response.actual_model,
            provider=response.provider,
        )

    from core.backends import get_backend

    backend = get_backend()
    
    manager = ModelManager()
    best_models = manager.get_best_models(task)
    
    return backend.chat(
        model=best_models,
        messages=messages,
        temperature=temperature,
    )


def ask_llm_stream(task: Task, messages, temperature=TEMPERATURE):
    """Yield native model/provider deltas using the normal Jupiter routing."""
    from config.provider_settings import multi_provider_enabled

    if multi_provider_enabled():
        from core.backends import ChatStreamChunk
        from services.ai_gateway import AIGateway, legacy_request

        def routed_stream():
            for event in AIGateway().stream(legacy_request(task, messages, stream=True)):
                yield ChatStreamChunk(
                    text=event.text,
                    event_type=event.event_type,
                    model=event.actual_model,
                    token_usage=event.usage,
                )

        return routed_stream()

    from core.backends import get_backend

    backend = get_backend()
    best_models = ModelManager().get_best_models(task)
    return backend.chat_stream(
        model=best_models,
        messages=messages,
        temperature=temperature,
    )
