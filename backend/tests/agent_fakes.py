"""A scripted stand-in for the Claude API, so guardrail tests run offline and can misbehave on purpose."""

import json
import re
from types import SimpleNamespace

import anthropic
import httpx2

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def timeout() -> Exception:
    return anthropic.APITimeoutError(request=REQUEST)


def server_error() -> Exception:
    return anthropic.InternalServerError(
        "overloaded", response=httpx2.Response(500, request=REQUEST), body=None
    )


def rate_limited() -> Exception:
    return anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=REQUEST), body=None)


def menu_from(kwargs: dict) -> list[dict]:
    """The approved menu the pipeline put in the first user message."""
    text = kwargs["messages"][0]["content"]
    start = text.index("Approved offer menu")
    return json.loads(text[text.index("[", start) : text.rindex("]") + 1])


def offers_from_haiku(kwargs: dict) -> list[dict]:
    text = kwargs["messages"][0]["content"]
    return json.loads(text[text.index("Offers:") + len("Offers:") :])


def good_message(offer: dict, name: str = "Simran") -> str:
    code = f" Use code {offer['code']} by {offer['valid_until']}." if offer.get("code") else ""
    return f"Hi {name}, here is {offer['label'].lower()} picked just for you.{code}"


def usage(inp: int = 900, out: int = 120):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0)


def tool_call(name: str, args: dict, call_id: str = "toolu_1", stop: str = "tool_use"):
    block = SimpleNamespace(type="tool_use", name=name, input=args, id=call_id)
    return SimpleNamespace(content=[block], stop_reason=stop, usage=usage(), model="claude-sonnet-5-5")


def submit(
    offer_id: str, value: float, message: str, reason: str = "best expected profit", call_id="toolu_s"
):
    return tool_call(
        "submit_decision",
        {"offer_id": offer_id, "value": value, "message": message, "reason": reason},
        call_id,
    )


def text_reply(text: str, stop: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=usage(300, 60),
        model="claude-haiku-5-5",
    )


class FakeLLM:
    """Plays back a script. Each step is a response, an exception, or a callable(kwargs) -> response."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        # snapshot: the pipeline keeps appending to the same messages list
        self.requests.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if not self.steps:
            raise AssertionError("FakeLLM script exhausted")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(kwargs) if callable(step) else step


def best_offer_submission(kwargs: dict):
    offer = menu_from(kwargs)[0]
    return submit(offer["offer_id"], offer["value"], good_message(offer))


def haiku_good(kwargs: dict):
    offer = offers_from_haiku(kwargs)[0]
    return text_reply(
        json.dumps({"offer_id": offer["offer_id"], "message": good_message(offer), "reason": "top"})
    )


def numbers_in(text: str) -> list[str]:
    return re.findall(r"\d[\d,]*", text)
