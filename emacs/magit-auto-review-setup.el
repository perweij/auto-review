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
(defcustom magit-auto-review-processing-provider nil
  "Default synthesis and discussion provider for new reviews.
When nil, a sole reviewer is used, or Codex for a pair."
  :type '(choice (const :tag "Automatic" nil) (const "claude") (const "codex")) :group 'magit-auto-review)
(defcustom magit-auto-review-claude-effort "high"
  "Default Claude effort for new reviews; validated against model metadata."
  :type 'string :group 'magit-auto-review)
(defcustom magit-auto-review-codex-effort "high"
  "Default Codex effort for new reviews; validated against model metadata."
  :type 'string :group 'magit-auto-review)

(defvar-local magit-auto-review--choice-adapter nil
  "Optional workflow callbacks for shared metadata and selection controls.")

(defvar-local magit-auto-review--setup-settings nil)
(defvar-local magit-auto-review--setup-catalogs nil)
(defvar-local magit-auto-review--setup-status nil)
(defvar-local magit-auto-review--setup-processes nil)
(defvar-local magit-auto-review--setup-token nil)
(defvar-local magit-auto-review--setup-field nil)
(defvar-local magit-auto-review--setup-page 0)
(defvar-local magit-auto-review--setup-provisional-processing nil)
(defvar-local magit-auto-review--setup-saved nil)
(defvar-local magit-auto-review--setup-roles-edited nil)
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
      (magit-auto-review--setup-stop)
      (when-let ((cleanup (plist-get magit-auto-review--choice-adapter :exit)))
        (funcall cleanup))))
  (setq magit-auto-review--setup-owner nil))
(add-hook 'transient-post-exit-hook #'magit-auto-review--setup-exited)

(defun magit-auto-review--setup-redraw ()
  "Refresh the active setup menu after metadata arrives."
  (when (and transient--prefix
             (eq (current-buffer) magit-auto-review--setup-owner)
             (memq (oref transient--prefix command)
                   (list 'magit-auto-review-setup-menu 'magit-auto-review--choice-menu
                         (plist-get magit-auto-review--choice-adapter :menu))))
    (transient--refresh-transient)))

(defun magit-auto-review-refresh-choices ()
  "Refresh metadata for enabled providers without running inference."
  (interactive)
  (magit-auto-review--setup-stop)
  (setq magit-auto-review--setup-token (list 'discovery)
        magit-auto-review--setup-status nil)
  (let ((token magit-auto-review--setup-token))
    (dolist (provider '("claude" "codex"))
      (when (member provider (magit-auto-review--setup-providers))
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
    (claude_model . ,magit-auto-review-claude-model)
    (codex_model . ,magit-auto-review-codex-model)
    (claude_effort . ,magit-auto-review-claude-effort)
    (codex_effort . ,magit-auto-review-codex-effort)
    ,@(when magit-auto-review-processing-provider
        `((processing_provider . ,magit-auto-review-processing-provider)))
    (synthesis_tests . ,magit-auto-review-synthesis-tests)
    (suggested_changes . ,(if magit-auto-review-suggested-changes t :false))))

(defun magit-auto-review-run ()
  "Configure this review, then explicitly run or resume it."
  (interactive)
  (when (process-live-p magit-auto-review--process)
    (user-error "A review operation is already running; use L or K"))
  (magit-auto-review--setup-exited)
  (setq magit-auto-review--choice-adapter nil)
  (let ((resolved
         (magit-auto-review--call
          (append
           (if magit-auto-review--session
               `((command . "settings") (session . ,magit-auto-review--session))
             `((command . "settings") (pr . ,magit-auto-review--target) (cwd . ,default-directory)
               (repository . ,magit-auto-review--repository)))
           `((defaults . ,(magit-auto-review--default-settings)))))))
    (setq magit-auto-review--setup-settings (copy-tree (alist-get 'settings resolved) t)
          magit-auto-review--setup-catalogs nil
          magit-auto-review--setup-saved (and (alist-get 'session resolved) t)
          magit-auto-review--setup-roles-edited nil
          magit-auto-review--setup-provisional-processing
          (and (not (alist-get 'session resolved))
               (not (assq 'processing_provider (magit-auto-review--default-settings))))
          magit-auto-review--setup-owner (current-buffer))
    (when (alist-get 'session resolved)
      (setq magit-auto-review--session (alist-get 'session resolved)))
    ;; The protocol decoder returns lists; JSON arrays must be vectors when
    ;; these settings are sent back, including the compatibility snapshot.
    (setf (alist-get 'reviewer_slots magit-auto-review--setup-settings)
          (vconcat (alist-get 'reviewer_slots magit-auto-review--setup-settings)))
    (dolist (settings (list magit-auto-review--setup-settings
                            (alist-get '_compatibility magit-auto-review--setup-settings)))
      (when (assq 'reviewers settings)
        (setf (alist-get 'reviewers settings)
              (vconcat (alist-get 'reviewers settings))))))
  (add-hook 'kill-buffer-hook #'magit-auto-review--setup-stop nil t)
  (magit-auto-review-refresh-choices)
  (transient-setup (or (plist-get magit-auto-review--choice-adapter :menu) 'magit-auto-review-setup-menu)))

(defun magit-auto-review--setup-providers ()
  "Return providers used by reviewer slots and processing."
  (if-let ((callback (plist-get magit-auto-review--choice-adapter :providers)))
      (funcall callback)
    (delete-dups (append (mapcar (lambda (slot) (alist-get 'provider slot))
                                 (append (alist-get 'reviewer_slots magit-auto-review--setup-settings) nil))
                         (list (alist-get 'provider (alist-get 'processing magit-auto-review--setup-settings)))))))

(defun magit-auto-review--choice-target (field)
  "Return the role object and property named by FIELD."
  (if-let ((callback (plist-get magit-auto-review--choice-adapter :target)))
      (funcall callback field)
    (let ((name (symbol-name field)))
      (cond ((string-match "\\`reviewer_\\([12]\\)_\\(provider\\|model\\|effort\\)\\'" name)
             (cons (nth (1- (string-to-number (match-string 1 name)))
                        (append (alist-get 'reviewer_slots magit-auto-review--setup-settings) nil))
                   (intern (match-string 2 name))))
            ((string-match "\\`processing_\\(provider\\|model\\|effort\\)\\'" name)
             (cons (alist-get 'processing magit-auto-review--setup-settings) (intern (match-string 1 name))))))))

(defun magit-auto-review--field-value (field)
  "Return FIELD's current value."
  (let ((target (magit-auto-review--choice-target field)))
    (if target (alist-get (cdr target) (car target))
      (alist-get field magit-auto-review--setup-settings))))

(defun magit-auto-review--setup-model-for (provider name)
  "Return metadata for PROVIDER's model NAME."
  (cl-find name (alist-get 'models (alist-get (intern provider) magit-auto-review--setup-catalogs))
           :key (lambda (m) (alist-get 'value m)) :test #'equal))

(defun magit-auto-review--setup-model (provider)
  "Return metadata for PROVIDER's legacy selected model."
  (magit-auto-review--setup-model-for provider
    (alist-get (intern (concat provider "_model")) magit-auto-review--setup-settings)))

(defun magit-auto-review--setup-ready-p ()
  "Whether every reviewer and processing choice has fresh metadata."
  (let ((roles (append (append (alist-get 'reviewer_slots magit-auto-review--setup-settings) nil)
                       (list (alist-get 'processing magit-auto-review--setup-settings)))))
    (and (car roles)
         (cl-every (lambda (role)
                     (let* ((provider (alist-get 'provider role))
                            (model (magit-auto-review--setup-model-for provider (alist-get 'model role))))
                       (and (eq (alist-get (intern provider) magit-auto-review--setup-status) 'ready)
                            model (member (alist-get 'effort role) (alist-get 'efforts model)))))
                   roles))))

(defun magit-auto-review--set-choice (field value)
  "Set FIELD to VALUE, updating canonical roles."
  (if-let ((callback (plist-get magit-auto-review--choice-adapter :set)))
      (funcall callback field value)
    (let* ((target (magit-auto-review--choice-target field))
           (role (car-safe target)) (property (cdr-safe target)))
      (if (not target)
          (setf (alist-get field magit-auto-review--setup-settings) value)
        (cl-pushnew field magit-auto-review--setup-roles-edited)
        (when (and (eq property 'provider) (not (equal value (alist-get 'provider role))))
          (setf (alist-get 'model role) (alist-get (intern (concat value "_model")) magit-auto-review--setup-settings)
                (alist-get 'effort role) (alist-get (intern (concat value "_effort")) magit-auto-review--setup-settings)))
        (setf (alist-get property role) value)
        (when (string-prefix-p "processing_" (symbol-name field))
          (setq magit-auto-review--setup-provisional-processing nil))
        (when (and (eq field 'reviewer_1_provider) (eq property 'provider))
          (magit-auto-review--adjust-provisional-processing))
        (magit-auto-review--sync-roles)
        (when (eq property 'provider) (magit-auto-review-refresh-choices))))))

(defun magit-auto-review--sync-roles ()
  "Synchronize compatibility fields without making them explicit overrides."
  (let* ((settings magit-auto-review--setup-settings)
         (slots (append (alist-get 'reviewer_slots settings) nil))
         (reviewers (vconcat (mapcar (lambda (slot) (alist-get 'provider slot)) slots)))
         (provider (alist-get 'provider (alist-get 'processing settings)))
         (compat (alist-get '_compatibility settings)))
    (setf (alist-get 'reviewers settings) reviewers
          (alist-get 'processing_provider settings) provider
          (alist-get 'reviewers compat) reviewers
          (alist-get 'processing_provider compat) provider)))

(defun magit-auto-review--adjust-provisional-processing ()
  "Follow a sole reviewer until processing is deliberately selected."
  (when magit-auto-review--setup-provisional-processing
    (let* ((slots (append (alist-get 'reviewer_slots magit-auto-review--setup-settings) nil))
           (provider (if (= (length slots) 1) (alist-get 'provider (car slots))
                       (or magit-auto-review-processing-provider "codex")))
           (processing (alist-get 'processing magit-auto-review--setup-settings)))
      (setf (alist-get 'provider processing) provider
            (alist-get 'model processing) (alist-get (intern (concat provider "_model")) magit-auto-review--setup-settings)
            (alist-get 'effort processing) (alist-get (intern (concat provider "_effort")) magit-auto-review--setup-settings))
      (magit-auto-review--sync-roles))))

(defun magit-auto-review--toggle-second-slot ()
  "Add or remove the second reviewer slot."
  (interactive)
  (cl-pushnew 'reviewers magit-auto-review--setup-roles-edited)
  (let* ((slots (append (alist-get 'reviewer_slots magit-auto-review--setup-settings) nil))
         (first (car slots)))
    (setf (alist-get 'reviewer_slots magit-auto-review--setup-settings)
          (vconcat (if (= (length slots) 2) (list first)
                     (list first (append '((id . "reviewer-2"))
                                         (cl-remove-if (lambda (x) (eq (car x) 'id)) (copy-tree first)))))))
    (magit-auto-review--adjust-provisional-processing)
    (magit-auto-review--sync-roles)
    (magit-auto-review-refresh-choices)))

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

(defun magit-auto-review--choice-open (field)
  "Show a closed selector for FIELD."
  (setq magit-auto-review--setup-field field magit-auto-review--setup-page 0)
  (transient-setup 'magit-auto-review--choice-menu))

(defun magit-auto-review--choices ()
  "Return advertised choices for the current role field."
  (let* ((field magit-auto-review--setup-field)
         (target (magit-auto-review--choice-target field))
         (role (car-safe target)) (property (cdr-safe target))
         (provider (alist-get 'provider role)))
    (cond ((eq property 'provider) '(("claude" . "Claude") ("codex" . "Codex")))
          ((or (eq field 'synthesis_tests) (eq property 'synthesis_tests)) '(("focused" . "Focused checks") ("full" . "Permit one full-suite attempt")))
          ((eq property 'model)
           (mapcar (lambda (m) (cons (alist-get 'value m) (format "%s (%s)" (alist-get 'label m) (alist-get 'value m))))
                   (alist-get 'models (alist-get (intern provider) magit-auto-review--setup-catalogs))))
          ((eq property 'effort)
           (mapcar (lambda (e) (cons e e))
                   (alist-get 'efforts (magit-auto-review--setup-model-for provider (alist-get 'model role))))))))

(defun magit-auto-review--choice-children (_children)
  "Build paginated selection-only suffixes from current metadata."
  (let* ((choices (magit-auto-review--choices))
         (offset (* 20 magit-auto-review--setup-page))
         (page (seq-subseq choices (min offset (length choices)) (min (+ offset 20) (length choices))))
         (keys "1234567890abcdefghij")
         (selected (magit-auto-review--field-value magit-auto-review--setup-field))
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
                        (magit-auto-review--set-choice field (car choice))
                        (transient-setup (or (plist-get magit-auto-review--choice-adapter :menu) 'magit-auto-review-setup-menu)))
                      :transient t)) page)
      (when (> offset 0)
        '(("p" "Previous page" (lambda () (interactive) (cl-decf magit-auto-review--setup-page)) :transient t)))
      (when (< (+ offset 20) (length choices))
        '(("n" "Next page" (lambda () (interactive) (cl-incf magit-auto-review--setup-page)) :transient t)))
      '(("q" "Back" (lambda () (interactive) (transient-setup (or (plist-get magit-auto-review--choice-adapter :menu) 'magit-auto-review-setup-menu))) :transient t))))))

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
    (user-error "Refresh choices and select a supported model and effort for each reviewer and processor"))
  (let ((settings (copy-tree magit-auto-review--setup-settings t)))
    (magit-auto-review--setup-stop)
    (magit-auto-review--launch-review settings nil (magit-auto-review--catalogs-for-request))))

(defun magit-auto-review-setup-submit-automatic ()
  "Explicitly analyse and publish a COMMENT review without staging prompts."
  (interactive)
  (unless (magit-auto-review--setup-ready-p)
    (user-error "Choose supported reviewer and processing models and efforts first"))
  (let ((settings (copy-tree magit-auto-review--setup-settings t))
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
           (magit-auto-review--setup-providers))))

(defun magit-auto-review--setup-preset (preset)
  "Apply PRESET as a default without overriding saved or edited roles."
  (let* ((current magit-auto-review--setup-settings)
         (defaults (if magit-auto-review--setup-saved current
                     `((reviewers . ,(alist-get 'reviewers current))
                       ,@(unless magit-auto-review--setup-provisional-processing
                           `((processing . ,(alist-get 'processing current))))
                       (synthesis_tests . ,(alist-get 'synthesis_tests current))
                       (suggested_changes . ,(alist-get 'suggested_changes current)))))
         (overrides (unless magit-auto-review--setup-saved
                      (cons (cons 'reviewers (alist-get 'reviewers current))
                            (mapcar (lambda (field) (cons field (magit-auto-review--field-value field)))
                                    (delq 'reviewers (copy-sequence magit-auto-review--setup-roles-edited)))))))
    (setq magit-auto-review--setup-settings
          (alist-get 'settings
                     (magit-auto-review--call
                      (append `((command . "settings") (,preset . t) (defaults . ,defaults))
                              overrides))))
    (magit-auto-review-refresh-choices)))

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
  [["Reviewer 1"
    ("c" (lambda () (format "Provider: %s" (magit-auto-review--field-value 'reviewer_1_provider)))
     (lambda () (interactive) (magit-auto-review--choice-open 'reviewer_1_provider)) :transient t)
    ("m" (lambda () (format "Model: %s" (magit-auto-review--field-value 'reviewer_1_model)))
     (lambda () (interactive) (magit-auto-review--choice-open 'reviewer_1_model)) :transient t)
    ("e" (lambda () (format "Effort: %s" (magit-auto-review--field-value 'reviewer_1_effort)))
     (lambda () (interactive) (magit-auto-review--choice-open 'reviewer_1_effort)) :transient t)]
   ["Reviewer 2"
    ("x" (lambda () (if (> (length (alist-get 'reviewer_slots magit-auto-review--setup-settings)) 1) "Remove second reviewer" "Add second reviewer"))
     magit-auto-review--toggle-second-slot :transient t)
    ("C" (lambda () (format "Provider: %s" (magit-auto-review--field-value 'reviewer_2_provider)))
     (lambda () (interactive) (magit-auto-review--choice-open 'reviewer_2_provider)) :transient t
     :inapt-if (lambda () (< (length (alist-get 'reviewer_slots magit-auto-review--setup-settings)) 2)))
    ("M" (lambda () (format "Model: %s" (magit-auto-review--field-value 'reviewer_2_model)))
     (lambda () (interactive) (magit-auto-review--choice-open 'reviewer_2_model)) :transient t
     :inapt-if (lambda () (< (length (alist-get 'reviewer_slots magit-auto-review--setup-settings)) 2)))
    ("E" (lambda () (format "Effort: %s" (magit-auto-review--field-value 'reviewer_2_effort)))
     (lambda () (interactive) (magit-auto-review--choice-open 'reviewer_2_effort)) :transient t
     :inapt-if (lambda () (< (length (alist-get 'reviewer_slots magit-auto-review--setup-settings)) 2)))] ]
  ["Processing"
   ("b" "Economical: Haiku + Terra/high" magit-auto-review-setup-economical :transient t)
   ("B" "Expensive: Fable + Astra/high" magit-auto-review-setup-expensive :transient t)
   ("p" (lambda () (format "Provider: %s" (magit-auto-review--field-value 'processing_provider)))
    (lambda () (interactive) (magit-auto-review--choice-open 'processing_provider)) :transient t)
   ("o" (lambda () (format "Model: %s" (magit-auto-review--field-value 'processing_model)))
    (lambda () (interactive) (magit-auto-review--choice-open 'processing_model)) :transient t)
   ("O" (lambda () (format "Effort: %s" (magit-auto-review--field-value 'processing_effort)))
    (lambda () (interactive) (magit-auto-review--choice-open 'processing_effort)) :transient t)
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
