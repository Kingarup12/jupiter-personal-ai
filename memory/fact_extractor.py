from brain.fact_prompt import FACT_PROMPT
from core.model_manager import Task, ModelManager

from memory.fact import Fact

from core.logger import Logger
from core.llm import LLM
from core.json_extractor import JSONExtractor


class FactExtractor:

    # ---------------------------------
    # Extract Facts
    # ---------------------------------

    def extract(self, memory):

        Logger.info("Fact extractor started.")

        # The memory analyzer already returns atomic facts. Preserve those facts
        # instead of making a second model call that can fail or subtly alter a
        # user-provided detail.
        supplied_facts = []
        for item in memory.facts:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject", "")).strip()
            predicate = str(item.get("predicate", "")).strip()
            object_ = str(item.get("object", "")).strip()
            if subject and predicate and object_:
                supplied_facts.append(
                    Fact.create(
                        memory_id=memory.memory_id,
                        subject=subject,
                        predicate=predicate,
                        object=object_,
                        confidence=float(item.get("confidence", memory.certainty)),
                    )
                )

        if supplied_facts:
            Logger.success(f"{len(supplied_facts)} supplied facts preserved.")
            return supplied_facts

        prompt = f"""
Category:
{memory.category}

Topic:
{memory.topic}

Summary:
{memory.summary}
"""

        result = LLM.generate(

            model=ModelManager().get_best_models(Task.MEMORY_ANALYZER),

            system_prompt=FACT_PROMPT,

            user_prompt=prompt

        )

        try:

            data = JSONExtractor.extract(result)

            facts = []

            for item in data.get("facts", []):

                facts.append(

                    Fact.create(

                        memory_id=memory.memory_id,

                        subject=item["subject"],

                        predicate=item["predicate"],

                        object=item["object"],

                        confidence=item.get(
                            "confidence",
                            1.0
                        )

                    )

                )

            Logger.success(
                f"{len(facts)} facts extracted."
            )

            return facts

        except Exception as e:

            Logger.error("Fact extraction failed.")
            Logger.error(str(e))

            return []
