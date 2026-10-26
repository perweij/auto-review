;; Local development entry point; the caller supplies the Guix channel pin.
;; guix time-machine -C /path/to/channels.scm -- build -f guix.scm
(use-modules (guix gexp)
             (srfi srfi-1)
             (srfi srfi-13))

(define auto-review-root
  (dirname (canonicalize-path (current-filename))))

;; add-to-load-path also runs at expansion time, before the root definition
;; above has necessarily been evaluated by Guile's compiler.
(add-to-load-path
 (string-append (dirname (canonicalize-path (current-filename))) "/guix"))
(use-modules (auto-review packages))

;; This is deliberately an allowlist: credentials, state, editor settings and
;; unrelated untracked files in the checkout must not enter the store.
(define (auto-review-source-file? file stat)
  (let* ((relative (string-drop file (string-length auto-review-root)))
         (parts (string-split (string-trim relative #\/) #\/)))
    (and (or (string-null? relative)
             (member (car parts)
                     '("bin" "lib" "prompts" "emacs" "tests" "guix"
                       "guix.scm" "Makefile" "LICENSE")))
         (not (any (lambda (name)
                     (or (string-prefix? "." name)
                         (member name '("__pycache__" "state"))
                         (string-suffix? ".pyc" name)
                         (string-suffix? ".elc" name)
                         (string-suffix? "~" name)))
                   parts))
         ;; Never follow an unexpected symlink into files outside the checkout.
         (memq (stat:type stat) '(regular directory)))))

(define auto-review-source
  (local-file auto-review-root "auto-review-source"
              #:recursive? #t
              #:select? auto-review-source-file?))

(make-emacs-magit-auto-review
 (make-auto-review-backend auto-review-source #:version "0.1.0"))
