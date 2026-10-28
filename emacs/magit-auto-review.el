;;; magit-auto-review.el --- Contextual PR review in Magit -*- lexical-binding: t; -*-

;; Version: 0.1.0
;; Package-Requires: ((emacs "29.1") (magit "4.6") (transient "0.13"))
;; Keywords: tools, vc
;; URL: https://github.com/perweij/auto-review
;; SPDX-License-Identifier: GPL-3.0-or-later

;;; Commentary:
;; Generated comments begin Unstaged.  Move whole comments with s/u, edit inline
;; with e, and inspect the Staged section before publishing.  The backend owns
;; all persistence; old generations remain available through the history view.

;;; Code:
(require 'cl-lib)
(require 'magit)
(require 'transient)
(require 'button)
(require 'magit-auto-review-api)

(defvar forge-buffer-topic)
(declare-function magit-auto-review--render-conversations "magit-auto-review-conversations" ())
(declare-function magit-auto-review--insert-review-status "magit-auto-review-conversations" ())
(declare-function magit-auto-review-pull-requests "magit-auto-review-pulls" (&optional directory))
(declare-function magit-auto-review-edit-menu "magit-auto-review-actions" ())
(declare-function magit-auto-review-owner-open "magit-auto-review-owner" (directory number &optional repository))

(defcustom magit-auto-review-claude-model nil
  "Default Claude model for new reviews; R validates it against CLI choices."
  :type '(choice (const nil) string) :group 'magit-auto-review)
(defcustom magit-auto-review-codex-model nil
  "Default Codex model for new reviews; R validates it against CLI choices."
  :type '(choice (const nil) string) :group 'magit-auto-review)
(defcustom magit-auto-review-synthesis-tests "focused"
  "Testing policy for synthesis or a single review.  R shows it before running.
Focused permits small checks; full permits one full-suite attempt.
With both providers enabled, their independent reviews always stay focused;
the full-suite permission applies only to synthesis.
These are model instructions, not an enforced process timeout."
  :type '(choice (const "focused") (const "full")) :group 'magit-auto-review)

(defface magit-auto-review-anchor '((t :inherit highlight :weight bold))
  "Exact lines to which the comment is attached." :group 'magit-auto-review)
(defface magit-auto-review-finding '((t :inherit default :weight bold))
  "Finding headings below the status groups." :group 'magit-auto-review)
(defface magit-auto-review-editing '((t :inherit highlight))
  "The comment body currently being edited." :group 'magit-auto-review)

(defvar-local magit-auto-review--review nil)
(defvar-local magit-auto-review--session nil)
(defvar-local magit-auto-review--generation nil)
(defvar-local magit-auto-review--editor nil)
(defvar-local magit-auto-review--process nil)
(defvar-local magit-auto-review--operation nil)
(defvar-local magit-auto-review--progress nil)
(defvar-local magit-auto-review--new-ready nil)
(defvar-local magit-auto-review--target nil)
(defvar-local magit-auto-review--repository nil)
(defvar-local magit-auto-review--sections nil)
(defvar-local magit-auto-review--history-origin nil)
(defvar-local magit-auto-review--history-view nil)
(defvar-local magit-auto-review--placement-comment nil)
(defvar-local magit-auto-review--placement-callback nil)
(defvar-local magit-auto-review--placement-context nil)
(defvar-local magit-auto-review--run-settings nil)
(defvar-local magit-auto-review--notice nil)
(defvar magit-auto-review--collapse-on-render nil)

(defvar magit-auto-review-file-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "q") #'quit-window)
    map))
(define-minor-mode magit-auto-review-file-mode
  "Read-only review context; q returns to the review."
  :lighter " Review context" :keymap magit-auto-review-file-mode-map
  (when magit-auto-review-file-mode (setq buffer-read-only t)))

(defvar magit-auto-review-edit-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map text-mode-map)
    (define-key map (kbd "C-c C-c") #'magit-auto-review-save)
    (define-key map (kbd "C-c C-k") #'magit-auto-review-cancel-edit)
    (define-key map (kbd "C-c C-r") #'magit-auto-review-recover-edit)
    (define-key map (kbd "C-c C-e") #'magit-auto-review-return-to-edit)
    (define-key map (kbd "C-c ?") #'magit-auto-review-edit-menu)
    map))

(defvar magit-auto-review-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-section-mode-map)
    (dolist (binding '(("g" . magit-auto-review-refresh)
                       ("s" . magit-auto-review-stage) ("u" . magit-auto-review-unstage)
                       ("k" . magit-auto-review-suppress) ("e" . magit-auto-review-edit)
                       ("RET" . magit-auto-review-visit) ("?" . magit-auto-review-dispatch)
                       ("P" . magit-auto-review-publish-menu) ("h" . magit-auto-review-history)
                       ("q" . quit-window) ("C-c C-c" . magit-auto-review-save)
                       ("C-c C-k" . magit-auto-review-cancel-edit)))
      (define-key map (kbd (car binding)) (cdr binding)))
    map))

(defun magit-auto-review--render-placeholder (message)
  "Render MESSAGE inside a valid section before a saved review is available."
  (let ((inhibit-read-only t))
    (erase-buffer)
    (setq magit-auto-review--sections nil)
    (magit-insert-section (review-root)
      (magit-insert-heading "Auto-review")
      (insert message "\n"))
    (goto-char (point-min))
    (set-buffer-modified-p nil))
  (magit-auto-review--header))

(define-derived-mode magit-auto-review-mode magit-section-mode "Auto-review"
  "Review saved comments without changing the Git index."
  (setq-local revert-buffer-function (lambda (&rest _) (magit-auto-review-refresh)))
  (setq-local truncate-lines nil)
  (setq-local buffer-read-only t)
  (add-hook 'kill-buffer-query-functions #'magit-auto-review--can-leave nil t)
  ;; Magit's command hooks require a section even if discovery or reading fails.
  (magit-auto-review--render-placeholder "No review loaded."))

(defun magit-auto-review--request (command &rest properties)
  "Build COMMAND request with PROPERTIES for the currently viewed generation."
  (append `((command . ,command) (session . ,magit-auto-review--session)
            (generation . ,magit-auto-review--generation)
            (revision . ,(alist-get 'revision magit-auto-review--review))) properties))

(defun magit-auto-review--comment (id)
  "Return comment ID in the displayed saved review."
  (cl-find id (alist-get 'comments magit-auto-review--review)
           :key (lambda (c) (alist-get 'id c)) :test #'equal))

(defun magit-auto-review--comment-section ()
  "Return the comment section at point, even inside a child section."
  (let ((section (magit-current-section)))
    (while (and section (not (eq (oref section type) 'review-comment)))
      (setq section (oref section parent)))
    section))

(defun magit-auto-review--id ()
  "Return the whole comment at point or report a user error."
  (let ((section (magit-auto-review--comment-section)))
    (unless section (user-error "Move to a comment first"))
    (oref section value)))

(defun magit-auto-review--section-context ()
  "Describe the nearest draft, posted discussion, or saved-version section."
  (let ((section (magit-current-section)) kind)
    (while (and section (not kind))
      (setq kind (pcase (oref section type)
                   ((or 'review-version 'review-versions) 'version)
                   ('review-thread 'discussion)
                   ('review-body 'body)
                   ('review-comment 'comment))
            section (oref section parent)))
    kind))

(defun magit-auto-review--require-current-draft ()
  "Prevent actions on context from unexpectedly changing its containing draft."
  (pcase (magit-auto-review--section-context)
    ('version (user-error "Earlier text is read-only history; y recovers a copy. Move to the current draft to change it"))
    ('discussion (user-error "These messages are posted GitHub context. Move to Your draft reply to change the outgoing draft"))))

(defun magit-auto-review--editable ()
  "Check that editing the current generation is possible."
  (when (or magit-auto-review--history-view (magit-auto-review--true-p (alist-get 'historical magit-auto-review--review)))
    (user-error "History is read-only; recover text into the current review"))
  (unless magit-auto-review--review (user-error "Run or open a saved review first")))

(defun magit-auto-review--can-leave ()
  "Resolve an inline editor before a structural change or buffer exit."
  (if (not magit-auto-review--editor) t
    (pcase (read-char-choice "Inline edit: [s]ave, [d]iscard, [c]ancel action: " '(?s ?d ?c))
      (?s (magit-auto-review-save) (not magit-auto-review--editor))
      (?d (magit-auto-review-cancel-edit) t)
      (_ nil))))

(defun magit-auto-review--header ()
  "Update only the header; never disturb an active editor."
  (let* ((snapshot (alist-get 'snapshot magit-auto-review--review))
         (checked (alist-get 'checked magit-auto-review--review))
         (head (alist-get 'head snapshot)))
    (setq header-line-format
          (concat
           ;; Keep editing/operation feedback visible even in a narrow terminal.
           (cond (magit-auto-review--editor
                  " EDITING · C-c C-c save draft · C-c C-k cancel · C-c ? help |")
                 (magit-auto-review--progress (concat " " magit-auto-review--progress " |"))
                 (magit-auto-review--new-ready
                  (if magit-auto-review--history-view
                      " New review ready (N to open) |"
                    " New review ready (g or N to open) |"))
                 (magit-auto-review--history-view " Saved review (N opens current) |")
                 (magit-auto-review--notice (concat " " magit-auto-review--notice " |"))
                 (t ""))
           (format " PR #%s · %s%s · %s"
                  (or (alist-get 'pr magit-auto-review--review) magit-auto-review--target "?")
                  (if head (substring head 0 (min 12 (length head))) "no saved review")
                  (if (or magit-auto-review--history-view (magit-auto-review--true-p (alist-get 'historical magit-auto-review--review))) " · History (read-only)" "")
                  (if checked (format "GitHub checked %s%s" (alist-get 'at checked)
                                      (if (equal head (alist-get 'head checked)) "" " · SOURCE CHANGED"))
                    "GitHub freshness not checked here"))))))

(defun magit-auto-review--notify (text &optional urls)
  "Show persistent action feedback TEXT and record clickable URLS in the log."
  (setq magit-auto-review--notice text)
  (with-current-buffer (get-buffer-create "*Auto-review progress*")
    (let ((inhibit-read-only t))
      (goto-char (point-max))
      (insert text "\n")
      (dolist (url (delete-dups (delq nil urls)))
        (insert-text-button url 'follow-link t 'action (lambda (_) (browse-url url)))
        (insert "\n"))))
  (magit-auto-review--header)
  (message "%s" text))

(defun magit-auto-review--placement-label (comment)
  "Describe COMMENT's current attachment in user-facing terms."
  (let* ((place (alist-get 'placement comment))
         (context (alist-get 'context comment))
         (thread (alist-get 'thread context)))
    (pcase (alist-get 'kind place)
      ("inline" (format "%s:%s%s (%s)" (alist-get 'path place)
                        (or (alist-get 'start_line place) (alist-get 'line place))
                        (if (and (alist-get 'start_line place)
                                 (/= (alist-get 'start_line place) (alist-get 'line place)))
                            (format "–%s" (alist-get 'line place)) "")
                        (if (equal (alist-get 'side place) "LEFT") "deleted/base lines" "reviewed lines")))
      ("reply" (format "Reply in %s%s" (or (alist-get 'path context) "existing discussion")
                       (if (alist-get 'line context) (format ":%s" (alist-get 'line context))
                         (if thread "" " (context unavailable)"))))
      ("review" "Overall PR assessment")
      (_ "Needs placement (a to choose)"))))

(defun magit-auto-review--insert-code (context)
  "Insert immutable code CONTEXT with its exact range highlighted."
  (when (alist-get 'error context)
    (insert (propertize (concat "  Context unavailable: " (alist-get 'error context) "\n") 'face 'warning)))
  (dolist (row (alist-get 'rows context))
    (let* ((begin (point)) (old (alist-get 'old row)) (new (alist-get 'new row))
           (highlight (magit-auto-review--true-p (alist-get 'highlight row))))
      (insert (format "%5s %5s %s%s\n" (or old "") (or new "")
                      (alist-get 'kind row) (alist-get 'text row)))
      (add-text-properties begin (point)
                           `(face ,(if highlight 'magit-auto-review-anchor
                                     (pcase (alist-get 'kind row) ("+" 'magit-diff-added) ("-" 'magit-diff-removed) (_ 'magit-diff-context)))
                             auto-review-location ,context
                             auto-review-new-location
                             ,(when (or new old)
                                `((kind . "inline") (path . ,(alist-get 'path context))
                                  (side . ,(if (equal (alist-get 'kind row) "-") "LEFT" "RIGHT"))
                                  (line . ,(if (equal (alist-get 'kind row) "-") old new)))))))))

(defun magit-auto-review--conversation-body (body)
  "Render saved conversation BODY without a trailing internal tracking marker."
  (replace-regexp-in-string "\n+<!-- pr-review [^>\n]* -->[ \t\n]*\\'" "" body))

(defun magit-auto-review--severity-label (comment)
  "Return COMMENT's model severity, marking incomplete grouped assessments."
  (let ((severity (alist-get 'severity comment)))
    (if severity
        (concat severity (if (magit-auto-review--true-p (alist-get 'severity_incomplete comment))
                             " / partly unrated" ""))
      "Unrated")))

(defun magit-auto-review--assessment-label (assessment)
  "Describe a finding ASSESSMENT without conflating impact and blocking status."
  (concat (alist-get 'id assessment) ": " (or (alist-get 'severity assessment) "Unrated")
          (pcase (alist-get 'blocking assessment)
            ('t " · blocking") (:false " · non-blocking") (_ ""))))

(defun magit-auto-review--attention-reasons (comment)
  "Return outstanding reasons for inspecting COMMENT before publication."
  (unless (member (alist-get 'status comment) '("published" "suppressed"))
    ;; Individual staging is the user's decision on bulk-only restrictions.
    (if (equal (alist-get 'status comment) "staged")
        (alist-get 'blockers comment)
      (or (alist-get 'bulk_blockers comment) (alist-get 'blockers comment)))))

(defun magit-auto-review--insert-automatic-status ()
  "Explain a saved automatic run and current recovery actions locally."
  (when-let ((run (alist-get 'automation magit-auto-review--review)))
    (let* ((analysis (alist-get 'analysis magit-auto-review--review))
           (superseded (magit-auto-review--true-p (alist-get 'superseded run)))
           (pending (alist-get 'pending analysis))
           (displayed (alist-get 'displayed analysis))
           (attention (cl-remove-if-not #'magit-auto-review--attention-reasons
                                        (alist-get 'comments magit-auto-review--review))))
      (insert (propertize (format "Automatic review: %s\n"
                                 (if superseded "Superseded by completed manual analysis"
                                   (pcase (alist-get 'outcome run)
                                     ("running" "Running") ("published" "Publication confirmed")
                                     ("noop" "Complete — nothing to publish") ("needs_attention" "Needs attention")
                                     ("partial" "Partly published") ("uncertain" "Publication uncertain")
                                     ("failed" "Failed") ("interrupted" "Interrupted") (_ "Saved outcome"))))
                          'face 'magit-section-heading))
      (insert "Last automatic run: " (or (alist-get 'reason run) "No saved explanation.") "\n")
      (when (and (not superseded) (not magit-auto-review--history-view)
                 (not (magit-auto-review--true-p (alist-get 'historical magit-auto-review--review)))
                 (equal (alist-get 'error_code run) "confirmation_required"))
        (insert "Agent configuration changed. Inspect the changes and L; T explicitly trusts the saved snapshot and resumes analysis.\n"))
      (cond
       (pending
        (insert (propertize "Showing the previous completed review. A replacement analysis is unfinished; R r resumes it.\n" 'face 'warning)))
       ((not (equal (alist-get 'status displayed) "complete"))
        (insert (propertize "Analysis is unfinished; this is not a completed empty review. R r resumes saved work.\n" 'face 'warning))))
      (when-let ((checked-head (alist-get 'head (alist-get 'checked magit-auto-review--review))))
        (unless (equal checked-head (alist-get 'head (alist-get 'snapshot magit-auto-review--review)))
          (insert (propertize "PR source changed since analysis. R r reviews the new snapshot; acknowledging a draft does not update its source.\n" 'face 'warning))))
      (let* ((target (or pending displayed))
             (discussion (alist-get 'discussion target)))
        (when-let ((error (alist-get 'error discussion)))
          (insert (propertize (concat "Discussion unavailable: " error "\n") 'face 'warning)))
        (when (equal (alist-get 'contributions (alist-get 'phases target)) "needs_reassessment")
          (insert "Discussion still challenges finding validity. Inspect L, then R r to retry analysis.\n")))
      (if attention
          (progn
            (insert (format "Attention now: %d finding(s). Select a finding below to inspect it.\n" (length attention)))
            (dolist (comment attention)
              (let ((id (alist-get 'id comment)))
                (insert-text-button (concat "  " (alist-get 'title comment))
                                    'follow-link t
                                    'action (lambda (_) (magit-auto-review--reveal-comment id)))
                (insert " — " (string-join (magit-auto-review--attention-reasons comment) " ") "\n"))))
        (when (equal (alist-get 'status displayed) "complete")
          (insert "No outstanding draft corrections. Inspect your selection; P p publishes Staged.\n")))
      (when (cl-some (lambda (batch)
                      (cl-some (lambda (action) (member (alist-get 'status action) '("draft" "failed" "submitting" "ambiguous")))
                               (alist-get 'actions batch)))
                    (alist-get 'batches magit-auto-review--review))
        (insert (propertize "Publication has unfinished actions. P opens publication and receipt recovery; confirmed posts stay Published.\n" 'face 'warning)))
      (insert "L opens the saved automatic-run log. Opening this review does not resume or publish it.\n\n"))))

(defun magit-auto-review--insert-comment (comment)
  "Insert COMMENT below its exact code anchor and existing discussion."
  (let* ((identity (alist-get 'id comment)) (context (alist-get 'context comment))
         (rows (alist-get 'rows context))
         (anchor (cl-position-if (lambda (row) (magit-auto-review--true-p (alist-get 'highlight row))) rows :from-end t))
         (after (and anchor (nthcdr (1+ anchor) rows))))
    (magit-insert-section section (review-comment identity t)
      (push (cons identity section) magit-auto-review--sections)
      (magit-insert-heading
        (propertize
         (concat (if (equal (alist-get 'classification comment) "HUMAN")
                     (concat (if (equal (alist-get 'status comment) "published") "Published" "Draft")
                             (if (equal (alist-get 'kind (alist-get 'placement comment)) "reply") " reply" " comment"))
                   (magit-auto-review--severity-label comment)) " · "
                (or (alist-get 'path context) "Overall / unplaced") ": " (alist-get 'title comment)
                (if (magit-auto-review--true-p (alist-get 'composing comment)) "  [new, unsaved]" "")
                (if (magit-auto-review--true-p (alist-get 'override comment)) "  [manual placement]" "")
                (if (magit-auto-review--attention-reasons comment) "  [needs attention]" ""))
         'face 'magit-auto-review-finding))
      (when-let ((assessments (alist-get 'finding_assessments comment)))
        (insert (propertize
                 (concat "Model assessment: "
                         (mapconcat #'magit-auto-review--assessment-label assessments "; ") "\n")
                 'face 'shadow)))
      (insert (propertize (concat "Placement: " (magit-auto-review--placement-label comment)
                                  (if (magit-auto-review--true-p (alist-get 'override comment)) " · manual" " · automatic")
                                  "\n\n") 'face 'shadow))
      ;; Place the comment immediately after the actual range, not below the
      ;; hunk's last context line. Any following code is displayed afterwards.
      (magit-auto-review--insert-code
       (if anchor (cons (cons 'rows (cl-subseq rows 0 (1+ anchor))) (assq-delete-all 'rows (copy-tree context))) context))
      (let ((thread (alist-get 'thread context)))
        (when thread
          (magit-insert-section (review-thread (alist-get 'key thread) t)
            (magit-insert-heading
              (format "GitHub discussion%s%s (%d posted comments)"
                      (if (magit-auto-review--true-p (alist-get 'is_resolved thread)) " · resolved" "")
                      (if (magit-auto-review--true-p (alist-get 'is_outdated thread)) " · outdated" "")
                      (length (alist-get 'comments thread))))
            (dolist (message (alist-get 'comments thread))
              (insert (propertize (format "@%s · %s\n" (alist-get 'author message) (alist-get 'created_at message))
                                  'face 'magit-section-secondary-heading))
              (insert (magit-auto-review--conversation-body (alist-get 'body message)) "\n\n")))))
      (magit-insert-section (review-body identity)
        (magit-insert-heading
          (concat (if (equal (alist-get 'status comment) "published") "Published"
                    (if (magit-auto-review--true-p (alist-get 'composing comment)) "New unsaved" "Your draft"))
                  (if (equal (alist-get 'kind (alist-get 'placement comment)) "reply") " reply" " comment")))
        (unless (equal (alist-get 'status comment) "published")
          (insert (propertize "Only this text is published from this item. e edits; k excludes this draft; ? explains.\n"
                              'face 'shadow)))
        (let ((start (point)))
          (insert (alist-get 'body comment))
          (put-text-property start (point) 'auto-review-body identity)
          ;; Empty drafts still have a stable insertion position.
          (insert (propertize "\n" 'auto-review-body-end identity 'rear-nonsticky t))))
      (when after
        (insert "\n")
        (magit-auto-review--insert-code
         (cons (cons 'rows after) (assq-delete-all 'rows (copy-tree context)))))
      (when-let ((url (alist-get 'html_url (alist-get 'receipt comment))))
        (insert-text-button url 'follow-link t
                            'action (lambda (_button) (browse-url url)))
        (insert "\n"))
      (when (alist-get 'blockers comment)
        (insert (propertize (concat "  " (string-join (alist-get 'blockers comment) " ") "\n") 'face 'warning)))
      (when (equal (alist-get 'kind (alist-get 'placement comment)) "unplaced")
        (insert "  Press a to choose a code line, thread, or overall PR assessment.\n"))
      (when (and (not (alist-get 'blockers comment)) (alist-get 'bulk_blockers comment))
        (unless (member (alist-get 'status comment) '("staged" "published" "suppressed"))
          (insert (propertize (concat "  " (string-join (alist-get 'bulk_blockers comment) " ")
                                      "\n  Inspect, then s on this item to stage individually, or k to exclude.\n") 'face 'warning))))
      (when (magit-auto-review--attention-reasons comment)
        (insert "  e edits wording; a corrects placement. After inspecting changed discussion, i acknowledges it; then s stages.\n"))
      (when (alist-get 'evidence comment)
        (magit-insert-section (review-evidence identity t)
          (magit-insert-heading "Evidence and additional locations (l to visit)")
          (dolist (evidence (alist-get 'evidence comment)) (insert evidence "\n"))))
      ;; Empty drafts from older clients are not useful text versions. Keep the
      ;; original indices and saved records so recovery/history remain intact.
      (let ((versions (cl-loop for version in (alist-get 'versions comment) for index from 0
                               unless (string-empty-p (string-trim (or (alist-get 'body version) "")))
                               collect (cons index version))))
        (when versions
          (magit-insert-section (review-versions identity t)
            (magit-insert-heading (format "Saved text versions (%d)" (length versions)))
            (insert "Earlier wording for recovery; never included in publication. TAB reads; y recovers a copy.\n")
            (dolist (entry versions)
              (let ((version (cdr entry)))
                (magit-insert-section (review-version (cons identity (car entry)) t)
                  (magit-insert-heading (format "%s · %s" (alist-get 'at version) (alist-get 'status version)))
                  (insert (alist-get 'body version) "\n")))))))
      (insert "\n"))))

(defun magit-auto-review--fold-key (section)
  "Identify SECTION across rerenders, including a comment moving status."
  (cons (oref section type) (oref section value)))

(defun magit-auto-review--walk-sections (function &optional root)
  "Apply FUNCTION to ROOT and its children."
  (when-let ((section (or root magit-root-section)))
    (funcall function section)
    (dolist (child (oref section children))
      (magit-auto-review--walk-sections function child))))

(defun magit-auto-review--goto-comment (id &optional reveal)
  "Visit comment ID, optionally REVEALing its ancestors."
  (when-let ((section (cdr (assoc id magit-auto-review--sections))))
    (let ((target section) (parent (oref section parent)))
      (while parent
        (if reveal (magit-section-show parent)
          (when (oref parent hidden) (setq target parent)))
        (setq parent (oref parent parent)))
      (goto-char (oref target start)))))

(defun magit-auto-review--reveal-comment (id)
  "Show draft ID, its own body, and its status group."
  (magit-auto-review--goto-comment id t)
  (when-let ((section (cdr (assoc id magit-auto-review--sections))))
    (magit-section-show section)
    (dolist (child (oref section children))
      (when (eq (oref child type) 'review-body) (magit-section-show child)))))

(defun magit-auto-review--render (&optional target)
  "Render the active view, optionally visiting draft TARGET."
  (if (derived-mode-p 'magit-auto-review-conversations-mode)
      (magit-auto-review--render-conversations)
    (magit-auto-review--render-drafts target)))

(defun magit-auto-review--render-drafts (&optional target)
  "Render the saved review, retaining folds and moving to TARGET if present."
  (unless magit-auto-review--editor
    (let ((inhibit-read-only t)
          (old-id (ignore-errors (magit-auto-review--id)))
          (folds (make-hash-table :test #'equal)))
      (magit-auto-review--walk-sections
       (lambda (s) (puthash (magit-auto-review--fold-key s) (oref s hidden) folds)))
      (erase-buffer)
      (setq magit-auto-review--sections nil)
      (magit-insert-section (review-root)
        (insert (propertize (format "Review #%s\n\n" (alist-get 'pr magit-auto-review--review)) 'face 'magit-section-heading))
        (when-let ((url (alist-get 'url magit-auto-review--review)))
          (insert-text-button url 'follow-link t 'action (lambda (_) (browse-url url)))
          (insert "\n\n"))
        (magit-auto-review--insert-review-status)
        (magit-auto-review--insert-automatic-status)
        (insert (propertize (format "Model suggestion: %s. Next review verdict: %s (v to choose).\n"
                                    (or (alist-get 'recommendation magit-auto-review--review)
                                        (if (equal (alist-get 'kind (alist-get 'coverage magit-auto-review--review)) "followup")
                                            "none (limited follow-up)" "unavailable"))
                                    (magit-auto-review--verdict-label)) 'face 'shadow))
        (insert "Each item is one draft; its GitHub discussion is context. s stages; k excludes this draft.\n")
        (insert "o sends findings to Owner for fixing. a chooses an attachment. P publishes Staged. ? shows actions.\n")
        (when-let ((reason (alist-get 'reason (cl-find-if (lambda (v) (not (magit-auto-review--true-p (alist-get 'available v))))
                                                         (alist-get 'verdicts magit-auto-review--review)))))
          (insert (propertize (concat reason "\n") 'face 'shadow)))
        (insert "\n")
        (when-let ((last (alist-get 'last_verdict magit-auto-review--review)))
          (insert (propertize (format "Already submitted: %s. Retrying remaining comments will not repeat it.\n"
                                      (alist-get 'event last)) 'face 'shadow))
          (when-let ((url (alist-get 'html_url (alist-get 'receipt last))))
            (insert-text-button url 'follow-link t 'action (lambda (_) (browse-url url)))
            (insert "\n"))
          (insert "\n"))
        (dolist (group '(("unstaged" . "Unstaged") ("staged" . "Staged") ("suppressed" . "Suppressed") ("published" . "Published")))
          (let ((comments (cl-remove-if-not (lambda (c) (equal (alist-get 'status c) (car group)))
                                            (alist-get 'comments magit-auto-review--review))))
            (magit-insert-section (review-group (car group) t)
              (magit-insert-heading
                (format "%s (%d)%s" (cdr group) (length comments)
                        (if (equal (car group) "staged")
                            (concat " · Overall verdict: " (magit-auto-review--verdict-label) " (v)") "")))
              (dolist (comment comments) (magit-auto-review--insert-comment comment))
              (insert "\n"))))
        (when (alist-get 'documents magit-auto-review--review)
          (magit-insert-section (review-documents nil t)
            (magit-insert-heading "Historical combined drafts (manual recovery only)")
            (dolist (doc (alist-get 'documents magit-auto-review--review))
              (magit-insert-section (review-document (alist-get 'name doc) t)
                (magit-insert-heading (alist-get 'name doc))
                (insert (alist-get 'text doc) "\n\n"))))))
      (magit-auto-review--walk-sections
       (lambda (s)
         (unless (eq s magit-root-section)
           (oset s hidden
                 (or (and (eq (oref s type) 'review-comment)
                          (member (oref s value) magit-auto-review--collapse-on-render))
                     (gethash (magit-auto-review--fold-key s) folds (not (eq (oref s type) 'review-body))))))))
      ;; Apply visibility from the root. Showing a descendant separately can
      ;; cut a hole in its collapsed parent's invisibility overlay.
      (magit-section-show magit-root-section)
      (goto-char (point-min))
      (magit-auto-review--goto-comment (or target old-id))
      (set-buffer-modified-p nil)))
  (magit-auto-review--header))

(defun magit-auto-review--accept (review &optional target)
  "Accept saved REVIEW without damaging an active editor; optionally visit TARGET."
  (if magit-auto-review--editor
      (setq magit-auto-review--new-ready t)
    (setq magit-auto-review--review review
          magit-auto-review--session (alist-get 'session review)
          magit-auto-review--generation (alist-get 'generation review)
          magit-auto-review--new-ready
          (not (equal magit-auto-review--generation (alist-get 'current_generation review))))
    (let ((operation (car (alist-get 'operations review))))
      (cond (operation
             (setq magit-auto-review--operation (alist-get 'operation_id operation)
                   magit-auto-review--progress (concat (alist-get 'kind operation) " operation running")))
            ((not (process-live-p magit-auto-review--process))
             (setq magit-auto-review--operation nil magit-auto-review--progress nil))))
    (magit-auto-review--render target)))

;;;###autoload
(defun magit-auto-review (&optional pr)
  "Open PR from Forge or a prefix argument; otherwise list repository PRs."
  (interactive (list (and current-prefix-arg (read-number "Pull request number: "))))
  (let* ((directory (or (magit-toplevel) default-directory))
         ;; Forge's current-topic helper also falls back to cached branch
         ;; metadata. Only an explicitly selected/visited PR bypasses the list.
         (topic (or (magit-section-value-if 'pullreq)
                    (and (boundp 'forge-buffer-topic) forge-buffer-topic)))
         (number (or pr (and topic (object-of-class-p topic 'forge-pullreq) (oref topic number)))))
    (if number (magit-auto-review--open-pr directory number)
      (magit-auto-review-pull-requests directory))))

(defun magit-auto-review--find-session (directory number repository)
  "Find a saved session for NUMBER in DIRECTORY, scoped to REPOSITORY.
Ask which session to open when more than one matches.  Discovery is local."
  (let* ((default-directory directory)
         (sessions (alist-get 'sessions
                             (magit-auto-review--call `((command . "discover") (cwd . ,directory)
                                                       (pr . ,number) (repository . ,repository))))))
    (if (= (length sessions) 1) (car sessions)
      (when sessions
        (let* ((choices (mapcar (lambda (s) (cons (format "#%s · %s · %s" (alist-get 'pr s) (alist-get 'repository s) (alist-get 'created_at s)) s)) sessions))
               (name (completing-read "Saved review: " choices nil t)))
          (cdr (assoc name choices)))))))

(defun magit-auto-review--open-pr (directory number &optional repository)
  "Open NUMBER using the remembered toolkit for DIRECTORY and REPOSITORY."
  (let ((choice (magit-auto-review--call `((command . "toolkit") (cwd . ,directory) (repository . ,repository)))))
    (if (equal (alist-get 'toolkit choice) "owner")
        (progn (require 'magit-auto-review-owner)
               (magit-auto-review-owner-open directory number repository))
      (magit-auto-review--open-review-pr directory number repository))))

(defun magit-auto-review-select-toolkit ()
  "Select and remember a toolkit, retaining existing workflow buffers."
  (interactive)
  (let* ((directory (or (magit-toplevel) default-directory))
         (number magit-auto-review--target)
         (repository magit-auto-review--repository)
         (choice (completing-read "Toolkit: " '("Reviewer" "Owner") nil t)))
    (magit-auto-review--call `((command . "toolkit") (cwd . ,directory)
                              (repository . ,repository) (toolkit . ,(downcase choice))))
    (if number (magit-auto-review--open-pr directory number repository)
      (magit-auto-review-pull-requests directory))))

(define-key magit-auto-review-mode-map (kbd "M") #'magit-auto-review-select-toolkit)

(defun magit-auto-review--open-review-pr (directory number &optional repository)
  "Open NUMBER in DIRECTORY, optionally scoped to REPOSITORY."
  (let ((selected (magit-auto-review--find-session directory number repository)))
    (pop-to-buffer (get-buffer-create (format "*Auto-review %s #%s (%s)*"
                                               (or repository (alist-get 'repository selected)
                                                   (file-name-nondirectory (directory-file-name directory)))
                                               number (abbreviate-file-name directory))))
    ;; Re-entering from Forge must not reset a live editor or jump generations.
    (unless (derived-mode-p 'magit-auto-review-mode)
      (magit-auto-review-mode)
      (setq default-directory directory magit-auto-review--target number
            magit-auto-review--repository (or repository (alist-get 'repository selected))
            magit-auto-review--session (alist-get 'session selected)
            magit-auto-review--generation (alist-get 'generation selected))
      (if selected (magit-auto-review-refresh)
        (magit-auto-review--render-placeholder
         "No saved review found here. Use R to run the reviewers explicitly; B lists pull requests.")))))

(defun magit-auto-review-refresh (&optional current)
  "Load the latest completed review locally, leaving an inline edit intact.
Explicit history buffers refresh their pinned generation instead.  With CURRENT,
leave that history view and open the latest completed review.  Saved drafts and
earlier results remain in history; this never makes network or model calls."
  (interactive)
  (if magit-auto-review--editor
      (message "Finish the inline edit before refreshing; progress remains visible in the header")
    (let* ((session (or magit-auto-review--session
                        (alist-get 'session
                                   (magit-auto-review--find-session
                                    default-directory magit-auto-review--target
                                    magit-auto-review--repository))))
           (request (magit-auto-review--request "read"))
           (previous magit-auto-review--generation))
      (unless session
        (user-error "No saved review found here yet; use R to run the reviewers"))
      (setf (alist-get 'session request) session)
      (when (or current (not magit-auto-review--history-view))
        (setf (alist-get 'generation request) nil))
      ;; Only change the view identity after a successful read.  A failed read
      ;; must leave subsequent actions attached to the displayed generation.
      (let ((review (magit-auto-review--call request)))
        (when current
          (setq magit-auto-review--history-view nil magit-auto-review--history-origin nil))
        (magit-auto-review--accept review))
      (when (and previous (not (equal previous magit-auto-review--generation)))
        (magit-auto-review--notify "Latest completed review opened. Previous drafts and results remain in h (history).")))))

(defun magit-auto-review--selected-ids ()
  "Return whole comment IDs for the section or sibling-heading region."
  (let ((section (magit-current-section)))
    (if (use-region-p)
        (let* ((begin (region-beginning)) (end (region-end))
               (sections (cl-remove-if-not
                          (lambda (entry) (let ((p (marker-position (oref (cdr entry) start)))) (and (>= p begin) (< p end))))
                          (reverse magit-auto-review--sections)))
               (parents (delete-dups (mapcar (lambda (entry) (oref (cdr entry) parent)) sections))))
          (save-excursion
            (goto-char begin)
            (let ((comment (magit-auto-review--comment-section)))
              (unless (and comment (< begin (oref comment content)))
                (user-error "Start the region on a comment heading"))))
          (unless (and sections (= (length parents) 1))
            (user-error "Select sibling comment headings, not part of a body or code"))
          (mapcar #'car sections))
      (if-let ((comment (magit-auto-review--comment-section))) (list (oref comment value))
        (unless section (user-error "Move to a comment or group"))
        (mapcar #'car
                (cl-remove-if-not
                 (lambda (entry) (let ((start (oref (cdr entry) start)))
                                   (and (>= start (oref section start)) (< start (oref section end)))))
                 (reverse magit-auto-review--sections)))))))

(defun magit-auto-review--mutate (operation &rest properties)
  "Apply local OPERATION with PROPERTIES and refresh the displayed result."
  (magit-auto-review--editable)
  (let ((result (magit-auto-review--call
                 (apply #'magit-auto-review--request "mutate" (cons `(operation . ,operation) properties)))))
    (magit-auto-review--accept (alist-get 'review result))
    result))

(defun magit-auto-review--move (operation)
  "Move whole comments with OPERATION while retaining the source cursor."
  (magit-auto-review--require-current-draft)
  (when (magit-auto-review--can-leave)
    (let* ((ids (magit-auto-review--selected-ids))
           (magit-auto-review--collapse-on-render ids)
           (bulk (or (use-region-p) (not (magit-auto-review--comment-section))))
           (first (magit-auto-review--comment (car ids)))
           (status (alist-get 'status first))
           (remaining (cl-remove-if-not (lambda (c) (and (equal (alist-get 'status c) status)
                                                        (not (member (alist-get 'id c) ids))))
                                        (mapcar (lambda (entry) (magit-auto-review--comment (car entry)))
                                                (reverse magit-auto-review--sections))))
           (position (point))
           (next (cl-find-if (lambda (c) (> (oref (cdr (assoc (alist-get 'id c) magit-auto-review--sections)) start) position)) remaining))
           (result (magit-auto-review--mutate operation `(comments . ,(vconcat ids)) `(bulk . ,(if bulk t :false))))
           (skipped (alist-get 'skipped result))
           (target (or (alist-get 'id (car skipped)) (alist-get 'id next) (alist-get 'id (car (last remaining))))))
      (deactivate-mark)
      (magit-auto-review--render target)
      (unless target
        (goto-char (point-min))
        (when-let ((group (cl-find status (oref magit-root-section children) :key (lambda (s) (oref s value)) :test #'equal)))
          (goto-char (oref group start))))
      (magit-auto-review--notify
       (format "%d comment(s) moved to %s%s%s" (length (alist-get 'changed result))
               (pcase operation ("stage" "Staged") ("suppress" "Suppressed") (_ "Unstaged"))
               (if (equal operation "suppress") "; r restores a suppressed comment" "")
               (if skipped (format "; %d skipped: %s" (length skipped) (string-join (alist-get 'reasons (car skipped)) " ")) ""))))))

(defun magit-auto-review-stage () "Stage whole comments at point." (interactive) (magit-auto-review--move "stage"))
(defun magit-auto-review-unstage () "Unstage whole comments at point." (interactive) (magit-auto-review--move "unstage"))
(defun magit-auto-review-suppress () "Dismiss comments reversibly to Suppressed." (interactive) (magit-auto-review--move "suppress"))
(defun magit-auto-review-restore () "Restore suppressed comments to Unstaged." (interactive) (magit-auto-review--move "restore"))

(defun magit-auto-review-return-to-edit ()
  "Reveal and return to the active inline editor without discarding text."
  (interactive)
  (unless magit-auto-review--editor (user-error "No inline edit is active"))
  (magit-auto-review--reveal-comment (plist-get magit-auto-review--editor :id))
  (goto-char (plist-get magit-auto-review--editor :start))
  (when-let ((window (get-buffer-window (current-buffer))))
    (set-window-point window (point))
    (with-selected-window window (recenter)))
  (magit-auto-review--header))

(defun magit-auto-review-edit ()
  "Edit only the selected comment body inline; save with C-c C-c."
  (interactive)
  (magit-auto-review--editable)
  (magit-auto-review--require-current-draft)
  (if magit-auto-review--editor (magit-auto-review-return-to-edit)
   (let* ((id (magit-auto-review--id)) (comment (magit-auto-review--comment id))
         (section (magit-auto-review--comment-section))
         (start (text-property-any (oref section start) (oref section end) 'auto-review-body id))
         (end (text-property-any (oref section start) (oref section end) 'auto-review-body-end id)))
    (when (equal (alist-get 'status comment) "published") (user-error "Published comments are read-only; create a follow-up"))
    (setq start (or start end))
    (magit-auto-review--reveal-comment id)
    (let ((inhibit-read-only t))
      (put-text-property (point-min) (point-max) 'read-only t)
      ;; Permit insertion at either boundary, including a zero-length body.
      ;; The overlay guard still forbids deleting the sentinel/context.
      (put-text-property start (1+ end) 'read-only nil)
      (put-text-property start (1+ end) 'front-sticky '(read-only))
      (put-text-property (1- start) start 'rear-nonsticky '(read-only))
      (put-text-property start (1+ end) 'rear-nonsticky '(read-only))
      (setq magit-auto-review--editor
            (list :id id :start (copy-marker start) :end (copy-marker end t)
                  :keymap (current-local-map)
                  :overlay (make-overlay start (1+ end) nil nil t)
                  :guard (make-overlay (point-min) (point-max) nil nil t)
                  :hint (if (equal (alist-get 'kind (alist-get 'placement comment)) "reply")
                            "Type your reply here…" "Type your comment here…")
                  :revision (alist-get 'revision magit-auto-review--review) :original (alist-get 'body comment)))
      (overlay-put (plist-get magit-auto-review--editor :overlay) 'face '(:inherit magit-auto-review-editing :extend t))
      ;; Emacs clears before-change-functions when a hook rejects an edit.
      ;; Overlay hooks remain installed after an error, including at boundaries.
      (dolist (hook '(modification-hooks insert-in-front-hooks insert-behind-hooks))
        (overlay-put (plist-get magit-auto-review--editor :guard) hook '(magit-auto-review--protect-edit)))
      (magit-auto-review--edit-hint)
      (use-local-map magit-auto-review-edit-map)
      (setq buffer-read-only nil)
      (buffer-enable-undo)
      (setq buffer-undo-list nil)
      (magit-auto-review-return-to-edit)
      (message "Editing the displayed comment. C-c C-c saves; C-c C-k cancels; C-c C-e returns here.")))))

(defun magit-auto-review--edit-hint ()
  "Show an empty editor's insertion point without adding draft text."
  (when magit-auto-review--editor
    (overlay-put (plist-get magit-auto-review--editor :overlay) 'before-string
                 (concat (propertize "C-c C-c save draft · C-c C-k cancel · C-c ? help\n" 'face 'magit-section-secondary-heading)
                         (when (= (marker-position (plist-get magit-auto-review--editor :start))
                                  (marker-position (plist-get magit-auto-review--editor :end)))
                           (propertize (plist-get magit-auto-review--editor :hint) 'face 'shadow 'cursor 0))))))

(defun magit-auto-review--protect-edit (_overlay after begin end &optional _length)
  "Keep edits between BEGIN and END in the active body; refresh its hint AFTER."
  (when magit-auto-review--editor
    (if after (magit-auto-review--edit-hint)
      (when (or (< begin (marker-position (plist-get magit-auto-review--editor :start)))
                (> end (marker-position (plist-get magit-auto-review--editor :end))))
        (user-error "Only the comment body is editable; C-c C-e returns to it")))))

(defun magit-auto-review--finish-edit ()
  "Remove editor markers and restore the read-only review buffer."
  (use-local-map (plist-get magit-auto-review--editor :keymap))
  (delete-overlay (plist-get magit-auto-review--editor :guard))
  (delete-overlay (plist-get magit-auto-review--editor :overlay))
  (dolist (key '(:start :end)) (set-marker (plist-get magit-auto-review--editor key) nil))
  (setq magit-auto-review--editor nil buffer-read-only t)
  (set-buffer-modified-p nil))

(defun magit-auto-review-save ()
  "Save the inline body; a stale-save error leaves text and undo intact."
  (interactive)
  (unless magit-auto-review--editor (user-error "No inline edit is active"))
  (let* ((id (plist-get magit-auto-review--editor :id))
         (new (plist-get magit-auto-review--editor :new))
         (comment (magit-auto-review--comment id))
         (body (buffer-substring-no-properties (plist-get magit-auto-review--editor :start)
                                              (plist-get magit-auto-review--editor :end)))
         (_ (when (and new (string-empty-p (string-trim body)))
              (user-error "Write the comment, or cancel it with C-c C-k")))
         (result (magit-auto-review--call
                  (if new
                      (magit-auto-review--request "mutate" '(operation . "create")
                                                 `(title . ,(alist-get 'title comment)) `(body . ,body)
                                                 `(placement . ,(alist-get 'placement comment)))
                    (magit-auto-review--request "mutate" '(operation . "edit") `(comment . ,id) `(body . ,body))))))
    (magit-auto-review--finish-edit)
    (setq id (if new (car (alist-get 'changed result)) id))
    (magit-auto-review--accept (alist-get 'review result) id)
    (magit-auto-review--reveal-comment id)
    (let* ((section (cdr (assoc id magit-auto-review--sections)))
           (saved (magit-auto-review--comment id))
           ;; Property lookup compares with `eq'. JSON supplies distinct string
           ;; objects for changed IDs and the rendered comment's ID.
           (body-id (alist-get 'id saved)))
      (goto-char (or (text-property-any (oref section start) (oref section end) 'auto-review-body body-id)
                     (text-property-any (oref section start) (oref section end) 'auto-review-body-end body-id)))
      (when-let ((window (get-buffer-window (current-buffer))))
        (set-window-point window (point))
        (with-selected-window window (recenter)))
      (magit-auto-review--notify
       (format "Saved “%s” in %s%s. Showing its text; s stages, k excludes this draft, P publishes Staged."
               (alist-get 'title saved) (capitalize (alist-get 'status saved))
               (if new " as a separate draft" ""))))))

(defun magit-auto-review-cancel-edit ()
  "Cancel a new comment completely, or restore an existing draft's saved body."
  (interactive)
  (unless magit-auto-review--editor (user-error "No inline edit is active"))
  (let ((id (plist-get magit-auto-review--editor :id))
        (original (plist-get magit-auto-review--editor :original-review)))
    (magit-auto-review--finish-edit)
    (if original (magit-auto-review--accept original)
      (magit-auto-review--render id))
    (magit-auto-review--notify (if original "New comment cancelled; no draft was saved."
                                "Edits cancelled; the saved draft is unchanged."))))

(defun magit-auto-review-visit (&optional location)
  "Visit the reviewed file at point, highlighting the actual line/range."
  (interactive)
  (let* ((location (or location (get-text-property (point) 'auto-review-location)
                       (alist-get 'context (magit-auto-review--comment (magit-auto-review--id)))))
         (path (alist-get 'path location)))
    (when (alist-get 'file_unavailable location) (user-error "%s" (alist-get 'file_unavailable location)))
    (unless (and path (alist-get 'line location)) (user-error "Choose a code attachment first"))
    (let* ((source (magit-auto-review--call (magit-auto-review--request "file" `(path . ,path)
                                                                     `(side . ,(or (alist-get 'side location) "RIGHT"))
                                                                     `(commit . ,(alist-get 'commit location)))))
           (buffer (get-buffer-create (format "*Review file %s:%s*" (substring (alist-get 'commit source) 0 12) path))))
      (pop-to-buffer buffer)
      (let ((inhibit-read-only t))
        (remove-overlays)
        (erase-buffer) (insert (alist-get 'text source))
        (setq buffer-file-name nil)
        (let ((buffer-file-name path)) (set-auto-mode))
        (magit-auto-review-file-mode 1)
        (goto-char (point-min))
        (forward-line (1- (or (alist-get 'start_line location) (alist-get 'line location))))
        (let* ((begin (point)) (end (save-excursion (forward-line (1+ (- (alist-get 'line location)
                                                                                     (or (alist-get 'start_line location) (alist-get 'line location))))) (point)))
               (overlay (make-overlay begin end)))
          (overlay-put overlay 'face 'magit-auto-review-anchor))
        (set-buffer-modified-p nil)))))

(defun magit-auto-review-evidence ()
  "Choose and visit a secondary evidence location."
  (interactive)
  (let* ((comment (magit-auto-review--comment (magit-auto-review--id)))
         (choices (mapcar (lambda (l) (cons (format "%s:%s (%s)" (alist-get 'path l) (alist-get 'line l) (alist-get 'side l)) l))
                          (alist-get 'locations comment)))
         (location (cdr (assoc (completing-read "Evidence: " choices nil t) choices))))
    (unless location (user-error "This finding has no structured secondary locations"))
    (magit-auto-review-visit location)))

(defun magit-auto-review--verdict-label ()
  "Return the current user verdict, separate from the model suggestion."
  (pcase (alist-get 'verdict magit-auto-review--review)
    ("REQUEST_CHANGES" "Request changes") ("APPROVE" "Approve") (_ "Comment")))

(provide 'magit-auto-review)
(require 'magit-auto-review-actions)
;;; magit-auto-review.el ends here
