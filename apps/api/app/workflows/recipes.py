"""Ready-made automations a shop owner can switch on without writing anything.

Each recipe is a plain-words sentence ("when this happens, do that"), at most one or two simple
questions, and the definition the workflow engine (engine.py) runs. Owners never see the definition.
A recipe only does things that work for every workspace as it is: add a task to the Tickets page,
ask for a call-back, or write a note on a customer. Nothing here sends a message to a customer.
"""

from dataclasses import dataclass, field
from typing import Any

from packages.database.models import WorkflowTrigger


class RecipeError(Exception):
    """Something the person choosing the recipe can fix, in plain words."""


@dataclass(frozen=True)
class Choice:
    value: str
    label: str


@dataclass(frozen=True)
class Question:
    key: str
    label: str
    choices: tuple[Choice, ...]
    default: str


@dataclass(frozen=True)
class Recipe:
    id: str
    title: str
    when: str
    then: str
    trigger: WorkflowTrigger
    questions: tuple[Question, ...] = field(default_factory=tuple)
    nodes: Any = None  # a function (answers) -> list of nodes


def _missed_call(a: dict[str, str]) -> list[dict]:
    return [
        {
            "key": "nobody_answered",
            "type": "condition",
            "field": "status",
            "op": "eq",
            "value": "no_answer",
        },
        {
            "key": "ask_for_callback",
            "type": "action",
            "action": "schedule_callback",
            "params": {"requested_time": a["when"], "note": "They did not pick up our call"},
        },
    ]


def _failed_call(a: dict[str, str]) -> list[dict]:
    return [
        {
            "key": "call_failed",
            "type": "condition",
            "field": "status",
            "op": "eq",
            "value": "failed",
        },
        {
            "key": "open_ticket",
            "type": "action",
            "action": "create_ticket",
            "params": {
                "subject": "A call could not connect. Please check the number and try again.",
                "priority": a["priority"],
            },
        },
    ]


def _new_customer(a: dict[str, str]) -> list[dict]:
    return [
        {
            "key": "say_hello_task",
            "type": "action",
            "action": "create_ticket",
            "params": {
                "subject": "New customer. Say hello and ask what they need.",
                "priority": a["priority"],
                "customer_id": "{{customer_id}}",
            },
        }
    ]


def _note_every_call(a: dict[str, str]) -> list[dict]:
    return [
        {
            "key": "write_note",
            "type": "action",
            "action": "add_customer_note",
            "params": {"note": "A call with this customer finished. Open Calls for the summary."},
        }
    ]


_PRIORITY = (Choice("low", "Normal"), Choice("medium", "Important"), Choice("high", "Urgent"))

RECIPES: tuple[Recipe, ...] = (
    Recipe(
        id="missed_call_callback",
        title="Call back people who did not pick up",
        when="A call to a customer ends and nobody answers",
        then="A call-back task appears in your Tickets so someone tries again",
        trigger=WorkflowTrigger.CALL_ENDED,
        questions=(
            Question(
                "when",
                "When should someone try again?",
                (
                    Choice("later today", "Later today"),
                    Choice("tomorrow morning", "Tomorrow morning"),
                    Choice("in two days", "In two days"),
                ),
                "later today",
            ),
        ),
        nodes=_missed_call,
    ),
    Recipe(
        id="failed_call_ticket",
        title="Flag calls that could not connect",
        when="A call fails, for example a wrong or switched-off number",
        then="A task appears in your Tickets so you can fix the number",
        trigger=WorkflowTrigger.CALL_ENDED,
        questions=(Question("priority", "How important is it?", _PRIORITY, "medium"),),
        nodes=_failed_call,
    ),
    Recipe(
        id="new_customer_hello",
        title="Say hello to every new customer",
        when="Someone calls you for the first time",
        then="A task appears in your Tickets to welcome them and ask what they need",
        trigger=WorkflowTrigger.CUSTOMER_CREATED,
        questions=(Question("priority", "How important is it?", _PRIORITY, "low"),),
        nodes=_new_customer,
    ),
    Recipe(
        id="note_every_call",
        title="Keep a note after every call",
        when="Any call ends",
        then="A short note is added to that customer's profile, so the history is always complete",
        trigger=WorkflowTrigger.CALL_ENDED,
        nodes=_note_every_call,
    ),
)

_BY_ID = {r.id: r for r in RECIPES}


def get_recipe(recipe_id: str) -> Recipe:
    try:
        return _BY_ID[recipe_id]
    except KeyError as exc:
        raise RecipeError("We don't have that automation.") from exc


def clean_answers(recipe: Recipe, given: dict[str, str]) -> dict[str, str]:
    """Only the recipe's own questions, each one of its listed choices (default if skipped)."""
    answers: dict[str, str] = {}
    for q in recipe.questions:
        value = given.get(q.key, q.default)
        if value not in {c.value for c in q.choices}:
            raise RecipeError(f"Please choose one of the options for “{q.label}”.")
        answers[q.key] = value
    return answers


def build(recipe: Recipe, given: dict[str, str]) -> dict[str, Any]:
    """The definition the engine runs, tagged with its recipe so the dashboard can describe it."""
    answers = clean_answers(recipe, given)
    return {"recipe": recipe.id, "answers": answers, "nodes": recipe.nodes(answers)}


def catalogue() -> list[dict[str, Any]]:
    return [
        {
            "id": r.id,
            "title": r.title,
            "when": r.when,
            "then": r.then,
            "trigger": r.trigger.value,
            "questions": [
                {
                    "key": q.key,
                    "label": q.label,
                    "default": q.default,
                    "choices": [{"value": c.value, "label": c.label} for c in q.choices],
                }
                for q in r.questions
            ],
        }
        for r in RECIPES
    ]
