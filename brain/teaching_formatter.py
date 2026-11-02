"""Formatter for Language Learning and Pronunciation responses."""

class TeachingResponseFormatter:
    """Instructs the LLM to output structured JSON for teaching responses."""

    @classmethod
    def get_instruction(cls) -> str:
        """System prompt instruction to activate JSON teaching structure."""
        return (
            "LANGUAGE TEACHING & PRONUNCIATION INSTRUCTION:\n"
            "If the user explicitly asks you to teach them a language, translate a phrase for learning, or explain pronunciation, "
            "you MUST output your response strictly as a JSON object (without markdown wrappers like ```json) following this exact schema:\n"
            "{\n"
            "  \"native_text\": \"The phrase in its native script (e.g., আপনি কেমন আছেন?)\",\n"
            "  \"romanization\": \"Romanized transliteration (e.g., Apni kemon achhen?)\",\n"
            "  \"meaning\": \"Meaning/translation (e.g., How are you?)\",\n"
            "  \"explanation\": \"Brief teaching explanation or context\",\n"
            "  \"examples\": [\"Example 1\", \"Example 2\"],\n"
            "  \"speech_segments\": [\n"
            "    {\"kind\": \"native_phrase\", \"text\": \"native script phrase here\", \"language\": \"target language\", \"speak\": true, \"order\": 0},\n"
            "    {\"kind\": \"romanization\", \"text\": \"romanized phrase here\", \"language\": \"romanized target language\", \"speak\": false, \"order\": 1},\n"
            "    {\"kind\": \"meaning\", \"text\": \"meaning here\", \"language\": \"explanation language\", \"speak\": true, \"order\": 2},\n"
            "    {\"kind\": \"explanation\", \"text\": \"explanation here\", \"language\": \"explanation language\", \"speak\": true, \"order\": 3},\n"
            "    {\"kind\": \"example\", \"text\": \"useful example content without duplicate Roman transliteration\", \"language\": \"mixed if needed\", \"speak\": true, \"order\": 4}\n"
            "  ]\n"
            "}\n\n"
            "CRITICAL ROMANIZATION SPEECH RULE:\n"
            "- Use `kind` (not an unstructured label), `text`, `language`, `speak`, and consecutive `order` on EVERY segment.\n"
            "- The speech plan MUST cover every useful visible native phrase, meaning, explanation, and example exactly once. Never truncate or omit the remainder of a visible sentence.\n"
            "- \"romanization\" segments MUST have `\"speak\": false` by default so they are not read aloud (since the native phrase is already spoken).\n"
            "- Only set `\"speak\": true` for romanization if the user EXPLICITLY asks you to 'read the Roman spelling' or similar.\n"
            "- \"native_phrase\" segments MUST have `\"speak\": true`.\n"
            "- \"meaning\", \"explanation\", and useful \"example\" segments default to `\"speak\": true`. For an example like `নমস্কার (Nomaskar) - Hello`, speak the native phrase and `Hello`, but not the duplicate Roman rendering.\n"
            "- Explicit user speech instructions override these defaults. For example, if the user says not to read the meaning, include its segment with `\"speak\": false`; if the user asks only for native pronunciation, all non-native segments must be false.\n"
            "For normal conversational chat in another language (e.g., 'Let's talk in Bengali'), DO NOT use this JSON structure. Just respond naturally in normal text/markdown."
        )
