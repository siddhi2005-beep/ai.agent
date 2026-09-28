import json
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("aftermath.llm")


class LLMService:
    def __init__(self, client: Any = None):
        self.client = client
        self.available = client is not None
        self.last_error: Optional[str] = None
        self.model = os.getenv("AFTERMATH_GEMINI_MODEL", "gemini-2.5-flash")
        if self.client is None and os.getenv("GEMINI_API_KEY"):
            try:
                from google import genai
                self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options={"timeout": 10000})
                self.available = True
            except Exception as exc:
                logger.warning("Gemini initialization failed: %s", exc)
                self.last_error = "Gemini client could not be initialized."

    def runtime_status(self) -> dict:
        return {"mode": "LIVE_GEMINI" if self.available else "RULE_BASED_EVIDENCE", "available": self.available, "model": self.model if self.available else None, "message": "Structured Gemini inference enabled." if self.available else "Gemini is not configured; evidence-only rules are active."}

    def generate_json(self, prompt: str, system_instruction: Optional[str] = None) -> Optional[dict]:
        if not self.client:
            return None
        try:
            from google.genai import types
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema={
                    "type": "OBJECT",
                    "properties": {"inferences": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}, "evidence": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"source_id": {"type": "STRING"}, "quote": {"type": "STRING"}}, "required": ["source_id", "quote"]}}}, "required": ["text", "evidence"]}}},
                    "required": ["inferences"],
                },
                system_instruction=system_instruction or "You are an engineering incident analyst. Do not invent historical facts. Every inference must cite exact evidence quotes from the supplied sources. Return only the requested schema.",
                temperature=0.1,
                max_output_tokens=1200,
            )
            response = self.client.models.generate_content(model=self.model, contents=prompt, config=config)
            if response and response.text:
                parsed = json.loads(response.text)
                if isinstance(parsed, dict) and isinstance(parsed.get("inferences"), list):
                    self.last_error = None
                    return parsed
            self.last_error = "Gemini returned an invalid structured response."
        except Exception as exc:
            logger.exception("Gemini structured inference failed: %s", exc)
            self.last_error = "Gemini analysis is temporarily unavailable."
        return None


llm_service = LLMService()
