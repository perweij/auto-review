;;; magit-auto-review-setup.el --- Review setup selectors -*- lexical-binding: t; -*-

;; SPDX-License-Identifier: GPL-3.0-or-later

;;; Commentary:
;; CLI metadata supplies closed choices.  Opening this menu never runs inference.

;;; Code:
(require 'magit-auto-review)
(declare-function magit-auto-review--launch-review "magit-auto-review-actions" (settings &optional trusted-snapshot catalogs))
(declare-function magit-auto-review--launch-automatic-review "magit-auto-review-actions" (settings catalogs))

(defcustom magit-auto-review-reviewers '("claude" "codex")
  "Default enabled reviewers for new reviews."
  :type '(set (const "claude") (const "codex")) :group 'magit-auto-review)
(defcustom magit-auto-review-processing-provider "codex"
  "Default synthesis and discussion provider when both reviewers are enabled."
  :type '(choice (const "claude") (const "codex")) :group 'magit-auto-review)
(defcustom magit-auto-review-claude-effort "high"
  "Default Claude effort for new reviews; validated against model metadata."
  :type 'string :group 'magit-auto-review)
(defcustom magit-auto-review-codex-effort "high"
  "Default Codex effort for new reviews; validated against model metadata."
  :type 'string :group 'magit-auto-review)

(defvar-local magit-auto-review--setup-settings nil)
(defvar-local magit-auto-review--setup-catalogs nil)
(defvar-local magit-auto-review--setup-status nil)
(defvar-local magit-auto-review--setup-processes nil)
(defvar-local magit-auto-review--setup-token nil)
(defvar-local magit-auto-review--setup-field nil)
(defvar-local magit-auto-review--setup-page 0)
(defvar magit-auto-review--setup-owner nil)
(defvar magit-auto-review--catalog-cache (make-hash-table :test #'equal))

(defun magit-auto-review--setup-enabled (provider)
  "Whether PROVIDER is enabled in the provisional setup."
  (member provider (append (alist-get 'reviewers magit-auto-review--setup-settings) nil)))

(defun magit-auto-review--setup-stop ()
  "Cancel metadata children and invalidate their callbacks."
  (setq magit-auto-review--setup-token nil)
  (dolist (entry magit-auto-review--setup-processes)
    (when (process-live-p (cdr entry))
      ;; The backend handles TERM by killing and reaping its CLI process group.
      (signal-process (cdr entry) 'SIGTERM)))
  (setq magit-auto-review--setup-processes nil))

(defun magit-auto-review--setup-exited ()
  "Clean up metadata when all Transient menus have closed."
  (when (buffer-live-p magit-auto-review--setup-owner)
    (with-current-buffer magit-auto-review--setup-owner
      (magit-auto-review--setup-stop)))
  (setq magit-auto-review--setup-owner nil))
(add-hook 'transient-post-exit-hook #'magit-auto-review--setup-exited)

(defun magit-auto-review--setup-redraw ()
  "Refresh the active setup menu after metadata arrives."
  (when (and transient--prefix
             (eq (current-buffer) magit-auto-review--setup-owner)
             (memq (oref transient--prefix command)
                   '(magit-auto-review-setup-menu magit-auto-review--choice-menu)))
    (transient--refresh-transient)))

(defun magit-auto-review-refresh-choices ()
  "Refresh metadata for enabled providers without running inference."
  (interactive)
  (magit-auto-review--setup-stop)
  (setq magit-auto-review--setup-token (list 'discovery)
        magit-auto-review--setup-status nil)
  (let ((token magit-auto-review--setup-token))
    (dolist (provider '("claude" "codex"))
      (when (magit-auto-review--setup-enabled provider)
        (setf (alist-get (intern provider) magit-auto-review--setup-status) "Loading choices…")
        (let ((cached (gethash (list magit-auto-review-executable provider) magit-auto-review--catalog-cache)))
          (when cached (setf (alist-get (intern provider) magit-auto-review--setup-catalogs) cached)))
        (push
         (cons provider
               (magit-auto-review--async
                `((command . "models") (provider . ,provider)) #'ignore
                (lambda (result)
                  (when (eq token magit-auto-review--setup-token)
                    (setf (alist-get (intern provider) magit-auto-review--setup-catalogs) result
                          (alist-get (intern provider) magit-auto-review--setup-status) 'ready)
                    (puthash (list magit-auto-review-executable provider) result magit-auto-review--catalog-cache)
                    (magit-auto-review--setup-redraw)))
                (lambda (error)
                  (when (eq token magit-auto-review--setup-token)
                    (setf (alist-get (intern provider) magit-auto-review--setup-status)
                          (concat (magit-auto-review--error-message error) " [g: retry]"))
                    (magit-auto-review--setup-redraw)))))
         magit-auto-review--setup-processes)))))

(defun magit-auto-review--default-settings ()
  "Return customized defaults for initializing a new workflow."
  `((reviewers . ,(vconcat magit-auto-review-reviewers))
    (processing_provider . ,magit-auto-review-processing-provider)
    (claude_model . ,magit-auto-review-claude-model)
    (codex_model . ,magit-auto-review-codex-model)
    (claude_effort . ,magit-auto-review-claude-effort)
    (codex_effort . ,magit-auto-review-codex-effort)
    (synthesis_tests . ,magit-auto-review-synthesis-tests)
    (suggested_changes . ,(if magit-auto-review-suggested-changes t :false))))

(defun magit-auto-review-run ()
  "Configure this review, then explicitly run or resume it."
  (interactive)
  (when (process-live-p magit-auto-review--process)
    (user-error "A review operation is already running; use L or K"))
  (magit-auto-review--setup-exited)
  (let ((resolved
         (magit-auto-review--call
          (append
           (if magit-auto-review--session
               `((command . "settings") (session . ,magit-auto-review--session))
             `((command . "settings") (pr . ,magit-auto-review--target) (cwd . ,default-directory)
               (repository . ,magit-auto-review--repository)))
           `((defaults . ,(magit-auto-review--default-settings)))))))
    (setq magit-auto-review--setup-settings (copy-tree (alist-get 'settings resolved))
          magit-auto-review--setup-catalogs nil
          magit-auto-review--setup-owner (current-buffer))
    (when (alist-get 'session resolved)
      (setq magit-auto-review--session (alist-get 'session resolved)))
    (setf (alist-get 'reviewers magit-auto-review--setup-settings)
          (vconcat (alist-get 'reviewers magit-auto-review--setup-settings))))
  (add-hook 'kill-buffer-hook #'magit-auto-review--setup-stop nil t)
  (magit-auto-review-refresh-choices)
  (transient-setup 'magit-auto-review-setup-menu))

(defun magit-auto-review--setup-model (provider)
  "Return metadata for PROVIDER's selected model."
  (let ((name (alist-get (intern (concat provider "_model")) magit-auto-review--setup-settings)))
    (cl-find name (alist-get 'models (alist-get (intern provider) magit-auto-review--setup-catalogs))
             :key (lambda (m) (alist-get 'value m)) :test #'equal)))

(defun magit-auto-review--setup-ready-p ()
  "Whether enabled selections have been validated by fresh metadata."
  (and (alist-get 'reviewers magit-auto-review--setup-settings)
       (cl-every
        (lambda (provider)
          (and (eq (alist-get (intern provider) magit-auto-review--setup-status) 'ready)
               (member (alist-get (intern (concat provider "_effort")) magit-auto-review--setup-settings)
                       (alist-get 'efforts (magit-auto-review--setup-model provider)))))
        (append (alist-get 'reviewers magit-auto-review--setup-settings) nil))))

(defun magit-auto-review--setup-label (field label &optional provider)
  "Describe FIELD with LABEL and optional PROVIDER validation."
  (let* ((value (alist-get field magit-auto-review--setup-settings))
         (model (and provider (magit-auto-review--setup-model provider)))
         (supported (or (not provider)
                        (and model (or (string-suffix-p "_model" (symbol-name field))
                                       (member value (alist-get 'efforts model)))))))
    (format "%s: %s%s" label (or value "Choose…")
            (cond ((and provider (not (magit-auto-review--setup-enabled provider))) " (inactive)")
                  ((not supported) " — choose a listed value") (t "")))))

(defun magit-auto-review--toggle-provider (provider)
  "Toggle PROVIDER without discarding its model or effort."
  (let ((enabled (append (alist-get 'reviewers magit-auto-review--setup-settings) nil)))
    (if (member provider enabled)
        (progn
          (when (= (length enabled) 1) (user-error "At least one reviewer must stay enabled"))
          (setq enabled (delete provider enabled)))
      (push provider enabled))
    (setf (alist-get 'reviewers magit-auto-review--setup-settings)
          (vconcat (cl-remove-if-not (lambda (p) (member p enabled)) '("claude" "codex")))))
  (magit-auto-review-refresh-choices))

(defun magit-auto-review--choice-open (field)
  "Show a closed selector for FIELD."
  (setq magit-auto-review--setup-field field magit-auto-review--setup-page 0)
  (transient-setup 'magit-auto-review--choice-menu))

(defun magit-auto-review--choices ()
  "Return only advertised choices for the current field."
  (let* ((name (symbol-name magit-auto-review--setup-field))
         (provider (car (split-string name "_"))))
    (cond
     ((eq magit-auto-review--setup-field 'processing_provider)
      '(("claude" . "Claude") ("codex" . "Codex")))
     ((eq magit-auto-review--setup-field 'synthesis_tests)
      '(("focused" . "Focused checks") ("full" . "Permit one full-suite attempt")))
     ((string-suffix-p "_model" name)
      (mapcar (lambda (m) (cons (alist-get 'value m) (format "%s (%s)" (alist-get 'label m) (alist-get 'value m))))
              (alist-get 'models (alist-get (intern provider) magit-auto-review--setup-catalogs))))
     (t (mapcar (lambda (e) (cons e e)) (alist-get 'efforts (magit-auto-review--setup-model provider)))))))

(defun magit-auto-review--choice-children (_children)
  "Build paginated selection-only suffixes from current metadata."
  (let* ((choices (magit-auto-review--choices))
         (offset (* 20 magit-auto-review--setup-page))
         (page (seq-subseq choices (min offset (length choices)) (min (+ offset 20) (length choices))))
         (keys "1234567890abcdefghij")
         (selected (alist-get magit-auto-review--setup-field magit-auto-review--setup-settings))
         (field magit-auto-review--setup-field)
         (index -1))
    (transient-parse-suffixes
     'magit-auto-review--choice-menu
     (append
      (unless choices '("Choices unavailable. Return and use g to refresh."))
      (mapcar (lambda (choice)
                (list (char-to-string (aref keys (cl-incf index)))
                      (concat (if (equal selected (car choice)) "● " "  ") (cdr choice))
                      (lambda () (interactive)
                        (setf (alist-get field magit-auto-review--setup-settings) (car choice))
                        (transient-setup 'magit-auto-review-setup-menu))
                      :transient t)) page)
      (when (> offset 0)
        '(("p" "Previous page" (lambda () (interactive) (cl-decf magit-auto-review--setup-page)) :transient t)))
      (when (< (+ offset 20) (length choices))
        '(("n" "Next page" (lambda () (interactive) (cl-incf magit-auto-review--setup-page)) :transient t)))
      '(("q" "Back" (lambda () (interactive) (transient-setup 'magit-auto-review-setup-menu)) :transient t))))))

(transient-define-prefix magit-auto-review--choice-menu ()
  "Select one listed value; no free-text input."
  :refresh-suffixes t
  [:class transient-column
   :description (lambda ()
                  (pcase magit-auto-review--setup-field
                    ('processing_provider "Synthesis and discussion provider")
                    ('synthesis_tests "Verification policy")
                    (field (capitalize (replace-regexp-in-string "_" " " (symbol-name field))))))
   :setup-children magit-auto-review--choice-children])

(defun magit-auto-review-setup-submit ()
  "Run with the displayed configuration, preserving it for retries."
  (interactive)
  (unless (magit-auto-review--setup-ready-p)
    (user-error "Refresh choices and select a supported model and effort for each enabled reviewer"))
  (let* ((settings (copy-tree magit-auto-review--setup-settings))
         (reviewers (append (alist-get 'reviewers settings) nil)))
    (when (= (length reviewers) 1)
      (setf (alist-get 'processing_provider settings) (car reviewers)))
    (magit-auto-review--setup-stop)
    (magit-auto-review--launch-review settings nil (magit-auto-review--catalogs-for-request))))

(defun magit-auto-review-setup-submit-automatic ()
  "Explicitly analyse and publish a COMMENT review without staging prompts."
  (interactive)
  (unless (magit-auto-review--setup-ready-p)
    (user-error "Choose supported reviewer models and efforts first"))
  (let ((settings (copy-tree magit-auto-review--setup-settings))
        (catalogs (magit-auto-review--catalogs-for-request)))
    (magit-auto-review--setup-stop)
    (magit-auto-review--launch-automatic-review settings catalogs)))

(defun magit-auto-review--catalogs-for-request ()
  "Encode metadata arrays explicitly for the JSON transport."
  (mapcar
   (lambda (entry)
     (cons (car entry)
           `((provider . ,(alist-get 'provider (cdr entry)))
             (models . ,(vconcat
                         (mapcar (lambda (model)
                                   `((value . ,(alist-get 'value model))
                                     (efforts . ,(vconcat (alist-get 'efforts model)))))
                                 (alist-get 'models (cdr entry))))))))
   magit-auto-review--setup-catalogs))

(defun magit-auto-review--setup-info (_children)
  "Describe metadata loading and failure without hiding the Run action."
  (transient-parse-suffixes
   'magit-auto-review-setup-menu
   (mapcar (lambda (p) (format "%s: %s" (capitalize p)
                               (if (eq (alist-get (intern p) magit-auto-review--setup-status) 'ready)
                                   "Choices refreshed"
                                 (or (alist-get (intern p) magit-auto-review--setup-status) "Loading…"))))
           (append (alist-get 'reviewers magit-auto-review--setup-settings) nil))))

(defun magit-auto-review--setup-preset (preset)
  "Apply PRESET to the provisional settings without running a review."
  (setq magit-auto-review--setup-settings
        (alist-get 'settings
                   (magit-auto-review--call
                    `((command . "settings") (,preset . t)
                      (defaults . ,magit-auto-review--setup-settings)))))
  (magit-auto-review-refresh-choices))

(defun magit-auto-review-setup-economical ()
  "Select Haiku + Terra/high for both reviewers and Terra processing."
  (interactive)
  (magit-auto-review--setup-preset 'economical))

(defun magit-auto-review-setup-expensive ()
  "Select Fable + Astra/high for both reviewers and Astra processing."
  (interactive)
  (magit-auto-review--setup-preset 'expensive))

(transient-define-prefix magit-auto-review-setup-menu ()
  "Review settings.  Change any value, then Run/resume."
  :refresh-suffixes t
  [["Claude"
    ("c" (lambda () (if (magit-auto-review--setup-enabled "claude") "Reviewer: on" "Reviewer: off"))
     (lambda () (interactive) (magit-auto-review--toggle-provider "claude")) :transient t)
    ("m" (lambda () (magit-auto-review--setup-label 'claude_model "Model" "claude"))
     (lambda () (interactive) (magit-auto-review--choice-open 'claude_model)) :transient t
     :inapt-if (lambda () (not (magit-auto-review--setup-enabled "claude"))))
    ("e" (lambda () (magit-auto-review--setup-label 'claude_effort "Effort" "claude"))
     (lambda () (interactive) (magit-auto-review--choice-open 'claude_effort)) :transient t
     :inapt-if (lambda () (not (magit-auto-review--setup-enabled "claude"))))]
   ["Codex"
    ("x" (lambda () (if (magit-auto-review--setup-enabled "codex") "Reviewer: on" "Reviewer: off"))
     (lambda () (interactive) (magit-auto-review--toggle-provider "codex")) :transient t)
    ("M" (lambda () (magit-auto-review--setup-label 'codex_model "Model" "codex"))
     (lambda () (interactive) (magit-auto-review--choice-open 'codex_model)) :transient t
     :inapt-if (lambda () (not (magit-auto-review--setup-enabled "codex"))))
    ("E" (lambda () (magit-auto-review--setup-label 'codex_effort "Effort" "codex"))
     (lambda () (interactive) (magit-auto-review--choice-open 'codex_effort)) :transient t
     :inapt-if (lambda () (not (magit-auto-review--setup-enabled "codex"))))]]
  ["Processing"
   ("b" "Economical: Haiku + Terra/high" magit-auto-review-setup-economical :transient t)
   ("B" "Expensive: Fable + Astra/high" magit-auto-review-setup-expensive :transient t)
   ("p" (lambda ()
          (let ((reviewers (append (alist-get 'reviewers magit-auto-review--setup-settings) nil)))
            (format "Synthesis and discussion: %s%s"
                    (capitalize (if (= (length reviewers) 1) (car reviewers)
                                  (alist-get 'processing_provider magit-auto-review--setup-settings)))
                    (if (= (length reviewers) 1) " (only reviewer; no synthesis)" ""))))
    (lambda () (interactive) (magit-auto-review--choice-open 'processing_provider)) :transient t
    :inapt-if (lambda () (= (length (alist-get 'reviewers magit-auto-review--setup-settings)) 1)))
   ("t" (lambda () (magit-auto-review--setup-label 'synthesis_tests "Testing"))
    (lambda () (interactive) (magit-auto-review--choice-open 'synthesis_tests)) :transient t)
   ("s" (lambda () (format "Suggested changes: %s"
                           (if (magit-auto-review--true-p (alist-get 'suggested_changes magit-auto-review--setup-settings)) "on" "off")))
    (lambda () (interactive)
      (setf (alist-get 'suggested_changes magit-auto-review--setup-settings)
            (if (magit-auto-review--true-p (alist-get 'suggested_changes magit-auto-review--setup-settings)) :false t))) :transient t)]
  [:class transient-column :setup-children magit-auto-review--setup-info]
  [:description (lambda () (format "PR #%s · %s"
                                  (or (alist-get 'pr magit-auto-review--review) magit-auto-review--target)
                                  (or (alist-get 'repository magit-auto-review--review)
                                      magit-auto-review--repository "current repository")))
   ("r" "Run/resume" magit-auto-review-setup-submit :inapt-if (lambda () (not (magit-auto-review--setup-ready-p))))
   ("P" "Run and publish automatically (COMMENT)" magit-auto-review-setup-submit-automatic :inapt-if (lambda () (not (magit-auto-review--setup-ready-p))))
   ("g" "Refresh choices / retry" magit-auto-review-refresh-choices :transient t)
   ("q" "Cancel" transient-quit-one)])

(provide 'magit-auto-review-setup)
;;; magit-auto-review-setup.el ends here
