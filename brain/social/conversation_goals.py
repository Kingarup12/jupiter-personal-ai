"""Small, non-intrusive relationship goals for a personal assistant."""


def goals_for_stage(stage: str) -> list[str]:
    if stage == "NEW":
        return [
            "learn the user's preferred name",
            "understand what they want help with",
            "make the conversation feel welcoming",
        ]
    if stage == "KNOWN":
        return [
            "learn communication preferences naturally",
            "understand meaningful goals or ongoing projects when relevant",
            "be useful without interrogating the user",
        ]
    if stage == "REGULAR":
        return [
            "help with the current request",
            "remember relevant ongoing projects and preferences",
            "keep rapport warm without forcing personal questions",
        ]
    return [
        "help with the current request",
        "maintain natural rapport and occasional relevant callbacks",
        "never force personal questions",
    ]
