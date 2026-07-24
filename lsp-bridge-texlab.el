;;; lsp-bridge-texlab.el --- Texlab protocol for lsp-bridge   -*- lexical-binding: t; -*-

;;; Commentary:
;;
;; Texlab protocol for lsp-bridge: build and forward search support.
;;

;;; Code:

(defun lsp-bridge-texlab-build ()
  "Build the current LaTeX document using texlab."
  (interactive)
  (lsp-bridge-call-file-api "texlab_build"))

(defun lsp-bridge-texlab-build--callback (status)
  "Handle the build response with STATUS code.
0=Success, 1=Error, 2=Failure, 3=Cancelled."
  (message "texlab build: %s"
           (pcase status
             (0 "Success")
             (1 "Error")
             (2 "Failure")
             (3 "Cancelled")
             (_ (format "Unknown status %s" status)))))

(defun lsp-bridge-texlab-forward-search ()
  "Forward search from the current cursor position to the PDF viewer."
  (interactive)
  (lsp-bridge-call-file-api "texlab_forward_search" (lsp-bridge--position)))

(defun lsp-bridge-texlab-forward-search--callback (status)
  "Handle the forward search response with STATUS code.
0=Success, 1=Error, 2=Failure, 3=Unconfigured."
  (message "texlab forward search: %s"
           (pcase status
             (0 "Success")
             (1 "Error")
             (2 "Failure")
             (3 "Unconfigured")
             (_ (format "Unknown status %s" status)))))

(provide 'lsp-bridge-texlab)

;;; lsp-bridge-texlab.el ends here
