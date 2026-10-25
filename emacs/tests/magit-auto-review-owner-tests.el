;;; magit-auto-review-owner-tests.el --- Owner behavior -*- lexical-binding: t; -*-
(require 'ert)
(require 'magit-auto-review-owner)

(defun owner-test-view ()
  "Build a saved owner view with feedback and drafts."
  (copy-tree
   '((session . "/test/owner") (revision . 3) (pr . 7) (repository . "github.com/org/repo")
     (checked_at . "2026-10-25") (url . "https://github.com/org/repo/pull/7")
     (settings . ((fix . ((provider . "claude") (model . "fable") (effort . "medium")))
                  (review . ((provider . "codex") (model . "chosen") (effort . "high")))
                  (verification . "focused")))
     (feedback . (((key . "thread:T1") (kind . "thread") (attention . t) (actionable . t) (unread . t)
                   (is_outdated . t) (is_resolved . :false) (path . "source.py")
                   (comments . (((author . "reviewer") (body . "First concern") (url . "https://example.com/1")))))
                  ((key . "thread:T2") (kind . "thread") (attention . t) (actionable . t) (unread . :false)
                   (is_resolved . :false) (comments . (((author . "reviewer") (body . "Second concern")))))
                  ((key . "comment:3") (kind . "comment") (attention . :false) (actionable . t)
                   (comments . (((author . "reviewer") (body . "Deferred concern")))))))
     (replies . (((id . "r1") (source . "thread:T1") (status . "staged") (body . "Fixed in HASH"))
                 ((id . "r2") (source . "thread:T2") (status . "published") (body . "Published reply"))))
     (batches . (((id . "b1") (status . "ready") (created_at . "today") (baseline . "abc")
                  (items . ("thread:T1" "thread:T2"))
                  (tasks . (((id . "t1") (status . "complete") (rationale . "Shared fix")
                             (expected . (((item . "thread:T1") (outcome . "First fixed")) ((item . "thread:T2") (outcome . "Second fixed")))) (items . ("thread:T1" "thread:T2")))))))))))

(defmacro owner-test-buffer (&rest body)
  `(with-temp-buffer
     (magit-auto-review-owner-mode)
     (magit-auto-review-owner--accept (owner-test-view))
     ,@body))

(defun owner-test-goto (type value)
  "Visit TYPE section VALUE."
  (let ((section (cl-find-if (lambda (s) (and (eq (oref s type) type) (equal (oref s value) value)))
                              magit-auto-review-owner--sections)))
    (should section)
    (goto-char (oref section start))
    section))

(defun owner-test-local-view ()
  "Return imported findings followed by unrelated incoming feedback."
  (let ((view (owner-test-view)))
    (setf (alist-get 'feedback view)
          (append
           (cl-loop for key in '("local:first" "local:second") collect
                    `((key . ,key) (kind . "local_review") (title . ,key) (attention . t) (actionable . t)
                      (source . ((snapshot . ((head . "reviewed-sha"))) (finding_ids . ("F1"))))
                      (review_context . ((path . "source.py") (line . 1)
                                         (rows . (((new . 1) (kind . "+") (text . "saved code context") (highlight . t))))))
                      (evidence . ("Traced the concern"))
                      (comments . (((author . "Saved reviewer finding") (body . "Preserved local wording"))))))
           (list (car (alist-get 'feedback view)))))
    view))

(ert-deftest owner/import-selects-only-handed-off-items-for-f ()
  (save-window-excursion
    (let ((transient-mark-mode t) buffer request)
      (unwind-protect
          (progn
            (magit-auto-review-owner-show-import
             `((owner . ,(owner-test-local-view)) (items . ("local:first" "local:second"))) temporary-file-directory)
            (setq buffer (current-buffer))
            (should (use-region-p))
            (should (string-match-p "saved code context" (buffer-string)))
            (should (string-match-p "Preserved local wording" (buffer-string)))
            (should (string-match-p "No GitHub reply destination" (buffer-string)))
            (cl-letf (((symbol-function 'magit-auto-review-owner--start) (lambda (r &optional _cb) (setq request r))))
              (magit-auto-review-owner-fix))
            (should (equal (alist-get 'items request) ["local:first" "local:second"])))
        (when (buffer-live-p buffer) (kill-buffer buffer))))))

(ert-deftest owner/own-comments-are-visible-and-fixable-without-an-alert ()
  (owner-test-buffer
   (let ((view (owner-test-view)) request)
     (setf (alist-get 'feedback view)
           '(((key . "thread:SELF") (kind . "thread") (own_only . t) (triage . "available")
              (actionable . t) (attention . :false) (unread . :false) (is_resolved . :false)
              (comments . (((author . "me") (body . "My review feedback")))))))
     (magit-auto-review-owner--accept view)
     (should (string-match-p "Your GitHub comments (1)" (buffer-string)))
     (should-not (string-match-p "NEW" (buffer-string)))
     (owner-test-goto 'owner-group "own")
     (cl-letf (((symbol-function 'magit-auto-review-owner--start) (lambda (r &optional _cb) (setq request r))))
       (magit-auto-review-owner-fix))
     (should (equal (alist-get 'items request) ["thread:SELF"]))
     (should (= 1 (cl-count-if (lambda (s) (eq (oref s type) 'owner-feedback)) magit-auto-review-owner--sections))))))

(ert-deftest owner/local-finding-does-not-open-a-github-reply-editor ()
  (owner-test-buffer
   (magit-auto-review-owner--accept (owner-test-local-view))
   (owner-test-goto 'owner-feedback "local:first")
   (cl-letf (((symbol-function 'magit-auto-review-owner--editor) (lambda (&rest _) (ert-fail "No reply target"))))
     (should-error (magit-auto-review-owner-compose) :type 'user-error))
   (should-error (magit-auto-review-owner-resolve) :type 'user-error)))

(ert-deftest owner/separate-keymap-and-complete-context ()
  (owner-test-buffer
   (should-not (derived-mode-p 'magit-auto-review-mode))
   (should (eq (key-binding "f") #'magit-auto-review-owner-fix))
   (should-not (lookup-key magit-auto-review-owner-mode-map "F"))
   (should (eq (key-binding "s") #'magit-auto-review-owner-stage))
   (should (string-match-p "Outdated" (buffer-string)))
   (should (string-match-p "Thread: unresolved" (buffer-string)))
   (should (string-match-p "First concern" (buffer-string)))
   (should-not (string-match-p "Request changes.*verdict" (buffer-string)))))

(ert-deftest owner/group-f-selects-attention-only-in-display-order ()
  (owner-test-buffer
   (owner-test-goto 'owner-group "attention")
   (let (request)
     (cl-letf (((symbol-function 'magit-auto-review-owner--start) (lambda (r &optional _cb) (setq request r))))
       (magit-auto-review-owner-fix))
     (should (equal (alist-get 'command request) "owner-fix"))
     (should (equal (alist-get 'items request) ["thread:T1" "thread:T2"]))
     (should (= (alist-get 'revision request) 3)))))

(ert-deftest owner/single-item-f-and-verification ()
  (owner-test-buffer
   (owner-test-goto 'owner-feedback "thread:T2")
   (let (request)
     (cl-letf (((symbol-function 'magit-auto-review-owner--start) (lambda (r &optional _cb) (setq request r))))
       (magit-auto-review-owner-verify))
     (should (equal (alist-get 'items request) ["thread:T2"]))
     (should (eq (alist-get 'verify_only request) t)))))

(ert-deftest owner/refresh-preserves-fold-and-heading-selection ()
  (owner-test-buffer
   (let ((section (owner-test-goto 'owner-feedback "thread:T1")))
     (magit-section-show section))
   (cl-letf (((symbol-function 'magit-auto-review--call)
              (lambda (request)
                (should (equal (alist-get 'command request) "owner-read"))
                (owner-test-view))))
     (magit-auto-review-owner-refresh))
   (should (equal (oref (magit-current-section) value) "thread:T1"))
   (should-not (oref (magit-current-section) hidden))))

(ert-deftest owner/stage-group-addresses-outgoing-replies-only ()
  (owner-test-buffer
   (owner-test-goto 'owner-reply-group "staged")
   (let (request)
     (cl-letf (((symbol-function 'magit-auto-review--call) (lambda (r) (setq request r) (owner-test-view))))
       (magit-auto-review-owner-unstage))
     (should (equal (alist-get 'replies request) ["r1"]))
     (should (equal (alist-get 'operation request) "unstage")))))

(ert-deftest owner/published-reply-is-not-editable ()
  (owner-test-buffer
   (owner-test-goto 'owner-reply "r2")
   (should-error (magit-auto-review-owner-edit) :type 'user-error)))

(ert-deftest owner/stale-save-retains-editor-text ()
  (with-temp-buffer
    (magit-auto-review-owner-edit-mode)
    (setq magit-auto-review-owner--editor-request '((command . "owner-mutate") (revision . 1)))
    (insert "Keep this exact wording")
    (cl-letf (((symbol-function 'magit-auto-review--call)
               (lambda (_request) (signal 'magit-auto-review-api-error '("Stale revision")))))
      (should-error (magit-auto-review-owner-save-reply) :type 'magit-auto-review-api-error))
    (should (equal (buffer-string) "Keep this exact wording"))
    (should (buffer-modified-p))))

(ert-deftest owner/publication-previews-and-requires-explicit-confirmation ()
  (save-window-excursion
    (owner-test-buffer
     (let (sent)
       (cl-letf (((symbol-function 'magit-auto-review-owner--start)
                  (lambda (request &optional cb)
                    (push (alist-get 'command request) sent)
                    (when cb (funcall cb `((owner . ,(owner-test-view)) (token . "token")
                                           (replies . (((source . "thread:T1") (body . "Fixed in HASH")))))))))
                 ((symbol-function 'yes-or-no-p) (lambda (&rest _) nil)))
         (magit-auto-review-owner-publish))
       (should (equal sent '("owner-prepare")))
       (with-current-buffer "*Owner publication preview*"
         (should (string-match-p "thread:T1\nFixed in HASH" (buffer-string))))))))

(ert-deftest owner/remembered-toolkit-routes-pr-without-reviewer-discovery ()
  (let (opened)
    (cl-letf (((symbol-function 'magit-auto-review--call)
               (lambda (request)
                 (should (equal (alist-get 'command request) "toolkit"))
                 '((toolkit . "owner"))))
              ((symbol-function 'magit-auto-review-owner-open)
               (lambda (directory number &optional repository) (setq opened (list directory number repository)))))
      (magit-auto-review--open-pr "/repo" 7 "github.com/org/repo"))
    (should (equal opened '("/repo" 7 "github.com/org/repo")))))

(ert-deftest owner/settings-use-metadata-without-running-inference ()
  (owner-test-buffer
   (let (request mutated (answers '("codex" "chosen" "medium")))
     (cl-letf (((symbol-function 'completing-read) (lambda (&rest _) (pop answers)))
               ((symbol-function 'magit-auto-review-owner--start)
                (lambda (r &optional cb)
                  (setq request r)
                  (funcall cb '((models . (((value . "chosen") (efforts . ("medium" "high")))))))))
               ((symbol-function 'magit-auto-review-owner--mutate)
                (lambda (_op &rest fields) (setq mutated (alist-get 'settings fields)))))
       (magit-auto-review-owner-configure "review"))
     (should (equal (alist-get 'command request) "models"))
     (should (equal (alist-get 'effort (alist-get 'review mutated)) "medium"))
     (should (equal (alist-get 'provider (alist-get 'fix mutated)) "claude")))))

(ert-deftest owner/native-magit-handoff-uses-owner-worktree ()
  (owner-test-buffer
   (setf (alist-get 'worktree magit-auto-review-owner--view) temporary-file-directory)
   (let (opened)
     (cl-letf (((symbol-function 'magit-status-setup-buffer) (lambda (path &rest _) (setq opened path))))
       (magit-auto-review-owner-magit))
     (should (equal opened temporary-file-directory)))))

(ert-deftest owner/partial-diff-requires-explicit-prefix-and-current-revision ()
  (save-window-excursion
    (owner-test-buffer
     (owner-test-goto 'owner-batch "b1")
     (let (request)
       (cl-letf (((symbol-function 'magit-auto-review--call)
                  (lambda (r) (setq request r) '((diff . "diff --git a/x b/x\n")))))
         (magit-auto-review-owner-diff t))
       (should (eq (alist-get 'current request) t))
       (should (= (alist-get 'revision request) 3))
       (should (equal (alist-get 'batch request) "b1"))))))

(ert-deftest owner/trust-shows-exact-config-before-confirmation ()
  (save-window-excursion
    (owner-test-buffer
     (setq magit-auto-review-owner--trust "snapshot-token"
           magit-auto-review-owner--trust-paths '("AGENTS.md")
           magit-auto-review-owner--trust-diff "+Changed instructions\n")
     (let (request)
       (cl-letf (((symbol-function 'yes-or-no-p)
                  (lambda (&rest _)
                    (with-current-buffer "*Owner agent configuration*"
                      (should (string-match-p "+Changed instructions" (buffer-string))))
                    t))
                 ((symbol-function 'magit-auto-review-owner--start) (lambda (r &optional _cb) (setq request r))))
         (magit-auto-review-owner-trust))
       (should (equal (alist-get 'trusted_snapshot request) "snapshot-token"))
       (should (equal (alist-get 'batch request) "b1"))))))

(ert-deftest owner/active-buffer-keeps-operation-controls-available ()
  (owner-test-buffer
   (cl-letf (((symbol-function 'process-live-p) (lambda (_) t)))
     (should-not (magit-auto-review-owner--can-close)))
   (should (magit-auto-review-owner--can-close))))

;;; magit-auto-review-owner-tests.el ends here
