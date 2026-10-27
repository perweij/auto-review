;;; magit-auto-review-api.el --- Auto-review protocol transport -*- lexical-binding: t; -*-

;; SPDX-License-Identifier: GPL-3.0-or-later

;;; Commentary:
;; Only this module invokes the backend.  No frontend code reads private files.

;;; Code:
(require 'json)
(require 'subr-x)

(defgroup magit-auto-review nil
  "Inspect and publish automated reviews using Magit sections."
  :group 'magit)

(defcustom magit-auto-review-executable "pr-review"
  "Backend executable implementing auto-review protocol version 1."
  :type 'file :group 'magit-auto-review)

(define-error 'magit-auto-review-api-error "Auto-review backend error" 'user-error)

(defun magit-auto-review--error-message (error)
  "Return the human-readable message from ERROR without protocol details."
  (if (eq (car-safe error) 'magit-auto-review-api-error)
      (or (cadr error) "Auto-review backend error")
    (error-message-string error)))

(defun magit-auto-review--get (object key)
  "Return KEY from a protocol OBJECT."
  (alist-get key object))

(defun magit-auto-review--json (string)
  "Decode protocol STRING with explicit false/null values."
  (json-parse-string string :object-type 'alist :array-type 'list
                     :null-object nil :false-object :false))

(defun magit-auto-review--true-p (value)
  "Return non-nil if VALUE is a protocol true value."
  (eq value t))

(defun magit-auto-review--encode (request)
  "Encode REQUEST as a single protocol line with an absolute working directory."
  ;; Emacs accepts abbreviated home paths, but the backend passes cwd directly
  ;; to subprocesses without shell expansion.  Copy before normalizing so saved
  ;; requests remain unchanged.  Both synchronous and async calls use this path.
  (let ((request (copy-alist request)))
    (when (alist-get 'cwd request)
      (setf (alist-get 'cwd request) (expand-file-name (alist-get 'cwd request))))
    (concat (json-serialize (cons '(protocol . 1) request)
                            :null-object nil :false-object :false) "\n")))

(defun magit-auto-review--event-result (event)
  "Validate and return a final result from EVENT, or raise its error."
  (unless (eq (alist-get 'protocol event) 1)
    (signal 'magit-auto-review-api-error '("Backend protocol mismatch; expected version 1")))
  (if (equal (alist-get 'type event) "error")
      (let ((error (alist-get 'error event)))
        (signal 'magit-auto-review-api-error
                (list (alist-get 'message error) (alist-get 'code error) error)))
    (alist-get 'result event)))

(defun magit-auto-review--call (request)
  "Send a local REQUEST synchronously and return its result."
  (let ((input (magit-auto-review--encode request))
        (output (generate-new-buffer " *auto-review-response*")))
    (unwind-protect
        (with-temp-buffer
          (insert input)
          (let ((status (call-process-region (point-min) (point-max)
                                             magit-auto-review-executable nil output nil "api")))
            (with-current-buffer output
              (let* ((lines (split-string (buffer-string) "\n" t))
                     (event (and lines (magit-auto-review--json (car (last lines))))))
                (unless (and event (member (alist-get 'type event) '("result" "error")))
                  (signal 'magit-auto-review-api-error
                          (list (format "Backend exited %s without a protocol result" status))))
                (magit-auto-review--event-result event)))))
      (kill-buffer output))))

(defun magit-auto-review--async (request on-event on-result on-error)
  "Send REQUEST asynchronously, calling ON-EVENT, ON-RESULT or ON-ERROR.
Callbacks execute in the initiating buffer if it still exists.  Return process."
  (let ((origin (current-buffer)) (pending "") (final nil))
    (let ((process
           (make-process
            :name "auto-review" :command (list magit-auto-review-executable "api")
            :connection-type 'pipe :noquery t :coding 'utf-8-unix
            :stderr (get-buffer-create "*Auto-review process errors*")
            :filter
            (lambda (_process chunk)
              (setq pending (concat pending chunk))
              (while (string-match "\n" pending)
                (let ((line (substring pending 0 (match-beginning 0))))
                  (setq pending (substring pending (match-end 0)))
                  (unless (string-empty-p line)
                    (condition-case error
                        (let ((event (magit-auto-review--json line)))
                          (if (member (alist-get 'type event) '("result" "error"))
                              (setq final event)
                            (when (buffer-live-p origin)
                              (with-current-buffer origin (funcall on-event event)))))
                      (error (setq final `((protocol . 1) (type . "error")
                                          (error . ((code . "invalid_protocol")
                                                    (message . ,(error-message-string error))))))))))))
            :sentinel
            (lambda (process _event)
              (when (and (memq (process-status process) '(exit signal))
                         (buffer-live-p origin))
                (with-current-buffer origin
                  (condition-case error
                      (if final
                          (funcall on-result (magit-auto-review--event-result final))
                        (signal 'magit-auto-review-api-error '("Backend stopped without a result; saved drafts remain available")))
                    (error (funcall on-error error)))))))))
      (process-send-string process (magit-auto-review--encode request))
      (process-send-eof process)
      process)))

(provide 'magit-auto-review-api)
;;; magit-auto-review-api.el ends here
