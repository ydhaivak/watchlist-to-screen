import json
import uuid
from pathlib import Path

import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tools import TOOLS, run_tool

# --- Config ---

SYSTEM_PROMPT = (
    "You are a movie discovery assistant. "
    "You help users explore Letterboxd watchlists and ratings, find where to stream films, "
    "and check what's currently in theaters. "
    "Do not call any tool for greetings, small talk, or questions you can answer directly. "
    "\n\n"
    "Tool usage:\n"
    "- get_letterboxd_watchlist: call when the user asks what a specific person wants to watch.\n"
    "- get_letterboxd_ratings: call when the user asks about one person's taste or viewing history.\n"
    "- compare_letterboxd_users: call when two Letterboxd usernames are in play and the user "
    "wants to find something to watch together or understand taste overlap.\n"
    "- find_in_theaters: call when the user asks what's playing or whether specific films "
    "are in theaters. Pass the film list from a watchlist or comparison if one is available. "
    "Format any showtimes_link as a markdown link so it is clickable.\n"
    "- find_where_to_watch: call when the user asks where to stream, rent, or buy films. "
    "Batch all films in a single call.\n"
    "- show_films: always call this as your final tool call before answering, "
    "passing only the films your answer actually mentions or recommends. "
    "For a full watchlist: pass all films if ≤ 100, otherwise the first 100.\n"
    "\n"
    "After compare_letterboxd_users, a compatibility card is shown automatically in the UI — "
    "do NOT repeat the numbers (score, counts, mean diff) in your text. "
    "Instead write 2–4 sentences about their taste overlap, then name 1–3 concrete picks "
    "for tonight and call find_where_to_watch + show_films as usual."
)
MAX_TOOL_ROUNDS = 6

# --- The Harness ---


def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model="vertex_ai/gemini-3.5-flash-lite",
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content, tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            args = json.loads(call.function.arguments)
            result = run_tool(call.function.name, args)
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process.
sessions: dict[str, list] = {}

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
