;;; guix-installed.el --- Installed package acceptance -*- lexical-binding: t; -*-
(require 'ert)
(require 'forge)
(require 'magit-auto-review)

(ert-deftest auto-review-package/store-frontend-and-forge ()
  (should (file-in-directory-p (locate-library "magit-auto-review")
                              (getenv "AUTO_REVIEW_FRONTEND")))
  (should (transient-get-suffix 'forge-dispatch "V"))
  (should (transient-get-suffix 'forge-topic-menu "V")))

(ert-deftest auto-review-package/paired-backend-ignores-path-shadow ()
  (should (string-prefix-p "/gnu/store/" magit-auto-review-executable))
  (should (file-executable-p magit-auto-review-executable))
  (let* ((shadow (make-temp-file "shadow-backend-" t))
         (exec-path (cons shadow exec-path))
         (process-environment (copy-sequence process-environment)))
    (unwind-protect
        (progn
          (with-temp-file (expand-file-name "pr-review" shadow)
            (insert "#!/bin/sh\nexit 97\n"))
          (set-file-modes (expand-file-name "pr-review" shadow) #o755)
          (setenv "PATH" (concat shadow ":" (getenv "PATH")))
          (should (equal (executable-find "pr-review")
                         (expand-file-name "pr-review" shadow)))
          (let ((result (magit-auto-review--call '((command . "capabilities")))))
            (should (= (alist-get 'protocol result) 1))
            (should (member "owner-fix" (alist-get 'commands result)))))
      (delete-directory shadow t))))

(ert-deftest auto-review-package/installed-runtime ()
  (let ((python (with-temp-buffer
                  (insert-file-contents magit-auto-review-executable)
                  (goto-char (point-min))
                  (should (looking-at "#!\\(/gnu/store/[^ \n]+/bin/python3\\)\n"))
                  (match-string 1))))
    (with-temp-buffer
      (let ((status (call-process python nil t nil
                                  (getenv "AUTO_REVIEW_RUNTIME_TEST")
                                  magit-auto-review-executable)))
        (unless (equal status 0)
          (ert-fail (buffer-string)))))))
