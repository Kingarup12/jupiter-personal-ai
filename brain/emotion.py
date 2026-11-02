"""Small, local emotion cues used to choose an appropriate response tone."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EmotionState:
    label: str = "neutral"
    intensity: str = "low"

    @property
    def needs_care(self) -> bool:
        return self.label in {"sad", "grieving", "stressed", "angry", "confused"}


class EmotionDetector:
    """Conservative keyword detector; it never diagnoses the user."""

    CUES = {
        "grieving": {"died", "passed away", "funeral", "bereavement", "condolence", "guzar gaye", "chal base"},
        "sad": {
            "sad", "crying", "heartbroken", "lonely", "depressed", "miss him", "miss her",
            "dukhi", "udaas", "rona aa raha", "bohot bura", "bahut bura",
        },
        "stressed": {
            "stressed", "overwhelmed", "anxious", "panic", "burned out", "burnt out",
            "tension", "tensed", "pareshan", "bahut tension", "bohot tension",
        },
        "angry": {
            "angry", "furious", "pissed", "annoyed", "frustrated", "hate this",
            "gussa", "bahut gussa", "chidh", "irritated",
        },
        "confused": {"confused", "lost", "don't understand", "dont understand", "stuck", "samajh nahi aa raha"},
        "excited": {
            "excited", "thrilled", "can't wait", "cant wait", "yay", "finally did it",
            "bohot excited", "bahut excited", "maza aa gaya", "let's go",
        },
        "happy": {
            "happy", "happiest", "proud", "great news", "celebrate", "won",
            "khush", "bohot khush", "bahut khush", "mast", "zabardast",
        },
        "playful": {
            "haha", "lol", "lmao", "rofl", "hehe", "xd", "pagal", "pagli",
            "mazak", "chal chal", "hatt", "are baap re", "arre baap",
            "oho", "kya baat", "tu bhi na",
        },
        "nostalgic": {
            "yaad aa rahi", "yaad aata", "miss karta", "miss karti",
            "those days", "purane din", "bachpan", "school time",
        },
        "sarcastic": {
            "wow great", "bahut acha", "bohot sahi", "wah wah",
            "kya baat hai", "amazing yaar", "haan bilkul",
        },
    }

    def detect(self, message: str) -> EmotionState:
        text = message.lower()
        for label, cues in self.CUES.items():
            if any(cue in text for cue in cues):
                intensity = "high" if text.count("!") >= 2 or label == "grieving" else "medium"
                return EmotionState(label, intensity)
        return EmotionState()

