from core.handler import Handler
from core.utils import *


class LeanPlainTermGoal(Handler):
    name = "lean_plain_term_goal"
    method = "$/lean/plainTermGoal"
    cancel_on_change = True

    def process_request(self, position) -> dict:
        return dict(position=position)

    def process_response(self, response) -> None:
        if response is not None and "goal" in response:
            eval_in_emacs("lsp-bridge-lean4--update-term-goal", response["goal"])
        else:
            eval_in_emacs("lsp-bridge-lean4--update-term-goal", "")
