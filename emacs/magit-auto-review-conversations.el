;;; magit-auto-review-conversations.el --- Follow review conversations -*- lexical-binding: t; -*-
;; SPDX-License-Identifier: GPL-3.0-or-later
;;; Commentary:
;; Saved conversation state is independent of drafts and review verdicts.
;;; Code:
(require 'magit-auto-review)
(declare-function magit-auto-review--compose-comment "magit-auto-review-actions" (title placement &optional body))
(declare-function magit-auto-review--start "magit-auto-review-actions" (request callback))
(declare-function magit-auto-review--mutable-p "magit-auto-review-actions" ())
(defvar-local magit-auto-review--conversation-origin nil)
(defvar-local magit-auto-review--followup-request nil)

(defun magit-auto-review--thread-at-point ()
  "Return the selected conversation key, including from child sections."
  (let ((section (magit-current-section)))
    (while (and section (not (eq (oref section type) 'review-conversation)))
      (setq section (oref section parent)))
    (and section (oref section value))))

(defun magit-auto-review--thread-context (&optional key)
  "Return context for KEY or the conversation at point."
  (alist-get (intern (or key (magit-auto-review--thread-at-point) ""))
             (alist-get 'thread_contexts magit-auto-review--review)))

(defun magit-auto-review--insert-review-status ()
  "Insert the saved GitHub status, independently of the next verdict."
  (let* ((status (alist-get 'review_status magit-auto-review--review))
         (opinion (alist-get 'opinionated status))
         (activity (alist-get 'latest_activity status))
         (coverage (alist-get 'coverage magit-auto-review--review))
         (threads (cl-remove-if-not (lambda (x) (equal (alist-get 'kind x) "thread"))
                                   (alist-get 'items (alist-get 'discussion magit-auto-review--review))))
         (unresolved (cl-remove-if (lambda (x) (magit-auto-review--true-p (alist-get 'is_resolved x))) threads)))
    (insert (format "GitHub review status: %s · Your submitted decision: %s\n"
                    (or (alist-get 'decision status) "unavailable")
                    (if (magit-auto-review--true-p (alist-get 'available status))
                        (or (alist-get 'state opinion) "none") "unavailable")))
    (when opinion
      (insert (format "Decision at %s · %s\n" (or (alist-get 'oid (alist-get 'commit opinion)) "unknown commit")
                      (or (alist-get 'submittedAt opinion) "unknown time")))
      (when-let ((url (alist-get 'url opinion)))
        (insert-text-button "Open submitted review" 'follow-link t 'action (lambda (_) (browse-url url)))
        (insert "\n")))
    (when (and activity (not (equal (alist-get 'id activity) (alist-get 'id opinion))))
      (insert (format "Latest review activity: %s (separate from the submitted decision)\n" (alist-get 'state activity))))
    (when (equal (alist-get 'kind coverage) "followup")
      (insert (propertize (format "Limited follow-up: %d selected conversations; the whole PR was not reviewed.\n"
                                  (length (alist-get 'threads coverage))) 'face 'warning)))
    (insert (format "C Conversations: %d unresolved (%d outdated); %d resolved\n\n"
                    (length unresolved)
                    (cl-count-if (lambda (x) (magit-auto-review--true-p (alist-get 'is_outdated x))) unresolved)
                    (- (length threads) (length unresolved))))))

(defvar magit-auto-review-conversations-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-auto-review-mode-map)
    (dolist (binding '(("c" . magit-auto-review-conversation-reply)
                       ("F" . magit-auto-review-check-conversations)
                       ("x" . magit-auto-review-resolve-thread)
                       ("X" . magit-auto-review-reopen-thread)
                       ("b" . magit-auto-review-browse-thread)
                       ("RET" . magit-auto-review-visit-thread)
                       ("?" . magit-auto-review-conversations-menu)))
      (define-key map (kbd (car binding)) (cdr binding)))
    map))

(define-derived-mode magit-auto-review-conversations-mode magit-auto-review-mode "PR Conversations"
  "Inspect conversations and explicitly act on their state.")

(defun magit-auto-review-conversations ()
  "Open all saved conversations without network or model work."
  (interactive)
  (when (magit-auto-review--can-leave)
    (unless magit-auto-review--review (user-error "Open a saved review first"))
    (let ((origin (current-buffer)) (review magit-auto-review--review)
          (history magit-auto-review--history-view) (directory default-directory))
      (pop-to-buffer (get-buffer-create (concat (buffer-name origin) " Conversations")))
      (unless (derived-mode-p 'magit-auto-review-conversations-mode)
        (magit-auto-review-conversations-mode))
      (setq default-directory directory magit-auto-review--conversation-origin origin
            magit-auto-review--history-view history)
      (magit-auto-review--accept review))))

(defun magit-auto-review--render-conversations ()
  "Render conversations, retaining folds and the selected thread."
  (let ((inhibit-read-only t) (key (magit-auto-review--thread-at-point))
        (folds (make-hash-table :test #'equal)))
    (magit-auto-review--walk-sections
     (lambda (s) (puthash (magit-auto-review--fold-key s) (oref s hidden) folds)))
    (erase-buffer)
    (magit-insert-section (review-conversations)
      (insert (format "Conversations for PR #%s\n\n" (alist-get 'pr magit-auto-review--review)))
      (magit-auto-review--insert-review-status)
      (insert (format "Next review verdict: %s (v to choose)\n" (magit-auto-review--verdict-label)))
      (insert "c Reply draft · F Check selected conversations · x Resolve · X Reopen · G Refresh GitHub\n\n")
      (dolist (resolved '(nil t))
        (let ((threads (cl-remove-if-not
                        (lambda (x) (and (equal (alist-get 'kind x) "thread")
                                         (eq (magit-auto-review--true-p (alist-get 'is_resolved x)) resolved)))
                        (alist-get 'items (alist-get 'discussion magit-auto-review--review)))))
          (magit-insert-section (conversation-group resolved resolved)
            (magit-insert-heading (format "%s (%d)" (if resolved "Resolved" "Unresolved") (length threads)))
            (dolist (thread threads)
              (let* ((identity (alist-get 'key thread))
                     (context (magit-auto-review--thread-context identity))
                     (assessment (cl-find identity (alist-get 'followup_assessments magit-auto-review--review)
                                          :key (lambda (x) (alist-get 'thread_key x)) :test #'equal)))
                (magit-insert-section (review-conversation identity t)
                  (magit-insert-heading
                    (format "%s:%s · %s%s%s%s"
                            (or (alist-get 'path thread) "file")
                            (or (alist-get 'line thread) (alist-get 'original_line thread) "file")
                            (truncate-string-to-width (replace-regexp-in-string "\n" " " (or (alist-get 'body (car (alist-get 'comments thread))) "Conversation")) 70 nil nil t)
                            (if (magit-auto-review--true-p (alist-get 'is_outdated thread)) " · outdated" "")
                            (if (magit-auto-review--true-p (alist-get 'participated context)) " · participated" "")
                            (if assessment (concat " · " (magit-auto-review--followup-label assessment)) "")))
                  (when assessment
                    (insert (format "Model assessment: %s%s\nChecked commit: %s\n%s\nLimitations: %s\n\n"
                                    (magit-auto-review--followup-label assessment)
                                    (if (magit-auto-review--true-p (alist-get 'stale assessment)) " · STALE" "")
                                    (alist-get 'head assessment) (alist-get 'evidence assessment)
                                    (alist-get 'limitations assessment))))
                  (magit-auto-review--insert-code context)
                  (when (alist-get 'error context) (insert (format "Code context unavailable: %s\n" (alist-get 'error context))))
                  (dolist (entry (alist-get 'comments thread))
                    (insert (format "@%s · %s\n%s\n\n" (alist-get 'author entry) (alist-get 'created_at entry)
                                    (magit-auto-review--conversation-body (alist-get 'body entry)))))
                  (when (alist-get 'resolved_by thread)
                    (insert (format "Resolved by @%s\n" (alist-get 'resolved_by thread)))))))))))
    (magit-auto-review--walk-sections
     (lambda (s)
       (unless (eq s magit-root-section)
         (oset s hidden (gethash (magit-auto-review--fold-key s) folds (oref s hidden))))))
    (magit-section-show magit-root-section)
    (goto-char (point-min))
    (when key
      (magit-auto-review--walk-sections
       (lambda (s) (when (and (eq (oref s type) 'review-conversation) (equal (oref s value) key))
                     (magit-section-show (oref s parent))
                     (goto-char (oref s start))))))
    (set-buffer-modified-p nil))
  (magit-auto-review--header))

(defun magit-auto-review--followup-label (assessment)
  "Return a readable outcome for ASSESSMENT."
  (concat (or (cdr (assoc (alist-get 'outcome assessment)
                         '(("ADDRESSED" . "Addressed") ("STILL_PRESENT" . "Still present")
                           ("NOT_APPLICABLE" . "No longer applicable") ("UNCERTAIN" . "Uncertain"))))
              "Uncertain")
          (if (magit-auto-review--true-p (alist-get 'stale assessment)) " (stale)" "")))

(defun magit-auto-review-conversation-reply ()
  "Compose an attached reply, optionally using a fresh saved proposal."
  (interactive)
  (magit-auto-review--editable)
  (when (and (buffer-live-p magit-auto-review--conversation-origin)
             (buffer-local-value 'magit-auto-review--editor magit-auto-review--conversation-origin))
    (user-error "Finish the inline edit in the drafts buffer before creating another reply"))
  (let* ((key (or (magit-auto-review--thread-at-point) (user-error "Choose a conversation")))
         (assessment (cl-find key (alist-get 'followup_assessments magit-auto-review--review)
                              :key (lambda (x) (alist-get 'thread_key x)) :test #'equal))
         (body (if (magit-auto-review--true-p (alist-get 'stale assessment)) ""
                 (or (alist-get 'suggested_reply assessment) "")))
         (review magit-auto-review--review)
         (origin magit-auto-review--conversation-origin)
         (directory default-directory))
    (pop-to-buffer (if (buffer-live-p origin) origin (get-buffer-create "*Auto-review drafts*")))
    (unless (derived-mode-p 'magit-auto-review-mode) (magit-auto-review-mode))
    (setq default-directory directory)
    (magit-auto-review--accept review)
    (magit-auto-review--compose-comment "Conversation reply" `((kind . "reply") (key . ,key)) body)))

(defun magit-auto-review--change-thread (resolved)
  "Explicitly change the selected thread to RESOLVED."
  (magit-auto-review--editable)
  (let ((key (or (magit-auto-review--thread-at-point) (user-error "Choose a conversation"))))
    (magit-auto-review--start
     (magit-auto-review--request "thread-state" (cons 'thread key)
                                (cons 'resolved (if resolved t :false))
                                (cons 'expected (alist-get 'expected (magit-auto-review--thread-context key))))
     (lambda (result)
       (magit-auto-review--accept (alist-get 'review result))
       (magit-auto-review--notify (alist-get 'reason result))))))
(defun magit-auto-review-resolve-thread () "Resolve the selected conversation." (interactive) (magit-auto-review--change-thread t))
(defun magit-auto-review-reopen-thread () "Reopen the selected conversation." (interactive) (magit-auto-review--change-thread nil))

(defun magit-auto-review--launch-followup (request)
  "Run a frozen follow-up REQUEST, including any explicit snapshot trust."
  (setq magit-auto-review--followup-request (copy-tree request))
  (magit-auto-review--start
   request (lambda (result)
             (setq magit-auto-review--new-ready (not (equal magit-auto-review--generation (alist-get 'generation result))))
             (magit-auto-review--notify "Follow-up completed. g opens the assessments; earlier work remains in history."))))

(defun magit-auto-review-check-conversations ()
  "Check the conversation at point or selected sibling conversations."
  (interactive)
  (magit-auto-review--editable)
  (let ((keys (when (use-region-p)
                (let (values)
                  (magit-auto-review--walk-sections
                   (lambda (s) (when (and (eq (oref s type) 'review-conversation)
                                         (<= (region-beginning) (oref s start)) (< (oref s start) (region-end)))
                                 (push (oref s value) values))))
                  (nreverse values)))))
    (unless keys (setq keys (list (or (magit-auto-review--thread-at-point) (user-error "Choose conversations to check")))))
    (let ((settings (alist-get 'settings (magit-auto-review--call (magit-auto-review--request "settings")))))
      (magit-auto-review--launch-followup
       (append (magit-auto-review--request "followup" (cons 'threads (vconcat keys))) settings)))))

(defun magit-auto-review-browse-thread ()
  "Open this conversation on GitHub."
  (interactive)
  (browse-url (or (alist-get 'url (alist-get 'thread (magit-auto-review--thread-context)))
                  (alist-get 'url magit-auto-review--review))))
(defun magit-auto-review-visit-thread ()
  "Visit the saved code revision for this conversation."
  (interactive)
  (let ((context (magit-auto-review--thread-context)))
    (unless (and (alist-get 'commit context) (alist-get 'line context))
      (user-error "Original file revision is unavailable; inspect the saved diff hunk"))
    (magit-auto-review-visit context)))
(defun magit-auto-review--can-change-thread-p (resolve)
  "Whether the displayed permissions allow RESOLVE (or reopening)."
  (and (magit-auto-review--mutable-p)
       (let ((thread (alist-get 'thread (magit-auto-review--thread-context))))
         (and thread (not (eq (magit-auto-review--true-p (alist-get 'is_resolved thread)) resolve))
              (magit-auto-review--true-p (alist-get (if resolve 'can_resolve 'can_unresolve) thread))))))

(transient-define-prefix magit-auto-review-conversations-menu ()
  "Inspect and follow up on GitHub conversations."
  [["Conversations"
    ("c" "Create reply draft" magit-auto-review-conversation-reply :if magit-auto-review--mutable-p)
    ("F" "Check selected conversations (model)" magit-auto-review-check-conversations :if magit-auto-review--mutable-p)
    ("x" "Resolve conversation" magit-auto-review-resolve-thread :if (lambda () (magit-auto-review--can-change-thread-p t)))
    ("X" "Reopen conversation" magit-auto-review-reopen-thread :if (lambda () (magit-auto-review--can-change-thread-p nil)))
    ("b" "Open conversation on GitHub" magit-auto-review-browse-thread)]
   ["Review"
    ("g" "Load latest saved results" magit-auto-review-refresh)
    ("G" "Refresh GitHub context" magit-auto-review-refresh-github :if magit-auto-review--mutable-p)
    ("v" "Choose next verdict" magit-auto-review-set-verdict :if magit-auto-review--mutable-p)
    ("P" "Publish staged comments / verdict" magit-auto-review-publish-menu :if magit-auto-review--mutable-p)
    ("q" "Close menu" transient-quit-one)]
   ["Advanced"
    ("T" "Confirm agent configuration" magit-auto-review-trust-config)
    ("K" "Cancel model operation" magit-auto-review-cancel-operation :if (lambda () magit-auto-review--operation))
    ("L" "Progress and receipts" magit-auto-review-log)]])

(provide 'magit-auto-review-conversations)
;;; magit-auto-review-conversations.el ends here
