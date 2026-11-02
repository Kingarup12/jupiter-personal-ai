import json
import re


class JSONExtractor:

    @staticmethod
    def extract(text: str):

        if not text:
            raise ValueError("Empty response")

        # ----------------------------------------
        # Remove markdown fences
        # ----------------------------------------

        text = re.sub(
            r"```(?:json)?",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = text.replace("```", "")
        text = text.strip()

        # ----------------------------------------
        # Fast path
        # ----------------------------------------

        try:
            return json.loads(text)

        except Exception:
            pass

        # ----------------------------------------
        # Remove boxed answers
        # ----------------------------------------

        text = re.sub(
            r"\\boxed\{.*?\}",
            "",
            text,
            flags=re.DOTALL,
        )

        # ----------------------------------------
        # Find every JSON object
        # ----------------------------------------

        decoder = json.JSONDecoder()

        for match in re.finditer(r"\{", text):

            start = match.start()

            try:

                obj, end = decoder.raw_decode(text[start:])

                if isinstance(obj, dict):

                    return obj

            except Exception:

                continue

        raise ValueError("No valid JSON object found.")