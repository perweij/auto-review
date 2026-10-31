;;; Reusable recipes: source and version belong to the consuming configuration.
;;; SOURCE must yield a directory (for example local-file or a git-fetch origin).
(define-module (auto-review packages)
  #:use-module (guix packages)
  #:use-module (guix gexp)
  #:use-module (guix build-system trivial)
  #:use-module (guix build-system emacs)
  #:use-module (gnu packages)
  #:use-module ((guix licenses) #:prefix license:)
  #:export (make-auto-review-backend
            make-emacs-magit-auto-review))

(define* (make-auto-review-backend source #:key (version "0.1.0"))
  "Build the backend from SOURCE using the caller's Guix dependency graph."
  (let ((python (specification->package "python"))
        (git (specification->package "git"))
        (github-cli (specification->package "github-cli"))
        (openssh (specification->package "openssh"))
        (certs (specification->package "nss-certs")))
    (package
      (name "auto-review")
      (version version)
      (source source)
      (build-system trivial-build-system)
      (arguments
       (list
        #:modules '((guix build utils))
        #:builder
        #~(begin
            (use-modules (guix build utils))
            ;; NSS has individual PEM files; Guix normally assembles their
            ;; bundle in a profile hook.  This private input has no profile.
            ;; Match that hook's UTF-8 filename handling and PEM separators.
            (setlocale LC_ALL "C.UTF-8")
            (let* ((files (sort (find-files
                                (string-append #$certs "/etc/ssl/certs")
                                "\\.pem$")
                               string<?))
                   (directory (string-append #$output "/share/auto-review"))
                   (bundle (string-append directory "/ca-certificates.crt")))
              (when (null? files)
                (error "auto-review: no PEM certificates in nss-certs"))
              (mkdir-p directory)
              (call-with-output-file bundle
                (lambda (port)
                  (for-each
                   (lambda (file)
                     (call-with-input-file file
                       (lambda (input) (dump-port input port)))
                     (newline port))
                   files)))
              ;; Load only this bundle so host trust cannot hide a bad output.
              (invoke/quiet
               (string-append #$python "/bin/python3") "-c"
               (string-append
                "import ssl, sys; "
                "context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); "
                "context.load_verify_locations(cafile=sys.argv[1]); "
                "assert context.cert_store_stats()['x509_ca'] > 0")
               bundle))
            (for-each
             (lambda (directory)
               (copy-recursively (string-append #$source "/" directory)
                                 (string-append #$output "/" directory)))
             '("bin" "lib" "prompts"))
            (let ((license-file (string-append #$source "/LICENSE")))
              (when (file-exists? license-file)
                (install-file license-file
                              (string-append #$output "/share/doc/auto-review"))))
            (for-each
             (lambda (entry)
              (let ((program (string-append #$output "/bin/" entry))
                  (interpreter-patches 0)
                  (environment-patches 0))
              (substitute* program
                (("^#!/usr/bin/env python3[[:space:]]*$")
                 (set! interpreter-patches (+ 1 interpreter-patches))
                 (string-append "#!" #$python "/bin/python3\n"))
                ;; substitute* includes the terminating newline in each line.
                (("^import sys[[:space:]]*$")
                 (set! environment-patches (+ 1 environment-patches))
                 (string-append
                  "import sys\nimport os\n"
                  "os.environ['PATH'] = '"
                  #$git "/bin:" #$github-cli "/bin:" #$openssh
                  "/bin:' + os.environ.get('PATH', '')\n"
                  "os.environ.setdefault('SSL_CERT_DIR', '"
                  #$certs "/etc/ssl/certs')\n"
                  "os.environ.setdefault('SSL_CERT_FILE', '"
                  #$output "/share/auto-review/ca-certificates.crt')\n")))
              (unless (and (= interpreter-patches 1)
                           (= environment-patches 1))
                (error "auto-review entry-point patch mismatch"
                       interpreter-patches environment-patches))
              (invoke/quiet program "--help")))
             '("pr-review" "fix-issue")))))
      ;; These are private runtime inputs, not extra commands in the user's
      ;; profile.  Host model CLIs, credentials, signing and editor stay inherited.
      (inputs (list python git github-cli openssh certs))
      (home-page "https://github.com/perweij/auto-review")
      (synopsis "Local GitHub review orchestration and versioned frontend API")
      (description
       "Run independent review models, retain drafts and historical contexts,
and publish confirmed GitHub review comments.  Model CLI programs are provided
by the caller; local draft operations do not require them.")
      (license license:gpl3+))))

(define (make-emacs-magit-auto-review backend)
  "Build the frontend from BACKEND's source and version, paired with BACKEND."
  (package
    (name "emacs-magit-auto-review")
    (version (package-version backend))
    (source (package-source backend))
    (build-system emacs-build-system)
    (arguments
     (list #:lisp-directory "emacs"
           #:tests? #t
           #:test-command #~(list "bash" "../emacs/tests/run.sh")
           #:phases
           #~(modify-phases %standard-phases
               (add-after 'patch-source-shebangs 'patch-test-backend
                 (lambda _
                   ;; emacs-build-system changes into emacs/ before the
                   ;; standard shebang scan.  The integration tests launch
                   ;; this sibling source script, outside that scan; the
                   ;; build sandbox has no /usr/bin/env.
                   (patch-shebang "../bin/pr-review")))
               (add-after 'unpack 'pair-backend
                 (lambda _
                   (let ((backend-patches 0))
                     (substitute* "magit-auto-review-api.el"
                       (("^\\(defcustom magit-auto-review-executable \"pr-review\"")
                        (set! backend-patches (+ 1 backend-patches))
                        (string-append "(defcustom magit-auto-review-executable \""
                                       #$backend "/bin/pr-review\"")))
                     (unless (= backend-patches 1)
                       (error "auto-review frontend/backend pairing patch mismatch"
                              backend-patches)))))
               (add-after 'install 'install-license
                 (lambda _
                   (when (file-exists? "../LICENSE")
                     (install-file
                      "../LICENSE"
                      (string-append #$output "/share/doc/emacs-magit-auto-review")))))
               (add-before 'install 'remove-test-lisp
                 (lambda _ (delete-file-recursively "tests"))))))
    (native-inputs (map specification->package '("bash" "git" "python")))
    (propagated-inputs
     (list (specification->package "emacs-magit")
           (specification->package "emacs-forge")
           backend))
    (home-page "https://github.com/perweij/auto-review")
    (synopsis "Inspect and stage contextual PR review comments in Magit")
    (description
     "A Magit-section and Transient interface for auto-review, with inline
draft editing, discussion-aware placement, explicit staging, immutable review
history, and exact-comment publication through a matching backend.")
    (license license:gpl3+)))
