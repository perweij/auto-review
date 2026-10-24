;; Build the local source snapshot with the caller's pinned Guix graph:
;; guix time-machine -C ../stable-emacs/channels.scm -- build -f guix.scm
;; No independent Magit/Transient pins and no runtime package installation.
(use-modules (guix packages)
             (guix gexp)
             (guix build-system trivial)
             (guix build-system emacs)
             (gnu packages)
             ((guix licenses) #:prefix license:))

(define auto-review-root
  (dirname (canonicalize-path (current-filename))))

(define auto-review-source
  (local-file auto-review-root "auto-review-source"
              #:recursive? #t
              #:select? (lambda (file stat)
                          (let ((name (basename file)))
                            (not (or (member name '(".git" "state" "__pycache__"))
                                     (string-suffix? ".pyc" name)
                                     (string-suffix? ".elc" name)))))))

(define auto-review-backend
  (package
    (name "auto-review")
    (version "0.1.0")
    (source auto-review-source)
    (build-system trivial-build-system)
    (arguments
     (list
      #:modules '((guix build utils))
      #:builder
      #~(begin
          (use-modules (guix build utils))
          (for-each (lambda (directory)
                      (copy-recursively (string-append #$auto-review-source "/" directory)
                                        (string-append #$output "/" directory)))
                    '("bin" "lib" "prompts"))
          (let ((program (string-append #$output "/bin/pr-review")))
            (substitute* program
              (("#!/usr/bin/env bash")
               (string-append "#!" #$(specification->package "bash") "/bin/bash")))
            (wrap-program program
              `("PATH" prefix
                ,(map (lambda (package) (string-append package "/bin"))
                      '#$(map specification->package
                               '("bash" "coreutils" "findutils" "grep" "sed" "jq"
                                 "git" "gh" "util-linux" "python")))))))))
    (inputs (map specification->package
                 '("bash" "coreutils" "findutils" "grep" "sed" "jq"
                   "git" "gh" "util-linux" "python")))
    (home-page "https://github.com/perweij/auto-review")
    (synopsis "Local GitHub review orchestration and versioned frontend API")
    (description "Run independent review models, retain drafts and historical
contexts, and publish confirmed GitHub review comments.  Model CLI programs are
provided by the caller; local draft operations do not require them.")
    ;; The existing backend repository has no declared license.
    (license #f)))

(define emacs-magit-auto-review
  (package
    (name "emacs-magit-auto-review")
    (version "0.1.0")
    (source auto-review-source)
    (build-system emacs-build-system)
    (arguments
     (list #:lisp-directory "emacs"
           #:tests? #t
           #:test-command #~(list "bash" "../emacs/tests/run.sh")
           #:phases
           #~(modify-phases %standard-phases
               (add-before 'install 'remove-test-lisp
                 (lambda _ (delete-file-recursively "tests"))))))
    (native-inputs (map specification->package '("bash" "git" "python")))
    (propagated-inputs
     (list (specification->package "emacs-magit")
           (specification->package "emacs-forge") auto-review-backend))
    (home-page "https://github.com/perweij/auto-review")
    (synopsis "Inspect and stage contextual PR review comments in Magit")
    (description "A Magit-section and Transient interface for auto-review, with
inline draft editing, discussion-aware placement, explicit staging, immutable
review history, and exact-comment publication through a versioned backend API.")
    (license license:gpl3+)))

emacs-magit-auto-review
