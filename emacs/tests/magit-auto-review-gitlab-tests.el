;;; magit-auto-review-gitlab-tests.el --- GitLab UI contracts -*- lexical-binding: t; -*-
(require 'ert)
(require 'magit-auto-review)
(require 'forge-gitlab)

(defun auto-review-gitlab-data ()
  (let ((review (auto-review-test-data)))
    (setf (alist-get 'repository review) "gitlab.com/group/sub/repo"
          (alist-get 'url review) "https://gitlab.com/group/sub/repo/-/merge_requests/7"
          (alist-get 'forge_project review) '((forge . "gitlab") (host . "gitlab.com") (path . "group/sub/repo"))
          (alist-get 'capabilities review)
          '((features . ("drafts" "history" "conversations" "finding-elaboration" "checkout" "prepared-publication-preview"))
            (placements . ("inline" "review"))))
    review))

(defmacro auto-review-gitlab-buffer (&rest body)
  `(auto-review-test-buffer
    (magit-auto-review--accept (auto-review-gitlab-data))
    ,@body))

(ert-deftest gitlab/selected-forge-mr-uses-structured-repository-identity ()
  (with-temp-buffer
    (setq-local forge-buffer-topic (forge-pullreq :number 7))
    (let ((repo (forge-gitlab-repository :githost "gitlab.com" :owner "group/sub" :name "repo")) opened)
      (cl-letf (((symbol-function 'magit-toplevel) (lambda () "/checkout/"))
                ((symbol-function 'forge-get-repository) (lambda (&rest _) repo))
                ((symbol-function 'magit-auto-review--open-pr) (lambda (&rest args) (setq opened args))))
        (magit-auto-review))
      (should (equal opened '("/checkout/" 7 "gitlab.com/group/sub/repo" "gitlab"))))))

(ert-deftest gitlab/prefix-entry-accepts-url-and-selects-reviewer ()
  (with-temp-buffer
    (let (opened request)
      (cl-letf (((symbol-function 'magit-toplevel) (lambda () "/checkout/"))
                ((symbol-function 'read-string) (lambda (&rest _) "https://gitlab.com/group/sub/repo/merge_requests/7/diffs?x=1#abc"))
                ((symbol-function 'magit-auto-review--call)
                 (lambda (r)
                   (if (equal (alist-get 'command r) "toolkit")
                       '((toolkit . "reviewer"))
                     (setq request r)
                     `((pr . 7) (repository . "gitlab.com/group/sub/repo")
                       (forge_project . ,(alist-get 'forge_project (auto-review-gitlab-data)))))))
                ((symbol-function 'magit-auto-review--open-review-pr) (lambda (&rest args) (setq opened args))))
        (let ((current-prefix-arg '(4))) (call-interactively #'magit-auto-review)))
      (should (equal (alist-get 'command request) "review-target"))
      (should (string-match-p "diffs?" (alist-get 'pr request)))
      (should (equal (seq-take opened 3) '("/checkout/" 7 "gitlab.com/group/sub/repo"))))))

(ert-deftest gitlab/saved-review-prompt-accepts-saved-choice ()
  (let ((context `((repository . "gitlab.com/group/sub/repo")
                   (forge_project . ,(alist-get 'forge_project (auto-review-gitlab-data))))) opened prompt)
    (cl-letf (((symbol-function 'magit-auto-review--call)
               (lambda (r) (should (equal (alist-get 'forge r) "gitlab"))
                 (if (equal (alist-get 'command r) "owner-discover") '((sessions . nil))
                   '((sessions . (((pr . 7) (session . "/saved") (created_at . "today"))))))))
              ((symbol-function 'completing-read) (lambda (p choices &rest _) (setq prompt p) (caar choices)))
              ((symbol-function 'magit-auto-review--open-review-pr) (lambda (&rest args) (setq opened args))))
      (magit-auto-review--prompt-mr "/checkout/" context))
    (should (string-match-p "MR number, URL, or saved review" prompt))
    (should (equal (alist-get 'session (nth 4 opened)) "/saved"))))

(ert-deftest gitlab/ambiguous-projects-prompt-once-before-target-resolution ()
  (let (requests prompts)
    (cl-letf (((symbol-function 'magit-auto-review--call)
               (lambda (r) (push r requests)
                 (if (alist-get 'repository r) '((repository . "gitlab.com/a/b") (pr . 7))
                   '((projects . (((repository . "gitlab.com/a/b")) ((repository . "github.com/a/b"))))))))
              ((symbol-function 'completing-read) (lambda (p &rest _) (push p prompts) "gitlab.com/a/b")))
      (should (= 7 (alist-get 'pr (magit-auto-review--resolve-target "/checkout/" 7)))))
    (should (= 1 (length prompts)))
    (should (= 2 (length requests)))))

(ert-deftest gitlab/unsupported-direct-actions-never-send-requests ()
  (auto-review-gitlab-buffer
   (auto-review-test-goto "p1")
   (cl-letf (((symbol-function 'magit-auto-review--call) (lambda (&rest _) (ert-fail "Unexpected request")))
             ((symbol-function 'magit-auto-review--start) (lambda (&rest _) (ert-fail "Unexpected async request"))))
     (dolist (command '(magit-auto-review-send-to-owner magit-auto-review-select-toolkit
                        magit-auto-review-pull-requests magit-auto-review-set-verdict
                        magit-auto-review-suggestion-menu magit-auto-review-suggestion-edit
                        magit-auto-review-suggestion-remove magit-auto-review-suggestion-verify
                        magit-auto-review-adapt magit-auto-review-conversation-reply
                        magit-auto-review-check-conversations magit-auto-review-resolve-thread
                        magit-auto-review-reopen-thread))
       (should-error (funcall command) :type 'user-error))
     (should-error (magit-auto-review--placement-browser "p1" 'thread) :type 'user-error))))

(ert-deftest gitlab/request-and-encoding-retain-project-before-session ()
  (auto-review-gitlab-buffer
   (setq magit-auto-review--session nil)
   (should (equal (alist-get 'forge (magit-auto-review--request "settings")) "gitlab"))
   (let ((encoded (magit-auto-review--json (magit-auto-review--encode '((command . "models") (provider . "codex"))))))
     (should (equal (alist-get 'repository encoded) "gitlab.com/group/sub/repo"))
     (should (equal (alist-get 'forge encoded) "gitlab")))
   (cl-letf (((symbol-function 'magit-auto-review--start)
              (lambda (r _callback)
                (should (equal (alist-get 'forge r) "gitlab"))
                (should (equal (alist-get 'repository r) "gitlab.com/group/sub/repo")))))
     (magit-auto-review--launch-review nil)
     (magit-auto-review--launch-automatic-review nil nil))))

(ert-deftest gitlab/fresh-defaults-ignore-suggestions-and-saved-setting-can-only-disable ()
  (auto-review-gitlab-buffer
   (let ((magit-auto-review-suggested-changes t))
     (should (eq :false (alist-get 'suggested_changes (magit-auto-review--default-settings)))))
   (setq magit-auto-review--setup-settings '((suggested_changes . t) (processing . ((model . "saved")))))
   (should (magit-auto-review--incompatible-suggestions-p))
   (magit-auto-review-setup-suggestions)
   (should (equal (alist-get 'model (alist-get 'processing magit-auto-review--setup-settings)) "saved"))
   (should-not (magit-auto-review--incompatible-suggestions-p))
   (should-error (magit-auto-review-setup-suggestions) :type 'user-error)))

(ert-deftest gitlab/presets-retain-forge-identity-and-saved-models ()
  (auto-review-gitlab-buffer
   (setq magit-auto-review--setup-settings '((suggested_changes . :false) (processing . ((model . "saved"))))
         magit-auto-review--setup-saved t)
   (cl-letf (((symbol-function 'magit-auto-review-refresh-choices) #'ignore)
             ((symbol-function 'magit-auto-review--call)
              (lambda (r)
                (should (equal (alist-get 'forge r) "gitlab"))
                (should (equal (alist-get 'model (alist-get 'processing (alist-get 'defaults r))) "saved"))
                `((settings . ,magit-auto-review--setup-settings)))))
     (magit-auto-review-setup-economical))))

(ert-deftest gitlab/conversations-are-read-only-and-inherit-capabilities ()
  (save-window-excursion
    (auto-review-gitlab-buffer
     (let (child)
       (unwind-protect
           (progn
             (magit-auto-review-conversations)
             (setq child (current-buffer))
             (should (equal magit-auto-review--forge "gitlab"))
             (should (string-match-p "Conversations for MR #7" (buffer-string)))
             (should (string-match-p "read-only" (buffer-string)))
             (should-not (string-match-p "v to choose\\|x Resolve" (buffer-string)))
             (should-not (magit-auto-review--can-change-thread-p t)))
         (when (buffer-live-p child) (kill-buffer child)))))))

(ert-deftest gitlab/placement-prompt-omits-thread-and-editor-keeps-original-prose ()
  (auto-review-gitlab-buffer
   (auto-review-test-goto "p1")
   (let (prompt choices)
     (cl-letf (((symbol-function 'read-char-choice)
                (lambda (p c &rest _) (setq prompt p choices c) ?o)))
       (magit-auto-review-placement))
     (should-not (memq ?t choices))
     (should-not (string-match-p "thread" prompt)))
   (setf (alist-get 'body (magit-auto-review--comment "p1")) "/close\nOriginal wording")
   (magit-auto-review--render)
   (auto-review-test-goto "p1")
   (magit-auto-review-edit)
   (should (equal "/close\nOriginal wording"
                  (buffer-substring-no-properties (plist-get magit-auto-review--editor :start)
                                                  (plist-get magit-auto-review--editor :end))))))

(ert-deftest gitlab/preview-displays-frozen-prose-and-position-before-declining ()
  (save-window-excursion
    (auto-review-gitlab-buffer
     (let* ((review magit-auto-review--review)
            (op '((kind . "inline") (body . "Frozen prose")
                  (destination . ((url . "https://gitlab.com/a/b/-/merge_requests/7")))
                  (position . ((new_path . "file") (new_line . 3)))))
            (result `((ready . t) (review . ,review) (confirmation . "token")
                      (counts . ((inline . 1) (replies . 0) (overall . 0)))
                      (preview . ((operations . (,op))))))
            requests confirmation)
       (cl-letf (((symbol-function 'magit-auto-review--start)
                  (lambda (r callback) (push r requests) (funcall callback result)))
                 ((symbol-function 'yes-or-no-p)
                  (lambda (p)
                    (setq confirmation p)
                    (with-current-buffer "*Auto-review publication preview*"
                      (should buffer-read-only)
                      (should (string-match-p "Frozen prose" (buffer-string)))
                      (should (string-match-p "new_line.*3" (buffer-string))))
                    nil)))
         (magit-auto-review-publish))
       (should (= 1 (length requests)))
       (should-not (string-match-p "overall PR verdict" confirmation))
       (should (equal "Body 1" (alist-get 'body (magit-auto-review--comment "p1"))))))))

(ert-deftest gitlab/menus-hide-unsupported-actions-with-mutation-checks-intact ()
  (auto-review-gitlab-buffer
   (auto-review-test-goto "p1")
   (let* ((transient--prefix (transient--init-prefix 'magit-auto-review-dispatch))
          (suffixes (transient--flatten-suffixes (transient--init-suffixes 'magit-auto-review-dispatch)))
          (commands (mapcar (lambda (s) (oref s command))
                            (cl-remove-if (lambda (s) (oref s inactive)) suffixes))))
     (dolist (command '(magit-auto-review-send-to-owner magit-auto-review-select-toolkit
                        magit-auto-review-pull-requests magit-auto-review-set-verdict
                        magit-auto-review-suggestion-menu magit-auto-review-adapt))
       (should-not (memq command commands)))
     (dolist (command '(magit-auto-review-edit magit-auto-review-stage magit-auto-review-conversations
                        magit-auto-review-open-checkout magit-auto-review-execution-report-menu))
       (should (memq command commands))))
   (setq magit-auto-review--history-view t)
   (should-not (magit-auto-review--mutable-p))))

(ert-deftest gitlab/history-buffer-and-pinned-generation-retain-identity ()
  (save-window-excursion
    (auto-review-gitlab-buffer
     (let ((review (auto-review-gitlab-data)) history child)
       (setf (alist-get 'historical review) t)
       (unwind-protect
           (cl-letf (((symbol-function 'magit-auto-review--call)
                      (lambda (r)
                        (should (equal (alist-get 'forge r) "gitlab"))
                        (pcase (alist-get 'command r)
                          ("history" '((generations . (((generation . "old") (human_edits . 0) (saved_versions . 0))))))
                          ("read" review)))))
             (magit-auto-review-history)
             (setq history (current-buffer))
             (should (equal magit-auto-review--forge "gitlab"))
             (magit-auto-review--walk-sections
              (lambda (s) (when (eq (oref s type) 'review-generation) (goto-char (oref s start)))))
             (magit-auto-review-open-generation)
             (setq child (current-buffer))
             (should (equal magit-auto-review--forge "gitlab"))
             (should (equal magit-auto-review--generation "old"))
             (should (magit-auto-review--supports-p "conversations"))
             (should-not (magit-auto-review--mutable-p)))
         (dolist (buffer (list child history)) (when (buffer-live-p buffer) (kill-buffer buffer))))))))

(ert-deftest gitlab/preset-settings-arrays-can-be-encoded-for-real-transport ()
  (auto-review-gitlab-buffer
   (setq magit-auto-review--setup-settings
         '((reviewer_slots . (((id . "reviewer-1") (provider . "codex") (model . "saved") (effort . "high"))))
           (reviewers . ("codex")) (_compatibility . ((reviewers . ("codex"))))))
   (magit-auto-review--normalize-setup-arrays magit-auto-review--setup-settings)
   (let ((encoded (magit-auto-review--json
                   (magit-auto-review--encode (append '((command . "run")) magit-auto-review--setup-settings)))))
     (should (equal '("codex") (alist-get 'reviewers encoded)))
     (should (equal "saved" (alist-get 'model (car (alist-get 'reviewer_slots encoded))))))))

(ert-deftest gitlab/selected-self-hosted-forge-fails-before-request ()
  (with-temp-buffer
    (setq-local forge-buffer-topic (forge-pullreq :number 7))
    (cl-letf (((symbol-function 'magit-toplevel) (lambda () "/checkout/"))
              ((symbol-function 'forge-get-repository)
               (lambda (&rest _) (forge-gitlab-repository :githost "gitlab" :owner "group" :name "repo")))
              ((symbol-function 'magit-auto-review--call) (lambda (&rest _) (ert-fail "Unexpected backend request"))))
      (should-error (magit-auto-review) :type 'user-error))))

(ert-deftest gitlab/owner-handoff-uses-separate-capability-and-cached-eligibility ()
  (auto-review-gitlab-buffer
   (setq magit-auto-review--capabilities '((features . ("owner-handoff" "toolkits"))))
   (setf (alist-get 'owner_fixing magit-auto-review--review) '((available . t)))
   (should (magit-auto-review--owner-handoff-p))
   (should-not (magit-auto-review--supports-p "owner"))
   (should-not (magit-auto-review--supports-p "replies"))
   (setf (alist-get 'owner_fixing magit-auto-review--review) '((available . :false) (reason . "Fork MR")))
   (should-not (magit-auto-review--owner-handoff-p))
   (cl-letf (((symbol-function 'magit-auto-review--call) (lambda (&rest _) (ert-fail "Unavailable handoff sent a request"))))
     (should-error (magit-auto-review-send-to-owner) :type 'user-error))))
