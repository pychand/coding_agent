

import json
import os
import openai
from typing import Callable
from tools import Toolbox,TOOL_SCHEMAS


SYSTEM_PROMPT = """You are Mini Cursor, a careful coding agent working inside the folder {workspace}. All paths are relative to that folder.
How to work:
1. Explore before you change anything: list_files, find_files, search_code, read_file. Use search_code to locate Python functions and classes by name instead of grep.
2. To change code, call write_file with the COMPLETE new file content. The user reviews a diff and may reject it.
3. Verify: after a change, run the tests with `pytest -q` when tests exist.
4. Use web_search only for facts that may be newer than your training data (latest versions, recent API changes).
5. Never try to read secrets (.env, keys) or work outside the workspace. If a tool returns an error, read it, adjust, and try a different approach.
Answer briefly: what you found, what you changed, and the result.

"""

MAX_STEPS = 12  # tool rounds per question; stops a confused model from looping forever

class CodingAgent:
    def __init__(self, workspace: str, model: str, approve: Callable[[str], bool],
                 on_step: Callable[[str, dict], None],
                 on_text: Callable[[str], None] | None = None):
        base_url = os.getenv("OPENAI_API_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        self.client = openai.OpenAI(base_url=base_url, api_key=os.getenv("OPENAI_API_KEY"))
        self.model = model
        self.tools = Toolbox(workspace, approve)
        self.instructions = SYSTEM_PROMPT.format(workspace=self.tools.root.name)
        self.last_response_id = None  # conversation memory lives on OpenAI's side
        self.on_step = on_step
        self.on_text = on_text
        self.streamed_answer = False

    def new_conversation(self) -> None:
        self.last_response_id = None

    def call_model(self, input_items, previous_id, allow_tools: bool = True, stream: bool = True):
        # instructions are NOT carried over by previous_response_id, so send them every time
        result = self.client.responses.create(
            model=self.model,
            instructions=self.instructions,
            input=input_items,
            tools=TOOL_SCHEMAS,
            tool_choice="auto" if allow_tools else "none",
            previous_response_id=previous_id,
            stream=stream,
        )
        if not stream:
            return result

        response = None
        for event in result:
            event_type = getattr(event, "type", "")
            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    self.streamed_answer = True
                    self.on_step("text_delta", {"delta": delta})
                    if self.on_text:
                        self.on_text(delta)
            elif event_type == "response.completed":
                response = getattr(event, "response", None)

        if response is None:
            get_final_response = getattr(result, "get_final_response", None)
            if callable(get_final_response):
                response = get_final_response()
        if response is None:
            raise RuntimeError("Streaming response ended without a final response.")
        return response

    def ask(self, user_message: str) -> str:

        """"Handle one user message: loop until the model gives a final answer."""
        self.streamed_answer = False
        response = self.call_model(user_message,self.last_response_id)

        for _ in range(MAX_STEPS):
            calls = [item for item in response.output if item.type == "function_call"]


            if not calls:
                break

            outputs = []
            for call in calls:
                args = json.loads(call.arguments or "{}")
                if call.name == "web_search":
                    self.on_step("web_search", {"query": args["query"]})
                self.on_step("tool_call", {"name": call.name, "args": args})
                result = self.tools.run(call.name, args)
                self.on_step("tool_result", {"name": call.name, "result": result})
                outputs.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": result,
                })

            response = self.call_model(outputs, response.id, allow_tools=True)

        else:

            calls = [item for item in response.output if item.type == "function_call"]
            outputs = [
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": "Step limit reached. Summarise progress without calling more tools.",
                }
                for call in calls
            ]
            if outputs:
                response = self.call_model(outputs, response.id, allow_tools=False)

        self.last_response_id = response.id
        return response.output_text

