from core.handler import Handler
from core.utils import *

class TexlabBuild(Handler):
    name = "texlab_build"
    method = "textDocument/build"
    cancel_on_change = False
    send_document_uri = True

    def process_request(self) -> dict:
        return dict()

    def process_response(self, response) -> None:
        if response is not None and "status" in response:
            eval_in_emacs("lsp-bridge-texlab-build--callback", response["status"])
        else:
            eval_in_emacs("lsp-bridge-texlab-build--callback", 1)
