#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
bytecode_dir=$(mktemp -d "${TMPDIR:-/tmp}/auto-review-elc.XXXXXX")
trap 'rm -rf -- "$bytecode_dir"' EXIT
args=(--batch -L emacs -L "$bytecode_dir" --eval '(setq load-prefer-newer t)')
if [[ -n ${AUTO_REVIEW_TEST_LOAD_FILE:-} ]]; then args+=(-l "$AUTO_REVIEW_TEST_LOAD_FILE"); fi
AUTO_REVIEW_TEST_BYTECODE_DIR="$bytecode_dir" "${EMACS:-emacs}" "${args[@]}" --eval '(progn
  (unless (version<= "29.1" emacs-version) (error "Tests require Emacs 29.1 or newer"))
  (setq byte-compile-error-on-warn t)
  (setq byte-compile-dest-file-function
        (lambda (file) (expand-file-name (concat (file-name-base file) ".elc")
                                        (getenv "AUTO_REVIEW_TEST_BYTECODE_DIR"))))
  (mapc (lambda (file) (unless (byte-compile-file file) (error "Byte compilation failed: %s" file)))
        (directory-files "emacs" t "\\.el$")))'
"${EMACS:-emacs}" "${args[@]}" -l emacs/tests/magit-auto-review-tests.el -l emacs/tests/magit-auto-review-owner-tests.el -f ert-run-tests-batch-and-exit
