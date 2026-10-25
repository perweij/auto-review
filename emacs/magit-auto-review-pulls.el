;;; magit-auto-review-pulls.el --- Repository PR selection -*- lexical-binding: t; -*-

;; SPDX-License-Identifier: GPL-3.0-or-later

;;; Commentary:
;; Opening reads saved data.  g explicitly fetches GitHub's PR list.  Selecting
;; a PR opens its own review buffer, preserving other reviews and inline edits.

;;; Code:
(require 'magit-auto-review)

(declare-function magit-auto-review-run "magit-auto-review-setup" ())

(defvar-local magit-auto-review--pulls nil)
(defvar-local magit-auto-review--pulls-process nil)

(defvar magit-auto-review-pulls-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-section-mode-map)
    (dolist (binding '(("g" . magit-auto-review-pulls-refresh)
                       ("RET" . magit-auto-review-pulls-open)
                       ("R" . magit-auto-review-pulls-run)
                       ("o" . magit-auto-review-pulls-number)
                       ("q" . quit-window)))
      (define-key map (kbd (car binding)) (cdr binding)))
    map))

(define-derived-mode magit-auto-review-pulls-mode magit-section-mode "Auto-review PRs"
  "Select a PR; g refreshes GitHub without running reviewers."
  (setq-local buffer-read-only t)
  (setq-local revert-buffer-function (lambda (&rest _) (magit-auto-review-pulls-refresh)))
  (magit-auto-review--pulls-render))

(defun magit-auto-review--pull-at-point ()
  "Return the selected PR, if point is on a PR section."
  (let ((section (magit-current-section)))
    (when (and section (eq (oref section type) 'pull-request))
      (oref section value))))

(defun magit-auto-review--pulls-render ()
  "Render saved PR discovery data, preserving a selected open PR."
  (let* ((selected (alist-get 'pr (magit-auto-review--pull-at-point)))
         (rows (alist-get 'pull_requests magit-auto-review--pulls))
         (checked (alist-get 'checked_at magit-auto-review--pulls))
         (inhibit-read-only t) first current)
    (erase-buffer)
    (magit-insert-section (pull-requests)
      (magit-insert-heading (format "Pull requests · %s"
                                   (or magit-auto-review--repository "Choose a repository with g")))
      (insert "g refresh from GitHub · RET open · R configure/run · o open by number\n")
      (insert (if checked (format "GitHub list last checked %s; showing saved data.\n\n" checked)
                "GitHub list not checked. Press g to discover open pull requests.\n\n"))
      (dolist (open '(t nil))
        (let ((group (cl-remove-if-not
                      (lambda (row) (eq open (equal (alist-get 'state row) "OPEN"))) rows)))
          (when group
            (magit-insert-section (pull-group open)
              (magit-insert-heading (if open "Open pull requests" "Other saved reviews"))
              (dolist (row group)
                (magit-insert-section (pull-request row)
                  (unless first (setq first (point)))
                  (when (and open (equal selected (alist-get 'pr row))) (setq current (point)))
                  (magit-insert-heading
                    (format "#%s  %s · %s · %s"
                            (alist-get 'pr row) (replace-regexp-in-string "[\n\r\t]" " " (alist-get 'title row))
                            (if (equal (alist-get 'state row) "UNKNOWN") "state not checked"
                              (if (magit-auto-review--true-p (alist-get 'draft row)) "DRAFT" (alist-get 'state row)))
                            (if (alist-get 'sessions row) "saved review" "no saved review"))))))))))
    (goto-char (or current first (point-min)))
    (set-buffer-modified-p nil)))

(defun magit-auto-review--pulls-accept (result)
  "Display RESULT from PR discovery."
  (setq magit-auto-review--pulls result
        magit-auto-review--repository (alist-get 'repository result))
  (magit-auto-review--pulls-render))

;;;###autoload
(defun magit-auto-review-pull-requests (&optional directory)
  "Open the PR list for DIRECTORY, initially using only saved local data."
  (interactive)
  (let* ((directory (or directory (magit-toplevel) default-directory))
         (repository magit-auto-review--repository)
         (result (magit-auto-review--call `((command . "pull-requests") (cwd . ,directory)
                                          (repository . ,repository)))))
    (unless (or (alist-get 'repository result) (not (alist-get 'repositories result)))
      (setq repository (completing-read "Repository: " (alist-get 'repositories result) nil t)
            result (magit-auto-review--call `((command . "pull-requests") (cwd . ,directory)
                                             (repository . ,repository)))))
    (pop-to-buffer (get-buffer-create (format "*Auto-review PRs %s (%s)*"
                                             (or (alist-get 'repository result) "repository")
                                             (abbreviate-file-name directory))))
    (unless (derived-mode-p 'magit-auto-review-pulls-mode) (magit-auto-review-pulls-mode))
    (setq default-directory directory)
    (magit-auto-review--pulls-accept result)))

(defun magit-auto-review-pulls-refresh ()
  "Refresh open PRs and saved PR states from GitHub, without running models."
  (interactive)
  (when (process-live-p magit-auto-review--pulls-process) (user-error "PR discovery is already running"))
  (unless magit-auto-review--repository (user-error "Add a GitHub remote before refreshing pull requests"))
  (setq header-line-format " Refreshing pull requests from GitHub…")
  (setq magit-auto-review--pulls-process
        (magit-auto-review--async
         `((command . "pull-requests") (cwd . ,default-directory)
           (repository . ,magit-auto-review--repository) (refresh . t))
         #'ignore
         (lambda (result)
           (setq magit-auto-review--pulls-process nil header-line-format nil)
           (magit-auto-review--pulls-accept result))
         (lambda (error)
           (setq magit-auto-review--pulls-process nil
                 header-line-format (concat " Refresh failed; saved list retained: " (magit-auto-review--error-message error)))
           (message "%s" header-line-format)))))

(defun magit-auto-review-pulls-open ()
  "Open the selected PR's own review buffer."
  (interactive)
  (let ((pull (magit-auto-review--pull-at-point)))
    (unless pull (user-error "Select a pull request first"))
    (magit-auto-review--open-pr default-directory (alist-get 'pr pull) (alist-get 'repository pull))))

(defun magit-auto-review-pulls-run ()
  "Open setup for the selected PR; no model runs until Run/resume is chosen."
  (interactive)
  (magit-auto-review-pulls-open)
  (magit-auto-review-run))

(defun magit-auto-review-pulls-number (number)
  "Open NUMBER explicitly, even before fetching a PR list."
  (interactive (list (read-number "Pull request number: ")))
  (unless (and (integerp number) (> number 0)) (user-error "Choose a positive pull request number"))
  (magit-auto-review--open-pr default-directory number magit-auto-review--repository))

(provide 'magit-auto-review-pulls)
;;; magit-auto-review-pulls.el ends here
