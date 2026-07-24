from core.handler import Handler
from core.utils import *

class TexlabForwardSearch(Handler):
    name = "texlab_forward_search"
    method = "textDocument/forwardSearch"
    cancel_on_change = False
    send_document_uri = True

    def process_request(self, position) -> dict:
        return dict(position=position)

    def process_response(self, response) -> None:
        if response is not None and "status" in response:
            eval_in_emacs("lsp-bridge-texlab-forward-search--callback", response["status"])
        else:
            eval_in_emacs("lsp-bridge-texlab-forward-search--callback", 1)
