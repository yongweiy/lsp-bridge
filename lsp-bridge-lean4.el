;;; lsp-bridge-lean4.el --- Lean 4 goal view via lsp-bridge  -*- lexical-binding: t; -*-

;;; Commentary:
;;
;; Provides the interactive goal view (*Lean Goal* buffer) for lean4-mode
;; using lsp-bridge instead of lsp-mode.  This replaces the functionality
;; in lean4-info.el which depends on lsp-mode.
;;

;;; Code:

(require 'magit-section)

(defvar lean4-highlight-inaccessible-names)
(declare-function lean4-info-mode "lean4-mode")

;; ──────────────────────────────────────────────────────────────────
;; State
;; ──────────────────────────────────────────────────────────────────

(defconst lsp-bridge-lean4-info-buffer-name "*Lean Goal*")

(defvar lsp-bridge-lean4--goals nil
  "Current goals received from lean server.")

(defvar lsp-bridge-lean4--term-goal nil
  "Current term-level expected type from lean server.")

;; ──────────────────────────────────────────────────────────────────
;; Buffer management
;; ──────────────────────────────────────────────────────────────────

(defun lsp-bridge-lean4--ensure-info-buffer ()
  "Create the *Lean Goal* buffer if it does not exist."
  (unless (get-buffer lsp-bridge-lean4-info-buffer-name)
    (with-current-buffer (get-buffer-create lsp-bridge-lean4-info-buffer-name)
      (buffer-disable-undo)
      (magit-section-mode)
      (set-syntax-table (if (boundp 'lean4-syntax-table) lean4-syntax-table (syntax-table)))
      (setq buffer-read-only t))))

(defun lsp-bridge-lean4-toggle-info ()
  "Toggle the *Lean Goal* buffer."
  (interactive)
  (if-let ((window (get-buffer-window lsp-bridge-lean4-info-buffer-name)))
      (quit-window nil window)
    (lsp-bridge-lean4--ensure-info-buffer)
    (display-buffer lsp-bridge-lean4-info-buffer-name))
  (lsp-bridge-lean4--refresh))

(defun lsp-bridge-lean4--info-buffer-active-p ()
  "Return non-nil if the info buffer is visible and the source buffer is focused."
  (and (get-buffer-window lsp-bridge-lean4-info-buffer-name t)
       (eq (current-buffer) (window-buffer))
       (derived-mode-p 'lean4-mode)))

;; ──────────────────────────────────────────────────────────────────
;; Rendering
;; ──────────────────────────────────────────────────────────────────

(defun lsp-bridge-lean4--insert-highlighted (text)
  "Insert TEXT, highlighting inaccessible names if configured."
  (let ((begin (point)))
    (insert text)
    (when (and (boundp 'lean4-highlight-inaccessible-names)
               lean4-highlight-inaccessible-names)
      (let ((end (point-marker)))
        (goto-char begin)
        (while (re-search-forward "\\(\\sw+\\)✝\\([¹²³⁴-⁹⁰]*\\)" end t)
          (replace-match
           (propertize (concat (match-string-no-properties 1)
                               (match-string-no-properties 2))
                       'font-lock-face 'font-lock-comment-face)
           'fixedcase 'literal))
        (goto-char end)))))

(defun lsp-bridge-lean4--fontify-as-lean (text)
  "Fontify TEXT as lean4 code."
  (with-temp-buffer
    (insert text)
    (delay-mode-hooks (lean4-info-mode))
    (font-lock-ensure)
    (buffer-string)))

(defun lsp-bridge-lean4--insert-goal-text (text delimiter)
  "Insert TEXT fontified as lean4 code, followed by DELIMITER."
  (lsp-bridge-lean4--insert-highlighted
   (concat (lsp-bridge-lean4--fontify-as-lean text) delimiter)))

(defun lsp-bridge-lean4--mk-message-section (value caption messages buffer)
  "Insert a magit-section with id VALUE, heading CAPTION, containing MESSAGES."
  (when messages
    (magit-insert-section (magit-section value)
      (magit-insert-heading caption)
      (magit-insert-section-body
        (dolist (e messages)
          (let* ((range (plist-get e :range))
                 (start (plist-get range :start))
                 (ln (1+ (or (plist-get start :line) 0)))
                 (col (or (plist-get start :character) 0))
                 (msg (or (plist-get e :message) "")))
            (insert-text-button (format "%d:%d:" ln col)
                                'action (lambda (_btn)
                                          (when (buffer-live-p buffer)
                                            (pop-to-buffer buffer)
                                            (goto-char (point-min))
                                            (forward-line (1- ln))
                                            (forward-char col)))
                                'face 'magit-section-heading
                                'help-echo "mouse-2: visit this location")
            (lsp-bridge-lean4--insert-highlighted (concat "\n" msg "\n"))))))))

(defun lsp-bridge-lean4--redisplay ()
  "Redisplay the *Lean Goal* buffer with current goals, term-goal, and diagnostics."
  (when (lsp-bridge-lean4--info-buffer-active-p)
    (let* ((inhibit-read-only t)
           (buffer (current-buffer))
           (cur-line (1- (line-number-at-pos nil t)))
           (diags (when (bound-and-true-p lsp-bridge-diagnostic-records)
                    lsp-bridge-diagnostic-records))
           errors-above errors-here errors-below)
      ;; Partition diagnostics by position relative to cursor
      (dolist (d diags)
        (let* ((range (plist-get d :range))
               (start-line (or (plist-get (plist-get range :start) :line) 0))
               (end-line (or (plist-get (plist-get range :end) :line) 0)))
          (cond
           ((< end-line cur-line) (push d errors-above))
           ((<= start-line cur-line) (push d errors-here))
           (t (push d errors-below)))))
      (setq errors-above (nreverse errors-above))
      (setq errors-here (nreverse errors-here))
      (setq errors-below (nreverse errors-below))
      (with-current-buffer lsp-bridge-lean4-info-buffer-name
        (erase-buffer)
        (magit-insert-section (magit-section 'root)
          ;; Goals
          (when lsp-bridge-lean4--goals
            (magit-insert-section (magit-section 'goals)
              (magit-insert-heading "Goals:")
              (magit-insert-section-body
                (if (> (length lsp-bridge-lean4--goals) 0)
                    (seq-doseq (g lsp-bridge-lean4--goals)
                      (magit-insert-section (magit-section)
                        (lsp-bridge-lean4--insert-goal-text g "\n\n")))
                  (insert "goals accomplished\n\n")))))
          ;; Term goal (expected type)
          (when (and lsp-bridge-lean4--term-goal
                     (not (string-empty-p lsp-bridge-lean4--term-goal)))
            (magit-insert-section (magit-section 'term-goal)
              (magit-insert-heading "Expected type:")
              (magit-insert-section-body
                (lsp-bridge-lean4--insert-goal-text lsp-bridge-lean4--term-goal "\n"))))
          ;; Diagnostics
          (lsp-bridge-lean4--mk-message-section 'errors-here "Messages here:" errors-here buffer)
          (lsp-bridge-lean4--mk-message-section 'errors-below "Messages below:" errors-below buffer)
          (lsp-bridge-lean4--mk-message-section 'errors-above "Messages above:" errors-above buffer))))))

;; ──────────────────────────────────────────────────────────────────
;; Callbacks from lsp-bridge Python handlers
;; ──────────────────────────────────────────────────────────────────

(defun lsp-bridge-lean4--update-goals (goals)
  "Receive GOALS from lsp-bridge handler and schedule redisplay."
  (setq lsp-bridge-lean4--goals goals)
  (lsp-bridge-lean4--redisplay-debounced))

(defun lsp-bridge-lean4--update-term-goal (goal)
  "Receive term GOAL from lsp-bridge handler and schedule redisplay."
  (setq lsp-bridge-lean4--term-goal goal)
  (lsp-bridge-lean4--redisplay-debounced))

;; ──────────────────────────────────────────────────────────────────
;; Debouncing (ported from lean4-info.el)
;; ──────────────────────────────────────────────────────────────────

(defvar lsp-bridge-lean4--debounce-delay 0.1
  "Seconds to wait before rendering the info buffer.")

(defvar lsp-bridge-lean4--debounce-upper-bound 0.5
  "Maximum seconds to stagger debouncing before forcing a render.")

(defvar lsp-bridge-lean4--debounce-timer nil)
(defvar lsp-bridge-lean4--debounce-begin-time nil)

(defun lsp-bridge-lean4--redisplay-debounced ()
  "Debounced redisplay of the *Lean Goal* buffer."
  (unless lsp-bridge-lean4--debounce-begin-time
    (setq lsp-bridge-lean4--debounce-begin-time (current-time)))
  (if (>= (time-to-seconds
            (time-subtract (current-time)
                           lsp-bridge-lean4--debounce-begin-time))
           lsp-bridge-lean4--debounce-upper-bound)
      (progn
        (setq lsp-bridge-lean4--debounce-begin-time nil)
        (lsp-bridge-lean4--redisplay))
    (when lsp-bridge-lean4--debounce-timer
      (cancel-timer lsp-bridge-lean4--debounce-timer))
    (setq lsp-bridge-lean4--debounce-timer
          (run-with-timer
           lsp-bridge-lean4--debounce-delay nil
           (lambda ()
             (setq lsp-bridge-lean4--debounce-begin-time nil)
             (lsp-bridge-lean4--redisplay))))))

;; ──────────────────────────────────────────────────────────────────
;; Refresh — sends requests to lsp-bridge
;; ──────────────────────────────────────────────────────────────────

(defun lsp-bridge-lean4--refresh ()
  "Request plain goal and term goal from lsp-bridge."
  (when (and (lsp-bridge-lean4--info-buffer-active-p)
             (lsp-bridge-call-file-api-p))
    (lsp-bridge-call-file-api "lean_plain_goal" (lsp-bridge--position))
    (lsp-bridge-call-file-api "lean_plain_term_goal" (lsp-bridge--position))))

;; ──────────────────────────────────────────────────────────────────
;; Hooks — trigger refresh on cursor movement / idle
;; ──────────────────────────────────────────────────────────────────

(defun lsp-bridge-lean4--post-command ()
  "Hook for `post-command-hook' in lean4 buffers."
  (lsp-bridge-lean4--redisplay-debounced))

(defvar lsp-bridge-lean4--idle-timer nil)

(defun lsp-bridge-lean4--setup ()
  "Set up lsp-bridge-lean4 hooks in the current lean4 buffer."
  (add-hook 'post-command-hook #'lsp-bridge-lean4--post-command nil t)
  (when lsp-bridge-lean4--idle-timer
    (cancel-timer lsp-bridge-lean4--idle-timer))
  (setq lsp-bridge-lean4--idle-timer
        (run-with-idle-timer 0.5 t #'lsp-bridge-lean4--refresh)))

(defun lsp-bridge-lean4--teardown ()
  "Remove lsp-bridge-lean4 hooks from the current lean4 buffer."
  (remove-hook 'post-command-hook #'lsp-bridge-lean4--post-command t)
  (when lsp-bridge-lean4--idle-timer
    (cancel-timer lsp-bridge-lean4--idle-timer)
    (setq lsp-bridge-lean4--idle-timer nil)))

(provide 'lsp-bridge-lean4)
;;; lsp-bridge-lean4.el ends here
