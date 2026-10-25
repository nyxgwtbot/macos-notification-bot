"""LLM (Ollama) notification classifier with tolerant JSON parsing and non-fatal fallback."""

import json
import logging
import urllib.request
from typing import Optional


def clean_json_text(text: str) -> str:
    """Tolerant cleanup: strip markdown fences and extract first {...} block."""
    s = text.strip()
    # Strip ```json or ``` fences if present
    if s.startswith("```"):
        lines = s.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    # Find first { and last }
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start : end + 1]
    return s


logger = logging.getLogger("notification_bot")


class LLMClassifier:
    """Classifies notifications using local Ollama model with tolerant JSON parsing and non-fatal fallback."""

    SYSTEM_PROMPT = (
        "You are a notification classification engine.\n"
        "Given the notification app, title, subtitle, and body, return a valid JSON object with EXACTLY three keys:\n"
        "- category: string, one of [\"message\", \"alert\", \"promo\", \"system\", \"other\"]\n"
        "- urgency: string, one of [\"high\", \"medium\", \"low\"]\n"
        "- summary: string, concise 1-sentence summary\n"
        "Return ONLY the JSON object. Do not wrap in explanation or conversational text."
    )

    # Re-exported for convenience/backward compatibility with tests.
    clean_json_text = staticmethod(clean_json_text)

    def __init__(
        self,
        url: str = "http://127.0.0.1:11434/api/chat",
        model: str = "glm-5.3-flash:cloud",
        timeout: float = 30.0,
    ):
        self.url = url
        self.model = model
        self.timeout = timeout

    def fallback(self, notif: dict) -> dict:
        """Non-fatal default classification."""
        summary = notif.get("title") or notif.get("body") or "Notification"
        return {
            "category": "other",
            "urgency": "low",
            "summary": summary[:120],
        }

    def classify(self, notif: dict) -> dict:
        """Call Ollama /api/chat with model and tolerant parsing."""
        user_content = (
            f"App: {notif.get('app', '')}\n"
            f"Title: {notif.get('title', '')}\n"
            f"Subtitle: {notif.get('subtitle', '')}\n"
            f"Body: {notif.get('body', '')}"
        )
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.url,
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                msg = resp_json.get("message", {})
                raw_content = msg.get("content", "")
                cleaned = clean_json_text(raw_content)
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict) and "category" in parsed and "urgency" in parsed:
                    category = str(parsed.get("category", "other")).lower().strip()
                    urgency = str(parsed.get("urgency", "low")).lower().strip()
                    summary = str(
                        parsed.get("summary") or notif.get("title") or notif.get("body") or "Notification"
                    )
                    return {
                        "category": category,
                        "urgency": urgency,
                        "summary": summary,
                    }
                logger.warning("LLM response missing expected keys: %s", raw_content)
                return self.fallback(notif)
        except Exception as e:
            logger.warning("LLM classification failed (%s); using fallback.", e)
            return self.fallback(notif)