#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
bytecode_dir=$(mktemp -d "${TMPDIR:-/tmp}/auto-review-elc.XXXXXX")
trap 'rm -rf -- "$bytecode_dir"' EXIT
export AUTO_REVIEW_TEST_BYTECODE_DIR="$bytecode_dir"
args=(--batch --no-site-file -L emacs -L "$bytecode_dir" --eval '(setq load-prefer-newer t)')
if [[ -n ${AUTO_REVIEW_TEST_LOAD_FILE:-} ]]; then
    if [[ ! -f $AUTO_REVIEW_TEST_LOAD_FILE ]]; then
        printf 'Dependency setup file not found: %s. Unset AUTO_REVIEW_TEST_LOAD_FILE to use automatic discovery.\n' "$AUTO_REVIEW_TEST_LOAD_FILE" >&2
        exit 1
    fi
    args+=(-l "$AUTO_REVIEW_TEST_LOAD_FILE")
else
    # Container dependencies are source checkouts, with Lisp either at the root
    # or in lisp/. Add only known dependencies; never load package startup files.
    dependency_root=${AUTO_REVIEW_EMACS_DEPENDENCY_ROOT:-/opt/emacs-lisp}
    for package in compat cond-let llama seq with-editor transient magit closql emacsql ghub treepy markdown-mode yaml forge; do
        directory="$dependency_root/$package"
        if [[ -d $directory/lisp ]]; then directory+=/lisp; fi
        if [[ -d $directory ]]; then args+=(-L "$directory"); fi
    done
fi
args+=(-l emacs/tests/bootstrap.el)
"${EMACS:-emacs}" "${args[@]}" --eval '(progn
  (unless (version<= "29.1" emacs-version) (error "Tests require Emacs 29.1 or newer"))
  (setq byte-compile-error-on-warn t)
  (setq byte-compile-dest-file-function
        (lambda (file) (expand-file-name (concat (file-name-base file) ".elc")
                                        (getenv "AUTO_REVIEW_TEST_BYTECODE_DIR"))))
  (dolist (file (directory-files "emacs" t "\\.el$"))
    ;; Guix generates this package descriptor during the build.
    (unless (equal (file-name-nondirectory file) "magit-auto-review-pkg.el")
      (unless (byte-compile-file file) (error "Byte compilation failed: %s" file))))
  (when (file-directory-p "tests/forge_smoke")
    (dolist (file (directory-files "tests/forge_smoke" t "\\.el$"))
      (with-temp-buffer (insert-file-contents file) (emacs-lisp-mode) (check-parens))))
  (auto-review-test-assert-origin))'
"${EMACS:-emacs}" "${args[@]}" -l emacs/tests/magit-auto-review-tests.el -l emacs/tests/magit-auto-review-owner-tests.el -l emacs/tests/magit-auto-review-gitlab-tests.el --eval '(auto-review-test-assert-origin)' -f ert-run-tests-batch-and-exit
python3 tests/helpers/gitlab-emacs-fixture.py "${EMACS:-emacs}" "${args[@]}"
