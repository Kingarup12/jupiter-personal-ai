"""Terminal entry point for Jupiter.

Run from the repository root with ``python -m app.main``.
"""

import sys

from brain.context_builder import ContextBuilder
from brain.conversation import Conversation
from brain.conversation_understanding import ConversationUnderstanding
from brain.dialogue_state import DialogueStateTracker
from brain.language_adapter import LanguageAdapter
from brain.llm import ask_llm
from core.model_manager import Task
from brain.reply_planner import ReplyPlanner
from brain.response_rewriter import ResponseRewriter
from brain.response_validator import ResponseValidator
from config.settings import APP_NAME, USER_ID, VERSION
from core.logger import Logger
from memory.service import MemoryService


def _configure_terminal_output():
    """Keep Unicode replies from crashing on legacy Windows code pages."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def main():
    _configure_terminal_output()
    print("===================================")
    print(f"      Welcome to {APP_NAME} v{VERSION}")
    print("===================================\n")

    Logger.info(f"Jupiter started for user '{USER_ID}'.")

    conversation = Conversation()
    memory_service = MemoryService()
    context_builder = ContextBuilder(memory_service.manager)
    understanding_engine = ConversationUnderstanding()
    dialogue_state = DialogueStateTracker()

    try:
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if user_input.lower() in {"exit", "quit"}:
                break

            if not user_input:
                continue

            conversation.add("user", user_input)

            # ============================================
            # STEP 1: UNDERSTAND the user's message
            # ============================================
            understanding = understanding_engine.understand(
                user_input, conversation.messages
            )
            dialogue_state.update_on_user_message(user_input, understanding)

            # ============================================
            # STEP 2: EXECUTIVE PLAN (memory + social)
            # ============================================
            memories = memory_service.get_all()
            profile_memories = memory_service.get_personality_memories()
            executive_plan = context_builder.plan_turn(
                user_input,
                conversation.messages,
                memories,
                profile_memories,
            )
            conversation.observe(executive_plan)

            # ============================================
            # STEP 3: MEMORY PROCESSING
            # Only store if understanding says it's worth storing
            # ============================================
            should_store = (
                executive_plan.should_consider_memory
                and understanding["should_store_memory"]
            )
            try:
                memory_service.process(
                    user_input,
                    recent_conversation=conversation.messages[:-1],
                    should_consider_memory=should_store,
                )
            except Exception as error:
                Logger.warning(f"Memory processing skipped for this turn: {error}")

            # Re-plan after memory processing
            executive_plan = context_builder.plan_turn(
                user_input,
                conversation.messages,
                memory_service.get_all(),
                memory_service.get_personality_memories(),
            )
            conversation.observe(executive_plan)

            # ============================================
            # STEP 4: BUILD CONTEXT
            # ============================================
            try:
                Logger.info("Building context")
                messages = context_builder.build(
                    user_message=user_input,
                    conversation_messages=conversation.messages,
                    memories=memory_service.get_all(),
                    profile_memories=memory_service.get_personality_memories(),
                    executive_plan=executive_plan,
                )

                # Inject dialogue understanding instruction
                dialogue_instruction = understanding_engine.get_dialogue_instruction(understanding)
                if dialogue_instruction:
                    messages.append({"role": "system", "content": dialogue_instruction})

                # Inject dialogue state instruction (boredom, question cooldown)
                state_instruction = dialogue_state.get_instruction()
                if state_instruction:
                    messages.append({"role": "system", "content": state_instruction})

                # Inject dialogue manager instruction (confusion, etc.)
                dm_instruction = conversation.dialogue_manager.get_instruction()
                if dm_instruction:
                    messages.append({"role": "system", "content": dm_instruction})

                # Inject language adapter instruction (prevent formal Hindi)
                messages.append({"role": "system", "content": LanguageAdapter.get_instruction()})

                # Inject reply planning instruction (think before speaking)
                messages.append({"role": "system", "content": ReplyPlanner.get_planning_instruction()})

                # ============================================
                # STEP 5: GENERATE
                # ============================================
                Logger.info("Sending message to LLM")
                raw_reply = ask_llm(Task.MAIN_CHAT, messages)

                # ============================================
                # STEP 6: POST-PROCESS
                # ============================================
                # Strip internal thought block
                reply = ReplyPlanner.strip_thought(raw_reply)

                # Fix formal Hindi and gender errors
                reply = LanguageAdapter.fix_response(reply)

                # Check for semantic repetition
                recent_assistant = [
                    msg["content"] for msg in conversation.messages[-6:]
                    if msg["role"] == "assistant"
                ]
                reply, was_broken = ResponseRewriter.rewrite(reply, recent_assistant)
                if was_broken:
                    Logger.warning("Response rewriter detected broken output. Using fallback.")
                    reply = ResponseValidator.get_fallback("repetitive", user_input)

                # Validate
                is_valid, reason = ResponseValidator.validate(reply, conversation.messages, user_input)
                if not is_valid:
                    Logger.warning(f"Response validation failed ({reason}). Triggering fallback.")
                    reply = ResponseValidator.get_fallback(reason, user_input)

            except Exception as error:
                Logger.error(f"Jupiter could not complete that turn: {error}")
                print(
                    "\nJupiter: Mujhe local AI service se connect karne mein "
                    "problem aa rahi hai. Ollama aur configured models check kar lo.\n"
                )
                continue

            conversation.add("assistant", reply)
            dialogue_state.update_on_assistant_message(reply)
            context_builder.defer_curiosity(user_input)
            print(f"\nJupiter: {reply}\n")
    finally:
        try:
            memory_service.finalize_session(
                conversation.messages,
                active_topic=conversation.state.active_topic,
                emotion=conversation.state.user_emotion,
            )
        except Exception as error:
            Logger.warning(f"Session replay was not saved: {error}")
        finally:
            memory_service.close()
        Logger.info("Jupiter closed")
        print("\nJupiter: Byee, take care ✨")


if __name__ == "__main__":
    main()
