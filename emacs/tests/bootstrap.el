;;; bootstrap.el --- Load only checkout code for tests -*- lexical-binding: t; -*-

;;; Commentary:
;; Dependency setup runs before this file, with site startup disabled.  Reject
;; foreign code already loaded there; changing load-path cannot undo a require.

;;; Code:
(require 'cl-lib)

(defconst auto-review-test-source-directory
  (file-name-as-directory
   (expand-file-name ".." (file-name-directory load-file-name))))
(defconst auto-review-test-bytecode-directory
  (getenv "AUTO_REVIEW_TEST_BYTECODE_DIR"))

(defun auto-review-test-check-origin (file)
  "Reject auto-review code loaded from FILE outside the test checkout."
  (unless (and file
               (or (file-in-directory-p file auto-review-test-source-directory)
                   (and auto-review-test-bytecode-directory
                        (file-in-directory-p file auto-review-test-bytecode-directory))))
    (error "Auto-review test contamination: %s; expected code from %s or its temporary bytecode directory. AUTO_REVIEW_TEST_LOAD_FILE must load only dependencies, not an installed auto-review package"
           (or file "unknown definition origin") auto-review-test-source-directory)))

(defun auto-review-test-assert-origin ()
  "Check loaded auto-review modules and representative function definitions."
  (dolist (entry load-history)
    (let ((file (car entry)))
      (when (and (stringp file)
                 (or (string-prefix-p "magit-auto-review" (file-name-nondirectory file))
                     (cl-some (lambda (item)
                                (and (consp item) (eq (car item) 'provide)
                                     (symbolp (cdr item))
                                     (string-prefix-p "magit-auto-review" (symbol-name (cdr item)))))
                              (cdr entry))))
        (auto-review-test-check-origin file))))
  (dolist (symbol '(magit-auto-review magit-auto-review--default-settings
                   magit-auto-review-owner-open))
    (when (fboundp symbol)
      (auto-review-test-check-origin (symbol-file symbol 'defun)))))

(auto-review-test-assert-origin)
;; Dependency setup may prepend paths.  Restore checkout precedence before
;; requiring the application, but never try to repair foreign preloaded code.
(setq load-path (append (delq nil (list auto-review-test-bytecode-directory
                                       auto-review-test-source-directory))
                        load-path)
      load-prefer-newer t)
;; Check resolution without eagerly loading the application: preloading changes
;; what definitions the byte compiler sees and can alter its diagnostics.
(dolist (file (directory-files auto-review-test-source-directory nil "\\.el$"))
  (auto-review-test-check-origin (locate-library (file-name-base file))))

;;; bootstrap.el ends here
