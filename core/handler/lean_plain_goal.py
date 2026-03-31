from core.handler import Handler
from core.utils import *


class LeanPlainGoal(Handler):
    name = "lean_plain_goal"
    method = "$/lean/plainGoal"
    cancel_on_change = True

    def process_request(self, position) -> dict:
        return dict(position=position)

    def process_response(self, response) -> None:
        if response is not None and "goals" in response:
            eval_in_emacs("lsp-bridge-lean4--update-goals", response["goals"])
        else:
            eval_in_emacs("lsp-bridge-lean4--update-goals", [])
