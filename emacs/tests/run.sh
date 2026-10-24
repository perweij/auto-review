#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
args=(--batch -L emacs --eval '(setq load-prefer-newer t)')
if [[ -n ${AUTO_REVIEW_TEST_LOAD_FILE:-} ]]; then args+=(-l "$AUTO_REVIEW_TEST_LOAD_FILE"); fi
"${EMACS:-emacs}" "${args[@]}" --eval '(progn
  (unless (version<= "29.1" emacs-version) (error "Tests require Emacs 29.1 or newer"))
  (setq byte-compile-error-on-warn t)
  (mapc (lambda (file) (unless (byte-compile-file file) (error "Byte compilation failed: %s" file)))
        (directory-files "emacs" t "\\.el$")))'
"${EMACS:-emacs}" "${args[@]}" -l emacs/tests/magit-auto-review-tests.el -f ert-run-tests-batch-and-exit
