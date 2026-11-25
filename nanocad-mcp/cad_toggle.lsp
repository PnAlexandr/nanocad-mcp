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

(defun nanocad-mcp-pack (pack / root)
  (setq root (strcat (getenv "LOCALAPPDATA") "\\nanoCAD-MCP\\"))
  (if (not (startapp (nanocad-mcp-arg (strcat root ".venv\\Scripts\\pythonw.exe"))
          (strcat (nanocad-mcp-arg (strcat root "tool_catalog.py")) " --toggle-pack " pack " --notify")))
    (princ "\nnanoCAD MCP: could not open the command settings."))
  (princ))

(defun nanocad-mcp-window (root action / result)
  (if (and (= action "on") (not (member "NCMCP_WINDOW" (atoms-family 1 '("NCMCP_WINDOW")))))
    (command "_-NETLOAD" (strcat root "NanoCadMcp.Window26.dll")))
  (if (member "NCMCP_WINDOW" (atoms-family 1 '("NCMCP_WINDOW")))
    (progn
      (setq result (vl-catch-all-apply 'NCMCP_WINDOW (list action)))
      (if (vl-catch-all-error-p result) (vl-catch-all-error-message result) result))
    "Window26 DLL unavailable"))

(defun nanocad-mcp-press (env root journal / nonce result started code n stop binding)
  (setq nonce (vl-filename-base (vl-filename-mktemp "ncmcp"))
        result (strcat root "toggle-result-" nonce ".txt"))
  (nanocad-mcp-write journal "a" "lisp: startapp")
  (setq started (startapp
    (nanocad-mcp-arg (strcat env "\\nanoCAD-MCP\\.venv\\Scripts\\pythonw.exe"))
    (strcat (nanocad-mcp-arg (strcat env "\\nanoCAD-MCP\\cad_access.py")) " --button " nonce)))
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
     (if code (vl-file-delete result))
     (cond
       ((not code) "\nnanoCAD MCP: no answer from the helper. Check the installation.")
       ((= code "10-26")
        (setq binding (nanocad-mcp-window root "on"))
        (if (= binding "OK") "\nnanoCAD MCP: ON (this window)"
          (strcat "\nnanoCAD MCP: ON, window registration failed: " binding)))
       ((= code "11-26")
        (nanocad-mcp-window root "off")
        "\nnanoCAD MCP: OFF")
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
