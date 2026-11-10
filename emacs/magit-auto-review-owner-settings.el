;;; magit-auto-review-owner-settings.el --- Owner settings draft -*- lexical-binding: t; -*-
;; SPDX-License-Identifier: GPL-3.0-or-later
;;; Commentary:
;; Shared selection controls, with Owner-specific roles and a single local save.
;;; Code:
(require 'magit-auto-review-owner)
(require 'magit-auto-review-setup)
(defvar-local magit-auto-review-owner-settings--draft nil)
(defvar-local magit-auto-review-owner-settings--baseline nil)
(defvar-local magit-auto-review-owner-settings--batch nil)

(defun magit-auto-review-owner-settings--final ()
  "Return explicit or derived final-review choices."
  (or (alist-get 'final_review magit-auto-review-owner-settings--draft)
      (let ((review (copy-tree (alist-get 'review magit-auto-review-owner-settings--draft))))
        `((reviewer_slots . ,(vector (cons '(id . "reviewer-1") (copy-tree review))))
          (synthesis . ,review) (synthesis_tests . "focused")))))

(defun magit-auto-review-owner-settings--target (field)
  "Resolve FIELD to a choice and property."
  (let* ((parts (split-string (symbol-name field) "_"))
         (role (car parts)) (property (intern (cadr parts)))
         (final (magit-auto-review-owner-settings--final)))
    (cons (pcase role
            ((or "fix" "review") (alist-get (intern role) magit-auto-review-owner-settings--draft))
            ("first" (elt (alist-get 'reviewer_slots final) 0))
            ("second" (nth 1 (append (alist-get 'reviewer_slots final) nil)))
            ("synthesis" (alist-get 'synthesis final))
            ("tests" final))
          (if (equal role "tests") 'synthesis_tests property))))

(defun magit-auto-review-owner-settings--set (field value)
  "Change FIELD to VALUE only in the draft."
  (unless (memq field '(fix_provider fix_model fix_effort review_provider review_model review_effort))
    (unless (alist-get 'final_review magit-auto-review-owner-settings--draft)
      (setf (alist-get 'final_review magit-auto-review-owner-settings--draft)
            (magit-auto-review-owner-settings--final))))
  (let* ((target (magit-auto-review-owner-settings--target field))
         (role (car target)) (property (cdr target)))
    (when (and (eq property 'provider) (not (equal value (alist-get 'provider role))))
      (let ((defaults (magit-auto-review--default-settings)))
        (setf (alist-get 'model role) (alist-get (intern (concat value "_model")) defaults)
              (alist-get 'effort role) (alist-get (intern (concat value "_effort")) defaults))))
    (setf (alist-get property role) value))
  (when (string-suffix-p "_provider" (symbol-name field))
    (magit-auto-review-refresh-choices)))

(defun magit-auto-review-owner-settings--roles ()
  "Return active choices, including legacy derived roles."
  (let ((final (magit-auto-review-owner-settings--final)))
    (append (list (alist-get 'fix magit-auto-review-owner-settings--draft)
                  (alist-get 'review magit-auto-review-owner-settings--draft))
            (append (alist-get 'reviewer_slots final) nil)
            (list (alist-get 'synthesis final)))))
(defun magit-auto-review-owner-settings--providers ()
  "Return active providers for shared metadata discovery."
  (delete-dups (mapcar (lambda (role) (alist-get 'provider role))
                       (magit-auto-review-owner-settings--roles))))

(defun magit-auto-review-owner-settings-toggle ()
  "Add or remove reviewer 2 without changing synthesis."
  (interactive)
  (let* ((final (copy-tree (magit-auto-review-owner-settings--final) t))
         (slots (append (alist-get 'reviewer_slots final) nil))
         (second (copy-tree (car slots))))
    (setf (alist-get 'id second) "reviewer-2"
          (alist-get 'reviewer_slots final) (if (cdr slots) (vector (car slots)) (vector (car slots) second))
          (alist-get 'final_review magit-auto-review-owner-settings--draft) final))
  (magit-auto-review-refresh-choices))

(defun magit-auto-review-owner-settings-copy-batch ()
  "Copy saved batch settings into the draft, without saving or resuming."
  (interactive)
  (unless magit-auto-review-owner-settings--batch
    (user-error "Select a batch in Owner before opening settings"))
  (setq magit-auto-review-owner-settings--draft
        (copy-tree (alist-get 'settings magit-auto-review-owner-settings--batch) t))
  (magit-auto-review-refresh-choices))

(defun magit-auto-review-owner-settings-save ()
  "Validate and atomically save the draft; keep it on failure."
  (interactive)
  (unless (equal magit-auto-review-owner-settings--draft magit-auto-review-owner-settings--baseline)
    (dolist (role (magit-auto-review-owner-settings--roles))
      (let* ((provider (alist-get 'provider role))
             (model (magit-auto-review--setup-model-for provider (alist-get 'model role))))
        (unless (and (eq (alist-get (intern provider) magit-auto-review--setup-status) 'ready)
                     model (member (alist-get 'effort role) (alist-get 'efforts model)))
          (user-error "Refresh choices and select a supported model and effort for every role"))))
    (magit-auto-review-owner--mutate
     "settings"
     `(settings . ,(magit-auto-review-owner--settings-payload magit-auto-review-owner-settings--draft))
     `(expected_settings . ,(magit-auto-review-owner--settings-payload magit-auto-review-owner-settings--baseline))))
  (transient-quit-one)
  (magit-auto-review--setup-exited))

(defun magit-auto-review-owner-settings--children (_)
  "Build all role controls using the shared selector."
  (let ((keys '("f" "r" "1" "2" "s")) rows)
    (dolist (role '(fix review first second synthesis))
      (let* ((prefix (pop keys))
             (choice (car (magit-auto-review-owner-settings--target (intern (format "%s_provider" role)))))
             (label (pcase role ('fix "Implementation") ('review "Task check") ('first "Final reviewer 1") ('second "Final reviewer 2") (_ "Final synthesis"))))
        (when choice
          (push label rows)
          (dolist (property '(provider model effort))
            (let ((field (intern (format "%s_%s" role property))))
              (push (list (concat prefix (pcase property ('provider "p") ('model "m") (_ "e")))
                          (format "%s: %s%s" property (alist-get property choice)
                                  (if (or (eq property 'provider)
                                          (let ((model (magit-auto-review--setup-model-for
                                                        (alist-get 'provider choice) (alist-get 'model choice))))
                                            (and model (or (eq property 'model)
                                                           (member (alist-get 'effort choice) (alist-get 'efforts model))))))
                                      "" " — choose a supported value"))
                          (lambda () (interactive) (magit-auto-review--choice-open field)) :transient t) rows))))))
    (dolist (provider (magit-auto-review-owner-settings--providers))
      (push (format "%s metadata: %s" provider (alist-get (intern provider) magit-auto-review--setup-status)) rows))
    (when magit-auto-review-owner-settings--batch
      (push (format "Batch %s: %s" (alist-get 'id magit-auto-review-owner-settings--batch)
                    (if (equal magit-auto-review-owner-settings--draft
                               (alist-get 'settings magit-auto-review-owner-settings--batch))
                        "matches saved settings" "differs; restore saved settings before interactive resume")) rows))
    (transient-parse-suffixes 'magit-auto-review-owner-settings-menu (nreverse rows))))

(defun magit-auto-review-owner-settings--discard ()
  "Discard unsaved local settings."
  (setq magit-auto-review-owner-settings--draft nil
        magit-auto-review-owner-settings--baseline nil
        magit-auto-review--choice-adapter nil))

(defun magit-auto-review-owner-settings-open ()
  "Initialize an isolated settings draft and discover active model metadata."
  (when (process-live-p magit-auto-review-owner--process) (user-error "An Owner operation is running"))
  (magit-auto-review--setup-exited)
  (let ((selected-id (alist-get 'id (magit-auto-review-owner--batch-at-point))))
    (magit-auto-review-owner--accept
     (magit-auto-review--call (magit-auto-review-owner--request "owner-read")))
    (setq magit-auto-review-owner-settings--baseline (copy-tree (alist-get 'settings magit-auto-review-owner--view) t)
          magit-auto-review-owner-settings--draft (copy-tree magit-auto-review-owner-settings--baseline t)
          magit-auto-review-owner-settings--batch
          (let* ((selected (cl-find selected-id (alist-get 'batches magit-auto-review-owner--view)
                                    :key (lambda (batch) (alist-get 'id batch)) :test #'equal))
                 (batches (cl-remove-if (lambda (b) (member (alist-get 'status b) '("complete" "ready" "superseded")))
                                        (alist-get 'batches magit-auto-review-owner--view))))
            (or selected (and (= (length batches) 1) (car batches))))
          magit-auto-review--setup-catalogs nil
          magit-auto-review--setup-owner (current-buffer)
          magit-auto-review--choice-adapter
          '(:menu magit-auto-review-owner-settings-menu
                  :exit magit-auto-review-owner-settings--discard
                  :providers magit-auto-review-owner-settings--providers
                  :target magit-auto-review-owner-settings--target
                  :set magit-auto-review-owner-settings--set))
    (add-hook 'kill-buffer-hook #'magit-auto-review--setup-stop nil t)
    (magit-auto-review-refresh-choices)
    (transient-setup 'magit-auto-review-owner-settings-menu)))

(transient-define-prefix magit-auto-review-owner-settings-menu ()
  "Edit session settings; started batches retain their saved settings."
  :refresh-suffixes t
  [:class transient-column :setup-children magit-auto-review-owner-settings--children]
  ["Changes are local until Save; started batches keep frozen settings"
   ("x" "Add/remove reviewer 2" magit-auto-review-owner-settings-toggle :transient t)
   ("t" (lambda () (format "Synthesis tests: %s" (alist-get 'synthesis_tests (magit-auto-review-owner-settings--final))))
    (lambda () (interactive) (magit-auto-review--choice-open 'tests_policy)) :transient t)
   ("b" "Use saved batch settings" magit-auto-review-owner-settings-copy-batch :transient t)
   ("g" "Refresh model choices" magit-auto-review-refresh-choices :transient t)
   ("S" "Save" magit-auto-review-owner-settings-save :transient t)
   ("q" "Cancel" transient-quit-one)])
(provide 'magit-auto-review-owner-settings)
;;; magit-auto-review-owner-settings.el ends here
