;;; magit-auto-review-actions.el --- Review actions and Transients -*- lexical-binding: t; -*-

;; SPDX-License-Identifier: GPL-3.0-or-later

;;; Commentary:
;; Network/model operations are explicit; a completed replacement never steals
;; the current buffer.  Placement selection uses actual code and conversations.

;;; Code:
(require 'magit-auto-review)
(require 'magit-auto-review-setup)
(require 'magit-auto-review-conversations)

(defun magit-auto-review--start (request callback)
  "Run explicit REQUEST asynchronously and deliver CALLBACK in this buffer."
  (when (process-live-p magit-auto-review--process) (user-error "An operation is already running; use the log or cancel command"))
  (setq magit-auto-review--progress "Starting" magit-auto-review--operation nil)
  (magit-auto-review--header)
  (setq magit-auto-review--process
        (magit-auto-review--async
         request
         (lambda (event)
           (when (alist-get 'operation_id event) (setq magit-auto-review--operation (alist-get 'operation_id event)))
           (setq magit-auto-review--progress (or (alist-get 'message event) "Running"))
           (with-current-buffer (get-buffer-create "*Auto-review progress*")
             (let ((inhibit-read-only t))
               (goto-char (point-max))
               (insert (or (alist-get 'message event) "Operation started") "\n")
               (when (alist-get 'log_path event)
                 (insert "Saved log: " (alist-get 'log_path event) "\n"))))
           (magit-auto-review--header))
         (lambda (result)
           (setq magit-auto-review--process nil magit-auto-review--operation nil magit-auto-review--progress nil)
           (funcall callback result)
           (magit-auto-review--header))
         (lambda (error)
           (setq magit-auto-review--process nil magit-auto-review--operation nil
                 magit-auto-review--progress (magit-auto-review--error-message error))
           (with-current-buffer (get-buffer-create "*Auto-review progress*")
             (let ((inhibit-read-only t))
               (goto-char (point-max))
               (insert "Error: " (magit-auto-review--error-message error) "\n")))
           (magit-auto-review--header)
           (message "%s (L opens the operation log)" (magit-auto-review--error-message error))))))

(defun magit-auto-review--launch-review (settings &optional trusted-snapshot catalogs)
  "Start using frozen SETTINGS, optional TRUSTED-SNAPSHOT and CATALOGS."
  (setq magit-auto-review--followup-request nil)
  (setq magit-auto-review--run-settings (copy-tree settings))
  (magit-auto-review--start
   (append (if magit-auto-review--session (magit-auto-review--request "run")
             `((command . "run") (pr . ,magit-auto-review--target) (cwd . ,default-directory)))
           settings
           (when catalogs `((catalogs . ,catalogs)))
           (when trusted-snapshot `((trusted_snapshot . ,trusted-snapshot))))
   (lambda (result)
     (if magit-auto-review--session
         (setq magit-auto-review--new-ready
               (not (equal magit-auto-review--generation (alist-get 'generation result))))
       (let ((session (car (alist-get 'sessions result))))
         (setq magit-auto-review--session (alist-get 'session session)
               magit-auto-review--new-ready t)))
     (magit-auto-review--notify
      (if magit-auto-review--new-ready
          (if magit-auto-review--history-view
              "Review completed. N opens the latest results; this history view stays unchanged."
            "Review completed. g or N opens the latest results; previous drafts remain in h (history).")
        "Review completed; this saved generation remains current.")))))

(defun magit-auto-review-trust-config ()
  "Confirm the exact agent-configuration snapshot reported by the backend."
  (interactive)
  (let ((buffer (get-buffer "*Auto-review progress*")) data)
    (when buffer
      (with-current-buffer buffer
        (save-excursion
          (goto-char (point-max))
          (when (re-search-backward "^{.*\"confirmation_required\".*}$" nil t)
            (setq data (magit-auto-review--json (match-string-no-properties 0)))))))
    (unless data (user-error "No agent-configuration confirmation is pending; inspect L for the actual failure"))
    (when (yes-or-no-p (format "Trust agent configuration at %s?\n%s" (alist-get 'snapshot data) (alist-get 'paths data)))
      (if magit-auto-review--followup-request
          (let ((request (copy-tree magit-auto-review--followup-request)))
            (setf (alist-get 'trusted_snapshot request) (alist-get 'snapshot data))
            (magit-auto-review--launch-followup request))
        (unless magit-auto-review--run-settings (user-error "Open R and configure the review first"))
        (magit-auto-review--launch-review magit-auto-review--run-settings (alist-get 'snapshot data))))))

(defun magit-auto-review-cancel-operation ()
  "Cancel only this buffer's owned backend operation, preserving saved work."
  (interactive)
  (unless magit-auto-review--operation (user-error "No cancellable model operation is running"))
  (magit-auto-review--call `((command . "cancel") (operation_id . ,magit-auto-review--operation)))
  (setq magit-auto-review--progress "Stopping operation")
  (magit-auto-review--header))

(defun magit-auto-review-log ()
  "Show operation and publication feedback; q returns to the review."
  (interactive)
  (pop-to-buffer (get-buffer-create "*Auto-review progress*"))
  (special-mode)
  (goto-char (point-max)))

(defun magit-auto-review-switch-current ()
  "Explicitly switch to the latest completed generation after resolving edits."
  (interactive)
  (unless (or magit-auto-review--new-ready magit-auto-review--history-view)
    (user-error "This is already the completed review; P publishes staged comments"))
  (when (magit-auto-review--can-leave)
    (magit-auto-review-refresh t)))

(defun magit-auto-review-refresh-github ()
  "Explicitly refresh GitHub context, with no model calls or rematching."
  (interactive)
  (when (magit-auto-review--can-leave)
    (magit-auto-review--editable)
    (magit-auto-review--start (magit-auto-review--request "refresh")
                            (lambda (result) (magit-auto-review--accept (alist-get 'review result))))))

(defun magit-auto-review-acknowledge ()
  "Acknowledge inspection of changed discussion, leaving the comment Unstaged."
  (interactive)
  (magit-auto-review--mutate "acknowledge" `(comment . ,(magit-auto-review--id)))
  (magit-auto-review--notify "Discussion change acknowledged; inspect placement, then s to stage"))

(defun magit-auto-review-set-verdict ()
  "Choose the overall PR verdict for the next submitted review."
  (interactive)
  (when (magit-auto-review--can-leave)
    (magit-auto-review--editable)
    (let* ((verdicts (alist-get 'verdicts magit-auto-review--review))
           (labels '(("COMMENT" . "Comment — feedback without approval")
                     ("REQUEST_CHANGES" . "Request changes — changes needed before merging")
                     ("APPROVE" . "Approve — ready to merge")))
           (choices (cl-loop for v in verdicts
                             when (magit-auto-review--true-p (alist-get 'available v))
                             collect (cons (cdr (assoc (alist-get 'value v) labels)) (alist-get 'value v)))))
      (if (= (length choices) 1)
          (magit-auto-review--notify
           (concat "Overall verdict is Comment. "
                   (or (alist-get 'reason (cl-find-if (lambda (v) (not (magit-auto-review--true-p (alist-get 'available v)))) verdicts)) "")))
        (let* ((label (completing-read "Overall PR verdict (applies to the whole review): " choices nil t nil nil
                                       (cdr (assoc (alist-get 'verdict magit-auto-review--review) labels))))
               (value (cdr (assoc label choices))))
          (magit-auto-review--mutate "verdict" `(verdict . ,value))
          (magit-auto-review--notify (concat "Overall review verdict: " (magit-auto-review--verdict-label))))))))

(defun magit-auto-review-publish ()
  "Validate Staged comments, then briefly confirm counts and the chosen verdict."
  (interactive)
  (when (magit-auto-review--can-leave)
    (magit-auto-review--editable)
    (magit-auto-review--notify "Checking GitHub and preparing the staged comments for publication…")
    (magit-auto-review--start
     (magit-auto-review--request "prepare")
     (lambda (result)
       (magit-auto-review--accept (alist-get 'review result))
       (if (not (magit-auto-review--true-p (alist-get 'ready result)))
           (magit-auto-review--notify (alist-get 'reason result))
         (let ((counts (alist-get 'counts result)))
           (when (and (not magit-auto-review--editor)
                      (yes-or-no-p (format "%sPublish %d inline comment(s), %d thread reply/replies, %d overall assessment(s)%s? "
                                           (if (alist-get 'warnings result)
                                               (concat (string-join (alist-get 'warnings result) " ") " ") "")
                                           (alist-get 'inline counts) (alist-get 'replies counts) (alist-get 'overall counts)
                                           (if (alist-get 'verdict result)
                                               (concat "; overall PR verdict: "
                                                       (pcase (alist-get 'verdict result)
                                                         ("REQUEST_CHANGES" "Request changes") ("APPROVE" "Approve") (_ "Comment"))) ""))))
             (magit-auto-review--start
              (magit-auto-review--request "publish" `(confirmation . ,(alist-get 'confirmation result)))
              (lambda (published)
                (magit-auto-review--accept (alist-get 'review published))
                (let* ((review (alist-get 'review published))
                       (urls (delq nil (mapcar (lambda (c) (alist-get 'html_url (alist-get 'receipt c)))
                                               (alist-get 'comments review)))))
                  (when-let ((url (alist-get 'html_url (alist-get 'receipt (alist-get 'last_verdict review)))))
                    (push url urls))
                  (magit-auto-review--notify
                   (if (magit-auto-review--true-p (alist-get 'complete published))
                       "Publication confirmed by GitHub. Expand Published; b opens the comment, L lists receipt URLs."
                     (concat "Publication paused: " (alist-get 'reason published))) urls)))))))))))

(defun magit-auto-review-resolve-publication ()
  "Resolve ambiguous posts before allowing editing or a confirmed retry."
  (interactive)
  (when (magit-auto-review--can-leave)
    (magit-auto-review--start (magit-auto-review--request "resolve")
                            (lambda (result) (magit-auto-review--accept (alist-get 'review result))))))

(defun magit-auto-review-new-comment ()
  "Create a new Unstaged comment, then edit it inline."
  (interactive)
  (when (magit-auto-review--can-leave)
    (let* ((placement (get-text-property (point) 'auto-review-new-location))
           (title (read-string "Comment title: "))
           (result (apply #'magit-auto-review--mutate "create" `(title . ,title) '(body . "")
                          (when placement `((placement . ,placement)))))
           (id (car (alist-get 'changed result))))
      (magit-auto-review--goto-comment id t)
      (magit-auto-review-edit))))

(defun magit-auto-review-follow-up ()
  "Create a distinct new follow-up to the published comment at point."
  (interactive)
  (when (magit-auto-review--can-leave)
    (let* ((comment (magit-auto-review--comment (magit-auto-review--id)))
           (_ (unless (equal (alist-get 'status comment) "published")
                (user-error "Follow-up applies to a published comment; c creates a new comment")))
           (result (magit-auto-review--mutate "create" `(title . ,(concat "Follow-up: " (alist-get 'title comment))) '(body . ""))))
      (magit-auto-review--goto-comment (car (alist-get 'changed result)) t)
      (magit-auto-review-edit)
      (magit-auto-review--notify "Write the follow-up here, save, then a to choose its discussion or code placement"))))

(defun magit-auto-review-placement ()
  "Choose an attachment using code/conversations, or return to automatic placement."
  (interactive)
  (when (magit-auto-review--can-leave)
    (magit-auto-review--editable)
    (let* ((id (magit-auto-review--id)) (comment (magit-auto-review--comment id)))
      (pcase (read-char-choice
              (format "Current: %s · %s. Choose [c]ode, [t]hread, [o]verall, [a]utomatic: "
                      (magit-auto-review--placement-label comment)
                      (if (magit-auto-review--true-p (alist-get 'override comment)) "manual" "automatic")) '(?c ?t ?o ?a))
        (?c (magit-auto-review--placement-browser id 'code))
        (?t (magit-auto-review--placement-browser id 'thread))
        (?o (magit-auto-review--mutate "placement" `(comment . ,id) '(placement . ((kind . "review")))))
        (?a (magit-auto-review--mutate "automatic" `(comment . ,id)))))))

(defun magit-auto-review--placement-browser (id kind)
  "Open a contextual attachment picker for ID of KIND."
  (let* ((origin (current-buffer))
         (review magit-auto-review--review)
         (diff (and (eq kind 'code) (magit-auto-review--call (magit-auto-review--request "diff")))))
    (pop-to-buffer (generate-new-buffer "*Choose comment placement*"))
    (magit-section-mode)
    (setq-local magit-auto-review--history-origin origin)
    (setq-local magit-auto-review--placement-comment id)
    (let ((inhibit-read-only t))
      (magit-insert-section (placement-root)
        (insert "Choose a code line/range or thread; RET attaches the unchanged text and unstages it.\n\n")
        (if (eq kind 'code)
            (magit-auto-review--insert-placement-diff diff)
          (magit-auto-review--insert-placement-threads review))))
    (use-local-map (copy-keymap magit-section-mode-map))
    (local-set-key (kbd "RET") #'magit-auto-review--choose-placement)
    (local-set-key (kbd "q") #'quit-window)
    (goto-char (point-min))))

(defun magit-auto-review--insert-placement-diff (diff)
  "Insert DIFF rows with whole-line attachment coordinates."
  (dolist (file (alist-get 'diffs diff))
    (magit-insert-section (placement-file (alist-get 'path file))
      (magit-insert-heading (alist-get 'path file))
      (dolist (hunk (alist-get 'hunks file))
        (magit-insert-section (placement-hunk (alist-get 'header hunk))
          (magit-insert-heading (alist-get 'header hunk))
          (dolist (row (alist-get 'rows hunk))
            (let ((begin (point)) (left (equal (alist-get 'kind row) "-")))
              (insert (format "%5s %5s %s%s\n" (or (alist-get 'old row) "") (or (alist-get 'new row) "")
                              (alist-get 'kind row) (alist-get 'text row)))
              (put-text-property begin (point) 'auto-review-choose
                                 `((kind . "inline") (path . ,(alist-get 'path file))
                                   (side . ,(if left "LEFT" "RIGHT"))
                                   (line . ,(alist-get (if left 'old 'new) row)))))))))))

(defun magit-auto-review--insert-placement-threads (review)
  "Insert every saved conversation in REVIEW, including unmatched threads."
  (dolist (thread (alist-get 'items (alist-get 'discussion review)))
    (when (equal (alist-get 'kind thread) "thread")
      (magit-insert-section (placement-thread (alist-get 'key thread))
        (magit-insert-heading
          (format "%s:%s%s%s" (alist-get 'path thread) (or (alist-get 'line thread) (alist-get 'original_line thread))
                  (if (magit-auto-review--true-p (alist-get 'is_resolved thread)) " · resolved" "")
                  (if (magit-auto-review--true-p (alist-get 'is_outdated thread)) " · outdated" "")))
        (magit-auto-review--insert-code
         (or (alist-get (intern (alist-get 'key thread)) (alist-get 'thread_contexts review)) '((rows . nil))))
        (dolist (entry (alist-get 'comments thread))
          (insert (format "@%s\n%s\n\n" (alist-get 'author entry)
                          (magit-auto-review--conversation-body (alist-get 'body entry)))))))))

(defun magit-auto-review--choose-placement ()
  "Attach to the thread/line/range chosen in the contextual picker."
  (interactive)
  (let ((placement (copy-tree (get-text-property (point) 'auto-review-choose)))
        (section (magit-current-section))
        (origin magit-auto-review--history-origin)
        (id magit-auto-review--placement-comment))
    (if (use-region-p)
        (let ((first (get-text-property (region-beginning) 'auto-review-choose))
              (last (get-text-property (1- (region-end)) 'auto-review-choose)))
          (unless (and first last (equal (alist-get 'path first) (alist-get 'path last))
                       (equal (alist-get 'side first) (alist-get 'side last)))
            (user-error "Choose a range on one side of one file"))
          (setq placement (copy-tree last))
          (setf (alist-get 'start_line placement) (alist-get 'line first)))
      (while (and section (not (eq (oref section type) 'placement-thread)))
        (setq section (oref section parent)))
      (when section (setq placement `((kind . "reply") (key . ,(oref section value))))))
    (unless placement (user-error "Move to a code line or a discussion"))
    (unless (buffer-live-p origin) (user-error "The original review buffer has been closed"))
    (with-current-buffer origin
      (magit-auto-review--mutate "placement" `(comment . ,id) `(placement . ,placement)))
    (quit-window t)
    (pop-to-buffer origin)))

(defun magit-auto-review-diff ()
  "Show the full reviewed PR diff from immutable Git revisions."
  (interactive)
  (let ((result (magit-auto-review--call (magit-auto-review--request "diff"))))
    (pop-to-buffer (generate-new-buffer "*Auto-review PR diff*"))
    (dolist (file (alist-get 'diffs result)) (insert (alist-get 'patch file)))
    (diff-mode)
    (magit-auto-review-file-mode 1)
    (goto-char (point-min))))

(defun magit-auto-review-adapt ()
  "Explicitly request wording that considers the full attached conversation."
  (interactive)
  (when (magit-auto-review--can-leave)
    (let ((id (magit-auto-review--id)) (origin (current-buffer)))
      (magit-auto-review--start
       (magit-auto-review--request "adapt" `(comment . ,id))
       (lambda (result)
         (let ((suggestion (alist-get 'body result)))
           (with-current-buffer (get-buffer-create "*Suggested review wording*")
             (let ((inhibit-read-only t)) (erase-buffer) (insert suggestion) (special-mode)))
           (display-buffer "*Suggested review wording*")
           (when (and (magit-auto-review--can-leave)
                      (yes-or-no-p "Use this proposed wording? Original saved text remains in history. "))
             (with-current-buffer origin
               ;; The request's original revision guards against intervening edits.
               (let* ((request (magit-auto-review--request "mutate" '(operation . "edit") `(comment . ,id) `(body . ,suggestion)))
                      (_ (setf (alist-get 'revision request) (alist-get 'revision result)))
                      (saved (magit-auto-review--call request)))
                 (magit-auto-review--accept (alist-get 'review saved) id))))))))))

(defun magit-auto-review-history ()
  "Browse all saved generations without changing the current review."
  (interactive)
  (let ((history (magit-auto-review--call (magit-auto-review--request "history")))
        (origin (current-buffer)))
    (pop-to-buffer (generate-new-buffer "*Auto-review history*"))
    (magit-section-mode)
    (setq-local magit-auto-review--history-origin origin)
    (let ((inhibit-read-only t))
      (magit-insert-section (history-root)
        (magit-insert-heading "Saved reviews · RET opens read-only context")
        (dolist (generation (alist-get 'generations history))
          (magit-insert-section (review-generation (alist-get 'generation generation))
            (magit-insert-heading
              (format "%s  %s  %-10s  %s · %d human edits · %d saved versions%s"
                      (substring (or (alist-get 'head generation) "unknown") 0 (min 12 (length (or (alist-get 'head generation) "unknown"))))
                      (or (alist-get 'updated_at generation) (alist-get 'created_at generation))
                      (alist-get 'status generation)
                      (if (equal (alist-get 'analysis_kind generation) "followup") "Limited follow-up" "Full review")
                      (alist-get 'human_edits generation)
                      (alist-get 'saved_versions generation)
                      (if (magit-auto-review--true-p (alist-get 'current generation)) " · current" "")))))))
    (use-local-map (copy-keymap magit-section-mode-map))
    (local-set-key (kbd "RET") #'magit-auto-review-open-generation)
    (local-set-key (kbd "q") #'quit-window)
    (goto-char (point-min))))

(defun magit-auto-review-open-generation ()
  "Open the saved generation under point in a separate contextual buffer."
  (interactive)
  (let* ((section (magit-current-section))
         (generation (and section (eq (oref section type) 'review-generation) (oref section value)))
         (origin magit-auto-review--history-origin)
         (session (buffer-local-value 'magit-auto-review--session origin))
         (directory (buffer-local-value 'default-directory origin)))
    (unless generation (user-error "Move to a saved generation"))
    (pop-to-buffer (generate-new-buffer "*Auto-review saved generation*"))
    (magit-auto-review-mode)
    (setq default-directory directory magit-auto-review--session session
          magit-auto-review--generation generation magit-auto-review--history-origin origin
          magit-auto-review--history-view t)
    (magit-auto-review-refresh)))

(defun magit-auto-review-recover ()
  "Recover the selected saved comment/version/document into a current draft."
  (interactive)
  (let* ((section (magit-current-section))
         (version (and section (eq (oref section type) 'review-version) (oref section value)))
         (document (and section (eq (oref section type) 'review-document) (oref section value)))
         (id (unless document (or (car version) (magit-auto-review--id))))
         (source magit-auto-review--generation)
         (origin (or magit-auto-review--history-origin (current-buffer))))
    (unless (buffer-live-p origin) (user-error "Open the current review first"))
    (with-current-buffer origin
      (when (magit-auto-review--can-leave)
        ;; Recovery always reads the current generation, even if the origin
        ;; has not yet learned that a newer generation completed.
        (magit-auto-review-refresh t)
        (let* ((choices (cons '("New Unstaged comment" . nil)
                              (mapcar (lambda (c) (cons (alist-get 'title c) (alist-get 'id c)))
                                      (cl-remove-if (lambda (c) (equal (alist-get 'status c) "published"))
                                                    (alist-get 'comments magit-auto-review--review)))))
               (target (cdr (assoc (completing-read "Recover wording into: " choices nil t) choices)))
               (result (magit-auto-review--mutate "recover" `(source_generation . ,source) `(source_comment . ,id)
                                                  `(version . ,(cdr version)) `(document . ,document) `(comment . ,target))))
          (magit-auto-review--render (car (alist-get 'changed result)))
          (magit-auto-review--goto-comment (car (alist-get 'changed result)) t)
          (pop-to-buffer origin))))))

(defun magit-auto-review-recover-edit ()
  "Save an unsaved/stale inline edit as a new Unstaged draft in the current review."
  (interactive)
  (unless magit-auto-review--editor (user-error "No unsaved inline edit is active"))
  (let* ((body (buffer-substring-no-properties (plist-get magit-auto-review--editor :start) (plist-get magit-auto-review--editor :end)))
         (current (magit-auto-review--call `((command . "read") (session . ,magit-auto-review--session))))
         (request `((command . "mutate") (session . ,magit-auto-review--session)
                    (generation . ,(alist-get 'generation current)) (revision . ,(alist-get 'revision current))
                    (operation . "create") (title . "Recovered unsaved comment") (body . ,body)))
         (result (magit-auto-review--call request)))
    (magit-auto-review--finish-edit)
    (magit-auto-review--accept (alist-get 'review result) (car (alist-get 'changed result)))))

(defun magit-auto-review-browse ()
  "Open the posted comment at point, or this PR, in the browser."
  (interactive)
  (let* ((comment (ignore-errors (magit-auto-review--comment (magit-auto-review--id))))
         (url (or (alist-get 'html_url (alist-get 'receipt comment))
                  (alist-get 'url magit-auto-review--review))))
    (unless url (user-error "No GitHub URL is available here"))
    (browse-url url)))

(defun magit-auto-review--mutable-p ()
  "Whether the displayed review supports draft mutations."
  (and magit-auto-review--review (not magit-auto-review--editor)
       (not magit-auto-review--history-view)
       (not (magit-auto-review--true-p (alist-get 'historical magit-auto-review--review)))))

(defun magit-auto-review--status-p (&rest statuses)
  "Whether the comment at point has one of STATUSES."
  (when-let ((comment (ignore-errors (magit-auto-review--comment (magit-auto-review--id)))))
    (member (alist-get 'status comment) statuses)))

(defun magit-auto-review--draft-p ()
  "Whether point is on a mutable draft."
  (and (magit-auto-review--mutable-p) (magit-auto-review--status-p "unstaged" "staged" "suppressed")))

(defun magit-auto-review--code-context-p ()
  "Whether a reviewed file can be opened from point."
  (when-let ((comment (ignore-errors (magit-auto-review--comment (magit-auto-review--id)))))
    (let ((context (alist-get 'context comment)))
      (and (alist-get 'path context) (alist-get 'line context)
           (not (alist-get 'file_unavailable context))))))

(defun magit-auto-review--evidence-p ()
  "Whether the selected finding has additional code locations."
  (when-let ((comment (ignore-errors (magit-auto-review--comment (magit-auto-review--id)))))
    (alist-get 'locations comment)))

(defun magit-auto-review--recoverable-p ()
  "Whether point selects historical text that can be recovered."
  (or (and (or magit-auto-review--history-view
               (magit-auto-review--true-p (alist-get 'historical magit-auto-review--review)))
           (magit-auto-review--comment-section))
      (when-let ((section (magit-current-section)))
        (memq (oref section type) '(review-version review-document)))))

(defun magit-auto-review--stage-context-p (status)
  "Whether point is in a mutable STATUS group or one of its comments."
  (and (magit-auto-review--mutable-p)
       (or (magit-auto-review--status-p status)
           (when-let ((section (magit-current-section)))
             (and (eq (oref section type) 'review-group) (equal (oref section value) status))))))

(defun magit-auto-review--reply-draft-p ()
  "Whether adaptation to a discussion applies here."
  (and (magit-auto-review--draft-p)
       (alist-get 'thread (alist-get 'context (magit-auto-review--comment (magit-auto-review--id))))))

(defun magit-auto-review--attention-p ()
  "Whether a changed discussion needs acknowledgement here."
  (and (magit-auto-review--draft-p)
       (alist-get 'needs_attention (magit-auto-review--comment (magit-auto-review--id)))))

(defun magit-auto-review--uncertain-publication-p ()
  "Whether publication recovery is relevant."
  (cl-some (lambda (batch) (cl-some (lambda (action) (member (alist-get 'status action) '("submitting" "ambiguous")))
                                   (alist-get 'actions batch)))
           (alist-get 'batches magit-auto-review--review)))

(transient-define-prefix magit-auto-review-publish-menu ()
  "Publish exactly the comments shown under Staged."
  :refresh-suffixes t
  [:description (lambda () (concat "Publish Staged · Overall PR verdict: " (magit-auto-review--verdict-label)))
   ("v" "Choose overall PR verdict" magit-auto-review-set-verdict :transient t)
   ("p" "Publish staged comments" magit-auto-review-publish)
   ("r" "Check uncertain submissions on GitHub" magit-auto-review-resolve-publication :if magit-auto-review--uncertain-publication-p)
   ("q" "Close menu" transient-quit-one)])

(transient-define-prefix magit-auto-review-dispatch ()
  "Inspect, edit, stage, and publish contextual review comments."
  [["Comments"
    ("s" "Move to Staged" magit-auto-review-stage :if (lambda () (magit-auto-review--stage-context-p "unstaged")))
    ("u" "Move to Unstaged" magit-auto-review-unstage :if (lambda () (magit-auto-review--stage-context-p "staged")))
    ("e" "Edit displayed comment" magit-auto-review-edit :if magit-auto-review--draft-p)
    ("a" "Inspect/change placement" magit-auto-review-placement :if magit-auto-review--draft-p)
    ("k" "Move to Suppressed" magit-auto-review-suppress :if (lambda () (and (magit-auto-review--draft-p) (magit-auto-review--status-p "unstaged" "staged"))))
    ("r" "Restore to Unstaged" magit-auto-review-restore :if (lambda () (magit-auto-review--stage-context-p "suppressed")))
    ("c" "New comment at code cursor" magit-auto-review-new-comment :if magit-auto-review--mutable-p)
    ("f" "New follow-up draft" magit-auto-review-follow-up :if (lambda () (and (magit-auto-review--mutable-p) (magit-auto-review--status-p "published"))))]
   ["Context and history"
    ("C" "Conversations and follow-up" magit-auto-review-conversations :if (lambda () magit-auto-review--review))
    ("RET" "Reviewed file" magit-auto-review-visit :if magit-auto-review--code-context-p)
    ("d" "Full PR diff" magit-auto-review-diff :if (lambda () magit-auto-review--review))
    ("l" "Other evidence" magit-auto-review-evidence :if magit-auto-review--evidence-p)
    ("h" "Saved generations" magit-auto-review-history :if (lambda () magit-auto-review--review))
    ("A" "Suggest reply using full discussion (model)" magit-auto-review-adapt :if magit-auto-review--reply-draft-p)
    ("i" "Mark changed discussion as inspected" magit-auto-review-acknowledge :if magit-auto-review--attention-p)
    ("b" "Open comment / PR on GitHub" magit-auto-review-browse)]
   ["Review"
    ("g" "Refresh results (history stays pinned)" magit-auto-review-refresh :if (lambda () magit-auto-review--review))
    ("R" "Configure and run/resume…" magit-auto-review-run)
    ("N" "Open current completed review" magit-auto-review-switch-current :if (lambda () (or magit-auto-review--new-ready magit-auto-review--history-view)))
    ("L" "Progress and receipt URLs" magit-auto-review-log)
    ("v" "Choose overall PR verdict" magit-auto-review-set-verdict :if magit-auto-review--mutable-p)
    ("P" "Publish Staged…" magit-auto-review-publish-menu :if magit-auto-review--mutable-p)
    ("q" "Close menu" transient-quit-one)]
   ["Advanced"
    ("G" "Refresh GitHub context" magit-auto-review-refresh-github :if magit-auto-review--mutable-p)
    ("K" "Cancel model operation" magit-auto-review-cancel-operation :if (lambda () magit-auto-review--operation))
    ("T" "Confirm agent configuration" magit-auto-review-trust-config)
    ("y" "Recover saved wording" magit-auto-review-recover :if magit-auto-review--recoverable-p)]])

(dolist (binding '(("a" . magit-auto-review-placement) ("r" . magit-auto-review-restore)
                   ("C" . magit-auto-review-conversations)
                   ("R" . magit-auto-review-run) ("N" . magit-auto-review-switch-current)
                   ("G" . magit-auto-review-refresh-github) ("L" . magit-auto-review-log)
                   ("l" . magit-auto-review-evidence) ("y" . magit-auto-review-recover)
                   ("c" . magit-auto-review-new-comment) ("f" . magit-auto-review-follow-up)
                   ("A" . magit-auto-review-adapt) ("i" . magit-auto-review-acknowledge)
                   ("v" . magit-auto-review-set-verdict) ("b" . magit-auto-review-browse)
                   ("K" . magit-auto-review-cancel-operation)
                   ("d" . magit-auto-review-diff) ("C-c C-r" . magit-auto-review-recover-edit)))
  (define-key magit-auto-review-mode-map (kbd (car binding)) (cdr binding)))

(with-eval-after-load 'forge-commands
  (unless (ignore-errors (transient-get-suffix 'forge-dispatch "V"))
    (transient-append-suffix 'forge-dispatch "v p" '("V" "Auto-review" magit-auto-review))))
(with-eval-after-load 'forge-topic
  (unless (ignore-errors (transient-get-suffix 'forge-topic-menu "V"))
    (transient-append-suffix 'forge-topic-menu "/r" '("V" "Auto-review" magit-auto-review))))
(with-eval-after-load 'magit
  (unless (ignore-errors (transient-get-suffix 'magit-dispatch "C-c r"))
    (transient-append-suffix 'magit-dispatch "!" '("C-c r" "Auto-review" magit-auto-review))))

(provide 'magit-auto-review-actions)
;;; magit-auto-review-actions.el ends here
