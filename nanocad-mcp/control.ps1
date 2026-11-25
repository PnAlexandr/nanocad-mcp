param([switch]$InstallShortcut)

$ErrorActionPreference = 'Stop'

if ($InstallShortcut) {
    $desktop = [Environment]::GetFolderPath('DesktopDirectory')
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $desktop 'nanoCAD MCP.lnk'))
    $shortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'
    $shortcut.Arguments = '-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.Description = 'Параметры подключения nanoCAD к ChatGPT через MCP'
    $shortcut.Save()
    return
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()

try {
    $form = [Windows.Forms.Form]::new()
    $form.Text = 'nanoCAD MCP'
    $form.Font = [Drawing.Font]::new('Segoe UI', 10)
    $form.AutoScaleMode = 'Dpi'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.ClientSize = [Drawing.Size]::new(600, 230)

    $panel = [Windows.Forms.TableLayoutPanel]::new()
    $panel.Dock = 'Fill'
    $panel.Padding = [Windows.Forms.Padding]::new(18, 14, 18, 14)
    $panel.ColumnCount = 1
    [void]$panel.ColumnStyles.Add([Windows.Forms.ColumnStyle]::new('Percent', 100))
    $form.Controls.Add($panel)

    function Add-Label([string]$Text, [int]$TopMargin = 0) {
        $label = [Windows.Forms.Label]::new()
        $label.Text = $Text
        $label.AutoSize = $true
        $label.Margin = [Windows.Forms.Padding]::new(3, $TopMargin, 3, 2)
        $panel.Controls.Add($label)
    }

    function Add-PathField([string]$Label, [string]$Path) {
        Add-Label $Label 6
        $field = [Windows.Forms.TextBox]::new()
        $field.Text = $Path
        $field.ReadOnly = $true
        $field.Dock = 'Fill'
        $field.add_Enter({ $this.SelectAll() })
        $panel.Controls.Add($field)
    }

    Add-Label 'Подключение к ChatGPT для Windows · тип STDIO'
    Add-PathField 'Команда' (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
    Add-PathField 'Аргумент' (Join-Path $PSScriptRoot 'nanocad_mcp.py')

    Add-Label 'Включите доступ внутри nanoCAD на вкладке MCP или панели «nanoCAD MCP».' 12
    [void]$form.ShowDialog()
} finally {
    if ($form) { $form.Dispose() }
}
