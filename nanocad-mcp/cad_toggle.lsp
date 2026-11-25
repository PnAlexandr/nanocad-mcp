;; The helper finds this nanoCAD through its parent chain and answers through
;; a file. toggle.log records each step, so a crash can be located.
(defun nanocad-mcp-write (path mode text / f)
  (if (setq f (open path mode))
    (progn (write-line text f) (close f) T)))

(defun nanocad-mcp-read (path / f line)
  (if (setq f (open path "r"))
    (progn (setq line (read-line f)) (close f) line)))

(defun nanocad-mcp-arg (s)
  (if (vl-string-search " " s) (strcat (chr 34) s (chr 34)) s))

(defun nanocad-mcp-press (env root journal / nonce result started code n stop)
  (setq nonce (rtos (getvar "CDATE") 2 8)
        result (strcat root "toggle-result-" nonce ".txt"))
  (nanocad-mcp-write (strcat root "toggle-request.txt") "w" nonce)
  (nanocad-mcp-write journal "a" "lisp: startapp")
  (setq started (startapp
    (nanocad-mcp-arg (strcat env "\\nanoCAD-MCP\\.venv\\Scripts\\pythonw.exe"))
    (nanocad-mcp-arg (strcat env "\\nanoCAD-MCP\\cad_access.py"))))
  (cond
    ((not started)
     (nanocad-mcp-write journal "a" "lisp: startapp failed")
     "\nnanoCAD MCP: could not start the helper. Check the installation.")
    (T
     (nanocad-mcp-write journal "a" "lisp: waiting")
     (setq stop (+ (getvar "DATE") (/ 8.0 86400.0)) n 0)
     (while (and (not code) (< n 500000) (< (getvar "DATE") stop))
       (setq code (nanocad-mcp-read result) n (1+ n)))
     (nanocad-mcp-write journal "a" (strcat "lisp: answer " (if code code "none")))
     (cond
       ((not code) "\nnanoCAD MCP: no answer from the helper. Check the installation.")
       ((= code "10") "\nnanoCAD MCP: ON")
       ((= code "11") "\nnanoCAD MCP: OFF")
       ((= code "13") "\nnanoCAD MCP: could not tell which nanoCAD window pressed the button.")
       (T "\nnanoCAD MCP: access could not be changed. Check the installation.")))))

(defun nanocad-mcp-toggle (/ env root journal text)
  (setq env (getenv "LOCALAPPDATA")
        root (strcat env "/nanoCAD-MCP/")
        journal (strcat root "toggle.log"))
  (nanocad-mcp-write journal "w" "lisp: start")
  (setq text (nanocad-mcp-press env root journal))
  (nanocad-mcp-write journal "a" "lisp: returned")
  (princ text)
  (nanocad-mcp-write journal "a" "lisp: printed")
  (princ))
